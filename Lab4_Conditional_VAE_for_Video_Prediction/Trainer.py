import os
import argparse
import numpy as np
import torch
import torch.nn as nn
from torchvision import transforms
from torch.utils.data import DataLoader
import torch.nn.utils as utils

from modules import Generator, Gaussian_Predictor, Decoder_Fusion, Label_Encoder, RGB_Encoder

from dataloader import Dataset_Dance
from torchvision.utils import save_image
import random
import torch.optim as optim
from torch import stack

from tqdm import tqdm
import imageio

import matplotlib.pyplot as plt
from math import log10

def Generate_PSNR(imgs1, imgs2, data_range=1.):
    """PSNR for torch tensor"""
    mse = nn.functional.mse_loss(imgs1, imgs2, reduction='mean')
    # mse = nn.functional.mse_loss(imgs1, imgs2) # wrong computation for batch size > 1
    psnr = 20 * log10(data_range) - 10 * torch.log10(mse)
    return psnr

def kl_criterion(mu, logvar, batch_size):
  KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
  KLD /= batch_size  
  return KLD


class kl_annealing():
    def __init__(self, args, current_epoch=0):
        self.cur_iter = 0

        self.type = args.kl_anneal_type
        self.cycle = args.kl_anneal_cycle
        self.ratio = args.kl_anneal_ratio

        self.total_iter = args.num_epoch * args.train_vi_len
        self.beta_list = np.ones(self.total_iter)

        if(self.type == 'Cyclical'):
            self.frange_cycle_linear(self.total_iter, n_cycle=self.cycle, ratio=self.ratio)

        elif(self.type == 'Monotonic'):
            self.cycle= 1
            self.frange_cycle_linear(self.total_iter, n_cycle=self.cycle, ratio=0.25)

        elif(self.type == 'None'):
            self.beta_list = np.zeros(self.total_iter)

    def update(self):
        self.cur_iter += 1
    
    def get_beta(self):
        return self.beta_list[self.cur_iter]

    def frange_cycle_linear(self, n_iter, start=0.0, stop=1.0,  n_cycle=1, ratio=1):
        period = n_iter / n_cycle
        step = (stop - start) / (period * ratio)

        for c in range(n_cycle):
            v, i = start, 0
            while v <= stop and (int(i + c * period) < n_iter):
                self.beta_list[int(i + c * period)] = v
                v += step
                i += 1


class VAE_Model(nn.Module):
    def __init__(self, args):
        super(VAE_Model, self).__init__()
        self.args = args
        
        # Modules to transform image from RGB-domain to feature-domain
        self.frame_transformation = RGB_Encoder(3, args.F_dim)
        self.label_transformation = Label_Encoder(3, args.L_dim)
        
        # Conduct Posterior prediction in Encoder
        self.Gaussian_Predictor   = Gaussian_Predictor(args.F_dim + args.L_dim, args.N_dim)
        self.Decoder_Fusion       = Decoder_Fusion(args.F_dim + args.L_dim + args.N_dim, args.D_out_dim)
        
        # Generative model
        self.Generator            = Generator(input_nc=args.D_out_dim, output_nc=3)
        
        self.optim      = optim.Adam(self.parameters(), lr=self.args.lr)
        self.scheduler  = optim.lr_scheduler.MultiStepLR(self.optim, milestones=[2, 5], gamma=0.1)
        self.kl_annealing = kl_annealing(args, current_epoch=0)
        self.mse_criterion = nn.MSELoss()
        self.current_epoch = 0
        
        # Teacher forcing arguments
        self.tfr = args.tfr
        self.tfr_d_step = args.tfr_d_step
        self.tfr_sde = args.tfr_sde
        
        self.train_vi_len = args.train_vi_len
        self.val_vi_len   = args.val_vi_len
        self.batch_size = args.batch_size

        self.train_loss_list = []
        self.val_PSNR_list = []
        self.tfr_list = []
        
    def forward(self, img, label):
        pass
    
    def training_stage(self):

        for i in range(self.args.num_epoch):
            train_loader = self.train_dataloader()
            adapt_TeacherForcing = True if random.random() < self.tfr else False

            train_loss = 0

            for (img, label) in (pbar := tqdm(train_loader, ncols=180)):
                img = img.to(self.args.device)
                label = label.to(self.args.device)
                loss = self.training_one_step(img, label, adapt_TeacherForcing)

                train_loss += loss.item()
                
                beta = self.kl_annealing.get_beta()
                if adapt_TeacherForcing:
                    self.tqdm_bar('train [TeacherForcing: ON, {:.1f}], beta: {:.4f}'.format(self.tfr, beta), pbar, loss.detach().cpu(), lr=self.scheduler.get_last_lr()[0])
                else:
                    self.tqdm_bar('train [TeacherForcing: OFF, {:.1f}], beta: {:.4f}'.format(self.tfr, beta), pbar, loss.detach().cpu(), lr=self.scheduler.get_last_lr()[0])
            
            if self.current_epoch % self.args.per_save == 0 and self.current_epoch != 0:
                self.save(os.path.join(self.args.save_root, f"epoch={self.current_epoch}.ckpt"))

            self.train_loss_list.append(train_loss / len(train_loader)) # average loss per frame 
            self.eval()
            self.current_epoch += 1
            self.scheduler.step()
            self.tfr_list.append(self.tfr)
            self.teacher_forcing_ratio_update()
            self.kl_annealing.update()

        self.plot_loss_curve()
        self.plot_val_PSNR()
        self.plot_tfr()
            
    @torch.no_grad()
    def eval(self):
        val_loader = self.val_dataloader()
        for (img, label) in (pbar := tqdm(val_loader, ncols=180)):
            img = img.to(self.args.device)
            label = label.to(self.args.device)
            loss = self.val_one_step(img, label)
            self.tqdm_bar('val', pbar, loss.detach().cpu(), lr=self.scheduler.get_last_lr()[0])
    
    def training_one_step(self, img, label, adapt_TeacherForcing):

        img = img.permute(1, 0, 2, 3, 4) # change tensor into (seq, B, C, H, W)
        label = label.permute(1, 0, 2, 3, 4) # change tensor into (seq, B, C, H, W)
        assert label.shape[0] == 16, "Training pose seqence should be 16"
        assert img.shape[0] == 16, "Training video seqence should be 16"

        decoded_frame_list = [img[0].cpu()]
        out = img[0]

        mse = 0
        kld = 0

        for i in range(1, self.train_vi_len):
            z = torch.cuda.FloatTensor(self.batch_size, self.args.N_dim, self.args.frame_H, self.args.frame_W).normal_() # N(0, I)
            label_feat = self.label_transformation(label[i]) # P2
            human_feat_hat = self.frame_transformation(out) # X1 (prev pred frame)
            ground_truth = self.frame_transformation(img[i]) # X2

            parm = self.Decoder_Fusion(human_feat_hat, label_feat, z) # (P2, X1, N(0, I))
            out = self.Generator(parm) # X2_hat
            
            if adapt_TeacherForcing:
                mse += self.mse_criterion(out, img[i].to(out.device)) # X2_hat vs ground truth
            else:
                mse += self.mse_criterion(out, decoded_frame_list[-1].to(out.device)) # X2_hat vs prev pred frame
            
            _, mu, logvar = self.Gaussian_Predictor(ground_truth, label_feat) # latent distribution
            kld += kl_criterion(mu, logvar, self.batch_size)

            decoded_frame_list.append(out.cpu())

        beta = self.kl_annealing.get_beta()
        epsilon = 1e-8
        loss = mse + kld * beta + epsilon

        # print(f"loss of 16 frames:{loss}")
        self.optim.zero_grad()
        loss.backward()
        self.optimizer_step()
        torch.cuda.empty_cache()
        return loss / (self.train_vi_len-1) 

    def val_one_step(self, img, label):
        img = img.permute(1, 0, 2, 3, 4) # change tensor into (seq, B, C, H, W)
        label = label.permute(1, 0, 2, 3, 4) # change tensor into (seq, B, C, H, W)
        assert label.shape[0] == 630, "Validation pose seqence should be 630"
        assert img.shape[0] == 630, "Validation video seqence should be 630"

        decoded_frame_list = [img[0].cpu()] # X1
        label_list = []
        img_frame_list = []

        out = img[0]

        mse = 0
        kld = 0

        for i in range(1, self.val_vi_len):
            z = torch.cuda.FloatTensor(1, self.args.N_dim, self.args.frame_H, self.args.frame_W).normal_() # Z = N(0, I)
            label_feat = self.label_transformation(label[i]) # P2
            human_feat_hat = self.frame_transformation(out) # X1
            ground_truth = self.frame_transformation(img[i]) # X2

            parm = self.Decoder_Fusion(human_feat_hat, label_feat, z) # (P2, X1, N(0, I))
            out = self.Generator(parm) # X2

            mse += self.mse_criterion(out, img[i]) 
            _, mu, logvar = self.Gaussian_Predictor(ground_truth, label_feat) # latent distribution
            kld += kl_criterion(mu, logvar, self.batch_size)
            
            decoded_frame_list.append(out.cpu()) # X2_hat - X630_hat
            label_list.append(label[i].cpu()) # P2 - P630
            img_frame_list.append(img[i].cpu())

        beta = self.kl_annealing.get_beta()
        loss = mse + kld * beta

        if self.current_epoch == self.args.num_epoch-1:
            decoded_frame_list = decoded_frame_list[1:]

            generated_frame = stack(decoded_frame_list).permute(1, 0, 2, 3, 4)
            img_frame = stack(img_frame_list).permute(1, 0, 2, 3, 4)

            os.makedirs("./validation_frame", exist_ok=True)
            self.make_gif(generated_frame[0], f"./validation_frame/{str(self.args.kl_anneal_type)}/pred_seq.gif")
            self.make_gif(img_frame[0], f"./validation_frame/{str(self.args.kl_anneal_type)}/pose.gif")

            for i in range(629):
                PSNR = Generate_PSNR(generated_frame[0][i], img_frame[0][i])
                self.val_PSNR_list.append(PSNR.item())
            
        torch.cuda.empty_cache()
        return loss / (self.val_vi_len-1)
                
    def make_gif(self, images_list, img_name):
        new_list = []
        for img in images_list:
            new_list.append(transforms.ToPILImage()(img))
            
        new_list[0].save(img_name, format="GIF", append_images=new_list,
                    save_all=True, duration=40, loop=0)
    
    def train_dataloader(self):
        transform = transforms.Compose([
            transforms.Resize((self.args.frame_H, self.args.frame_W)),
            transforms.ToTensor()
        ])

        dataset = Dataset_Dance(root=self.args.DR, transform=transform, mode='train', video_len=self.train_vi_len, \
                                                partial=args.fast_partial if self.args.fast_train else args.partial)
        if self.current_epoch > self.args.fast_train_epoch:
            self.args.fast_train = False
            
        train_loader = DataLoader(dataset,
                                  batch_size=self.batch_size,
                                  num_workers=self.args.num_workers,
                                  drop_last=True,
                                  shuffle=False)  
        return train_loader
    
    def val_dataloader(self):
        transform = transforms.Compose([
            transforms.Resize((self.args.frame_H, self.args.frame_W)),
            transforms.ToTensor()
        ])
        dataset = Dataset_Dance(root=self.args.DR, transform=transform, mode='val', video_len=self.val_vi_len, partial=1.0)  
        val_loader = DataLoader(dataset,
                                  batch_size=1,
                                  num_workers=self.args.num_workers,
                                  drop_last=True,
                                  shuffle=False)  
        return val_loader
    
    def teacher_forcing_ratio_update(self):
        if self.current_epoch % self.tfr_sde == 0:
            self.tfr -= self.args.tfr_d_step
            self.tfr = max(self.tfr, 0.0)
            
    def tqdm_bar(self, mode, pbar, loss, lr):
        pbar.set_description(f"({mode}) Epoch {self.current_epoch}, lr:{lr}" , refresh=False)
        pbar.set_postfix(loss=float(loss), refresh=False)
        pbar.refresh()
        
    def save(self, path):
        torch.save({
            "state_dict": self.state_dict(),
            "optimizer" : self.state_dict(),  
            "lr"        : self.scheduler.get_last_lr()[0],
            "tfr"       : self.tfr,
            "last_epoch": self.current_epoch
        }, path)
        print(f"save ckpt to {path}")

    def load_checkpoint(self):
        if self.args.ckpt_path != None:
            checkpoint = torch.load(self.args.ckpt_path)
            self.load_state_dict(checkpoint['state_dict'], strict=True) 
            self.args.lr = checkpoint['lr']
            self.tfr = checkpoint['tfr']
            
            self.optim = optim.Adam(self.parameters(), lr=self.args.lr)
            self.scheduler = optim.lr_scheduler.MultiStepLR(self.optim, milestones=[2, 4], gamma=0.1)
            self.kl_annealing = kl_annealing(self.args, current_epoch=checkpoint['last_epoch'])
            self.current_epoch = checkpoint['last_epoch']

    def optimizer_step(self):
        nn.utils.clip_grad_norm_(self.parameters(), 1.)
        self.optim.step()

    def plot_loss_curve(self):
        plt.title(f'Loss Curve(KL annealing type:{str(self.args.kl_anneal_type)})')
        plt.xlabel('Epoch')
        plt.ylabel('Loss')

        # self.train_loss_list = [t.detach().cpu().numpy() for t in self.train_loss_list]
        loss_array = np.array(self.train_loss_list)
        plt.plot(loss_array, label=f'average loss per frame', color='r')

        plt.legend()
        plt.savefig(f'./figures/{str(self.args.kl_anneal_type)}/loss_curve_{str(self.args.kl_anneal_type)}.png')
        plt.clf()


    def plot_val_PSNR(self):
        plt.title(f'Per frame Quality(PSNR)')
        plt.xlabel('Frame index')
        plt.ylabel('PSNR')

        # self.val_PSNR_list = [t.detach().cpu().numpy() for t in self.val_PSNR_list]
        PSNR_array = np.array(self.val_PSNR_list)
        mean_value = np.mean(PSNR_array)
        plt.plot(PSNR_array, label=f'Avg_PSNR:{mean_value:.2f}', color='r')

        plt.legend()
        plt.savefig(f'./figures/{str(self.args.kl_anneal_type)}/PSNR.png')
        plt.clf()
        
    def plot_tfr(self):
        plt.title(f'Teacher forcing strategy')
        plt.xlabel('Epoch')
        plt.ylabel('Teacher forcing ratio')

        tfr_array = np.array(self.tfr_list)
        plt.plot(tfr_array, label=f'teacher forcing ratio', color='r')
        plt.legend()
        plt.savefig(f'./figures/{str(self.args.kl_anneal_type)}/teacher_forcing_ratio.png')
        plt.clf()


def main(args):
    
    os.makedirs(args.save_root, exist_ok=True)
    model = VAE_Model(args).to(args.device)
    model.load_checkpoint()
    if args.test:
        model.eval()
    else:
        model.training_stage()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument('--batch_size',    type=int,    default=4)
    parser.add_argument('--lr',            type=float,  default=0.0001,     help="initial learning rate")
    parser.add_argument('--device',        type=str, choices=["cuda", "cpu"], default="cuda")
    parser.add_argument('--optim',         type=str, choices=["Adam", "AdamW"], default="Adam")
    parser.add_argument('--gpu',           type=int, default=1)
    parser.add_argument('--test',          action='store_true')
    parser.add_argument('--store_visualization',      action='store_true', help="If you want to see the result while training")
    parser.add_argument('--DR',            type=str, required=True,  help="Your Dataset Path")
    parser.add_argument('--save_root',     type=str, required=True,  help="The path to save your data")
    parser.add_argument('--num_workers',   type=int, default=14)
    parser.add_argument('--num_epoch',     type=int, default=61,    help="number of total epoch")
    parser.add_argument('--per_save',      type=int, default=3,      help="Save checkpoint every seted epoch")
    parser.add_argument('--partial',       type=float, default=1.0,  help="Part of the training dataset to be trained")
    parser.add_argument('--train_vi_len',  type=int, default=16,     help="Training video length")
    parser.add_argument('--val_vi_len',    type=int, default=630,    help="valdation video length")
    parser.add_argument('--frame_H',       type=int, default=32,     help="Height input image to be resize")
    parser.add_argument('--frame_W',       type=int, default=64,     help="Width input image to be resize")
    
    
    # Module parameters setting
    parser.add_argument('--F_dim',         type=int, default=128,    help="Dimension of feature human frame")
    parser.add_argument('--L_dim',         type=int, default=32,     help="Dimension of feature label frame")
    parser.add_argument('--N_dim',         type=int, default=12,     help="Dimension of the Noise")
    parser.add_argument('--D_out_dim',     type=int, default=192,    help="Dimension of the output in Decoder_Fusion")
    
    # Teacher Forcing strategy
    parser.add_argument('--tfr',           type=float, default=1.0,  help="The initial teacher forcing ratio")
    parser.add_argument('--tfr_sde',       type=int,   default=10,   help="The epoch that teacher forcing ratio start to decay")
    parser.add_argument('--tfr_d_step',    type=float, default=0.1,  help="Decay step that teacher forcing ratio adopted")
    parser.add_argument('--ckpt_path',     type=str,   default=None, help="The path of your checkpoints")
    
    # Training Strategy
    parser.add_argument('--fast_train',         action='store_true')
    parser.add_argument('--fast_partial',       type=float, default=0.4,    help="Use part of the training data to fasten the convergence")
    parser.add_argument('--fast_train_epoch',   type=int, default=10,        help="Number of epoch to use fast train mode")
    
    # Kl annealing stratedy arguments
    parser.add_argument('--kl_anneal_type',     type=str, default='None',       help="")
    parser.add_argument('--kl_anneal_cycle',    type=int, default=10,               help="")
    parser.add_argument('--kl_anneal_ratio',    type=float, default=1,              help="")


    args = parser.parse_args()
    
    main(args)
