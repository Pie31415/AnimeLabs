import os
import time
import datetime
import numpy as np
import matplotlib.pyplot as plt
from statistics import mean

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchvision.utils import save_image, make_grid
from torch.utils.tensorboard import SummaryWriter
from PIL import Image

from layers import *
from generator import Generator
from discriminator import Discriminator


class SAGAN():
    def __init__(self, dataloader, configs):

        # Data Loader
        self.dataloader = dataloader

        # model settings & hyperparams
        self.total_steps = configs.total_steps
        self.d_iters = configs.d_iters
        self.g_iters = configs.g_iters
        self.batch_size = configs.batch_size
        self.imsize = configs.imsize
        self.nz = configs.nz
        self.ngf = configs.ngf
        self.ndf = configs.ndf
        self.g_lr = configs.g_lr
        self.d_lr = configs.d_lr
        self.beta1 = configs.beta1
        self.beta2 = configs.beta2

        # instance noise
        self.inst_noise_sigma = configs.inst_noise_sigma
        self.inst_noise_sigma_iters = configs.inst_noise_sigma_iters

        # model logging and saving
        self.log_step = configs.log_step
        self.save_epoch = configs.save_epoch
        self.model_path = configs.model_path
        self.sample_path = configs.sample_path

        # pretrained
        self.pretrained_model = configs.pretrained_model

        # building
        self.build_model()

        # archive of all losses
        self.ave_d_losses = []
        self.ave_d_losses_real = []
        self.ave_d_losses_fake = []
        self.ave_d_gamma1 = []
        self.ave_d_gamma2 = []

        self.ave_g_losses = []
        self.ave_g_gamma1 = []
        self.ave_g_gamma2 = []

        if self.pretrained_model:
            self.load_pretrained()

    def build_model(self):
        # initialize Generator and Discriminator
        self.G = Generator(self.imsize, self.nz, self.ngf).cuda()
        self.D = Discriminator(self.ndf).cuda()

        # optimizers
        self.g_optimizer = optim.Adam(filter(
            lambda p: p.requires_grad, self.G.parameters()), self.g_lr, [self.beta1, self.beta2])
        self.d_optimizer = optim.Adam(filter(
            lambda p: p.requires_grad, self.D.parameters()), self.d_lr, [self.beta1, self.beta2])

        # tensorboard writer
        self.tb = SummaryWriter()

        print("Generator Parameters: ", parameters(self.G))
        print(self.G)
        print("Discriminator Parameters: ", parameters(self.D))
        print(self.D)

    def load_pretrained(self):
        """Loading pretrained model"""
        checkpoint = torch.load(
            os.path.join(self.model_path, "{}_sagan.pth".format(
                self.pretrained_model)))
        # load models
        self.G.load_state_dict(checkpoint["gen_state_dict"])
        self.D.load_state_dict(checkpoint["disc_state_dict"])

        # load optimizers
        self.g_optimizer.load_state_dict(checkpoint["gen_optimizer"])
        self.d_optimizer.load_state_dict(checkpoint["disc_optimizer"])

        # load losses
        self.ave_d_losses = checkpoint["ave_d_losses"]
        self.ave_d_losses_real = checkpoint["ave_d_losses_real"]
        self.ave_d_losses_fake = checkpoint["ave_d_losses_fake"]
        self.ave_d_gamma1 = checkpoint["ave_d_gamma1"]
        self.ave_d_gamma2 = checkpoint["ave_d_gamma2"]

        self.ave_g_losses = checkpoint["ave_g_losses"]
        self.ave_g_gamma1 = checkpoint["ave_g_gamma1"]
        self.ave_g_gamma2 = checkpoint["ave_g_gamma2"]

        print("Loading pretrained models (epoch: {})..!".format(
            self.pretrained_model))

    def reset_grad(self):
        """Reset gradients"""
        self.g_optimizer.zero_grad()
        self.d_optimizer.zero_grad()

    def train(self):
        step_per_epoch = len(self.dataloader)
        epochs = int(self.total_steps / step_per_epoch)

        # fixed z for sampling generator images
        fixed_z = tensor2var(torch.randn(self.batch_size, self.nz))

        print("Initiating Training")
        print("Epochs: {}, Total Steps: {}, Steps/Epoch: {}".
              format(epochs, self.total_steps, step_per_epoch))

        if self.pretrained_model:
            start_epoch = self.pretrained_model
        else:
            start_epoch = 0

        # train layers
        self.D.train()
        self.G.train()

        # total time
        start_time = time.time()
        for epoch in range(start_epoch, epochs):
            # local losses
            d_losses = []
            d_losses_real = []
            d_losses_fake = []
            d_gamma1 = []
            d_gamma2 = []

            g_losses = []
            g_gamma1 = []
            g_gamma2 = []

            data_iter = iter(self.dataloader)
            for step in range(step_per_epoch):
                # get real images
                real_images, _ = next(data_iter)
                real_images = tensor2var(real_images)

                # Instance noise - make random noise mean (0) and std for injecting
                inst_noise_mean = torch.full(
                    (real_images.size(0), 3, self.imsize, self.imsize), 0).cuda()
                inst_noise_std = torch.full(
                    (real_images.size(0), 3, self.imsize, self.imsize), self.inst_noise_sigma).cuda()

                # Instance noise std is linearly annealed from self.inst_noise_sigma to 0 thru self.inst_noise_sigma_iters
                inst_noise_sigma_curr = 0 if step > self.inst_noise_sigma_iters else (
                    1 - step/self.inst_noise_sigma_iters)*self.inst_noise_sigma
                inst_noise_std.fill_(inst_noise_sigma_curr)

                # ================== TRAIN DISCRIMINATOR ================== #

                for _ in range(self.d_iters):
                    self.reset_grad()

                    # TRAIN REAL
                    # creating instance noise
                    inst_noise = torch.normal(
                        mean=inst_noise_mean, std=inst_noise_std).cuda()
                    # get D output for real images + noise
                    d_real = self.D(real_images + inst_noise)
                    # compute hinge loss of D with real images
                    d_loss_real = loss_hinge_dis_real(d_real)
                    d_loss_real.backward()

                    # TRAIN FAKE
                    # generate fake images and get D output for fake images
                    z = tensor2var(torch.randn(real_images.size(0), self.nz))
                    fake_images = self.G(z)

                    # creating instance noise
                    inst_noise = torch.normal(
                        mean=inst_noise_mean, std=inst_noise_std).cuda()
                    # adding noise to fake images
                    # get D output for fake images
                    d_fake = self.D(fake_images + inst_noise)
                    # compute hinge loss of D with fake images
                    d_loss_fake = loss_hinge_dis_fake(d_fake)
                    d_loss_fake.backward()

                    d_loss = d_loss_real + d_loss_fake

                # optimize D
                self.d_optimizer.step()

                # ================== TRAIN GENERATOR ================== #

                for _ in range(self.g_iters):
                    self.reset_grad()

                    # create new latent vector
                    z = tensor2var(torch.randn(real_images.size(0), self.nz))

                    inst_noise = torch.normal(
                        mean=inst_noise_mean, std=inst_noise_std).cuda()
                    # generate fake images
                    fake_images = self.G(z)
                    g_fake = self.D(fake_images + inst_noise)

                    # compute hinge loss for G
                    g_loss = loss_hinge_gen(g_fake)
                    g_loss.backward()

                self.g_optimizer.step()

                # logging step progression
                if (step+1) % self.log_step == 0:
                    # logging losses and attention
                    d_losses.append(d_loss.item())
                    d_losses_real.append(d_loss_real.item())
                    d_losses_fake.append(d_loss_fake.item())
                    d_gamma1.append(self.D.attn1.gamma.data.item())
                    d_gamma2.append(self.D.attn2.gamma.data.item())

                    g_losses.append(g_loss.item())
                    g_gamma1.append(self.G.attn1.gamma.data.item())
                    g_gamma2.append(self.G.attn2.gamma.data.item())

                    # print out
                    elapsed = time.time() - start_time
                    elapsed = str(datetime.timedelta(seconds=elapsed))
                    print("Elapsed [{}], Epoch: [{}/{}], Step [{}/{}], g_loss: {:.4f}, d_loss: {:.4f},"
                          " d_loss_real: {:.4f}, d_loss_fake: {:.4f}".
                          format(elapsed, epoch+1, epochs, (step + 1), step_per_epoch,
                                 g_loss, d_loss, d_loss_real, d_loss_fake))

            # logging average losses over epoch
            self.ave_d_losses.append(mean(d_losses))
            self.ave_d_losses_real.append(mean(d_losses_real))
            self.ave_d_losses_fake.append(mean(d_losses_fake))
            self.ave_d_gamma1.append(mean(d_gamma1))
            self.ave_d_gamma2.append(mean(d_gamma2))

            self.ave_g_losses.append(mean(g_losses))
            self.ave_g_gamma1.append(mean(g_gamma1))
            self.ave_g_gamma2.append(mean(g_gamma2))

            # adding tensorboard logs
            self.tb.add_scalar("d loss", self.ave_d_losses[epoch], epoch)
            self.tb.add_scalar('d real', self.ave_d_losses_real[epoch], epoch)
            self.tb.add_scalar('d fake', self.ave_d_losses_fake[epoch], epoch)
            self.tb.add_scalar("g loss", self.ave_g_losses[epoch], epoch)

            self.tb.add_scalar("g gamma 1", self.ave_g_gamma1[epoch], epoch)
            self.tb.add_scalar("g gamma 2", self.ave_g_gamma2[epoch], epoch)
            self.tb.add_scalar("d gamma 1", self.ave_d_gamma1[epoch], epoch)
            self.tb.add_scalar("d gamma 2", self.ave_d_gamma2[epoch], epoch)

            # epoch update
            print("Elapsed [{}], Epoch: [{}/{}], ave_g_loss: {:.4f}, ave_d_loss: {:.4f},"
                  " ave_d_loss_real: {:.4f}, ave_d_loss_fake: {:.4f},"
                  " ave_g_gamma1: {:.4f}, ave_g_gamma2: {:.4f}, ave_d_gamma1: {:.4f}, ave_d_gamma2: {:.4f}".
                  format(elapsed, epoch+1, epochs, self.ave_g_losses[epoch], self.ave_d_losses[epoch],
                         self.ave_d_losses_real[epoch], self.ave_d_losses_fake[epoch],
                         self.ave_g_gamma1[epoch], self.ave_g_gamma2[epoch], self.ave_d_gamma1[epoch], self.ave_d_gamma2[epoch]))

            # sample images every epoch
            fake_images = self.G(fixed_z)
            fake_images = denorm(fake_images.data)
            save_image(fake_images,
                       os.path.join(self.sample_path,
                                    "Epoch {}.png".format(epoch+1)))

            # save model
            if (epoch+1) % self.save_epoch == 0:
                torch.save({
                    "gen_state_dict": self.G.state_dict(),
                    "disc_state_dict": self.D.state_dict(),
                    "gen_optimizer": self.g_optimizer.state_dict(),
                    "disc_optimizer": self.d_optimizer.state_dict(),
                    "ave_d_losses": self.ave_d_losses,
                    "ave_d_losses_real": self.ave_d_losses_real,
                    "ave_d_losses_fake": self.ave_d_losses_fake,
                    "ave_d_gamma1": self.ave_d_gamma1,
                    "ave_d_gamma2": self.ave_d_gamma2,
                    "ave_g_losses": self.ave_g_losses,
                    "ave_g_gamma1": self.ave_g_gamma1,
                    "ave_g_gamma2": self.ave_g_gamma2
                }, os.path.join(self.model_path, "{}_sagan.pth".format(epoch+1)))

                print("Saving models (epoch {})..!".format(epoch+1))

    def plot(self):
        plt.plot(self.ave_d_losses)
        plt.plot(self.ave_d_losses_real)
        plt.plot(self.ave_d_losses_fake)
        plt.plot(self.ave_g_losses)
        plt.legend(["d loss", "d real", "d fake", "g loss"], loc="upper left")
        plt.show()

    def sample(self, samples):
        z = tensor2var(torch.randn(samples, self.nz))
        images = self.G(z)
        images = denorm(images.data)
        # https://pytorch.org/docs/stable/_modules/torchvision/utils.html#save_image
        grid = make_grid(images, nrow=8, padding=2, pad_value=0,
                         normalize=False, range=None, scale_each=False)
        # Add 0.5 after unnormalizing to [0, 255] to round to nearest integer
        ndarr = grid.mul(255).add_(0.5).clamp_(0, 255).permute(
            1, 2, 0).to('cpu', torch.uint8).numpy()
        im = Image.fromarray(ndarr)
        plt.imshow(im)
        plt.show()
