"""
ESRGAN Network training script.
"""

import argparse
import collections
import json
import os
import time
import albumentations as A
import torch
import piq

from typing import List, Tuple, Iterable

from torch.utils.data import DataLoader, ConcatDataset
from tqdm import tqdm

import torch_srgan.datasets as datasets
from torch_srgan.loggers.wandb import WandbLogger
from torch_srgan.models.RRDBNet import RRDBNet
from torch_srgan.models.VGG_discriminator import VGGStyleDiscriminator
from torch_srgan.nn.criterions import AdversarialLoss, ContentLoss, PerceptualLoss


class AverageMeter(object):
    """
    Computes and stores the average, maximum and minimum value.
    """

    def __init__(self, name: str, fmt: str = ":f"):
        self.name = name
        self.fmt = fmt
        self.avg = 0
        self.max = None
        self.min = None
        self._sum = 0
        self._count = 0

    def reset(self):
        self.avg = 0
        self.max = None
        self.min = None
        self._sum = 0
        self._count = 0

    def update(self, val: float, n: int = 1):
        # Compute the new maximum and minimum value
        self.max = max(val, self.max) if self.max is not None else val
        self.min = min(val, self.min) if self.min is not None else val
        # Compute the new average value
        self._sum += val * n
        self._count += n
        self.avg = self._sum / self._count

    def __str__(self):
        fmtstr = "{name}: {avg" + self.fmt + "} (Min: {min" + self.fmt + "} / Max: {max" + self.fmt + "})"
        return fmtstr.format(**self.__dict__)


def _set_img_datasets_transforms(img_datasets: Iterable[datasets.ImagePairDataset],
                                 cr_patch_size: Tuple[int, int], train_aug_transforms: List):
    for dataset in img_datasets:
        dataset.set_dataset_transforms(cr_patch_size, train_aug_transforms)


def _setup_stage_datasets(train_datasets_list: Iterable[datasets.ImagePairDataset],
                          val_datasets_list: Iterable[datasets.ImagePairDataset],
                          cr_patch_size: Tuple[int, int], train_aug_transforms: List):
    # Set train and validate dataset transforms
    _set_img_datasets_transforms(train_datasets_list, cr_patch_size, train_aug_transforms)
    _set_img_datasets_transforms(val_datasets_list, cr_patch_size, [])

    # Define the dataset loaders
    train_dataloader = DataLoader(
        ConcatDataset(train_datasets_list), batch_size=hparams["batch_size"], num_workers=n_cpu,
        shuffle=True, pin_memory=True, drop_last=True
    )
    val_dataloader = DataLoader(
        ConcatDataset(val_datasets_list), batch_size=hparams["batch_size"], num_workers=n_cpu,
        shuffle=False, pin_memory=True, drop_last=True
    )

    return train_dataloader, val_dataloader


def _measure_psnr_ssim_metrics(hr_images: torch.Tensor, out_images: torch.Tensor):
    # Measure PSNR metric against ground truth image
    psnr = piq.psnr(hr_images, out_images, data_range=1.0, reduction="mean", convert_to_greyscale=False)
    psnr_metric.update(psnr.item(), hr_images.size(0))
    # Measure SSIM metric against ground truth image
    ssim, _ = piq.ssim(
        hr_images, out_images, kernel_size=11, kernel_sigma=1.5, k1=0.01, k2=0.03,
        data_range=1.0, reduction="mean", full=True
    )
    ssim_metric.update(ssim.item(), hr_images.size(0))


def pretraining_stage_train(dataloader: DataLoader, optimizer: torch.optim.Optimizer,
                            scheduler: torch.optim.lr_scheduler.StepLR, epoch_i: int, num_epoch: int):
    # Switch generator model to train mode
    generator.train()

    # Reset metrics
    content_loss_metric.reset()

    # Iterate over train image batches for this epoch
    for i, (lr_images, hr_images) in enumerate(tqdm(dataloader, desc="[TRAINING]", leave=False)):

        # Move images to device
        lr_images = lr_images.to(device)
        hr_images = hr_images.to(device)

        # Set optimizer gradients to zero
        optimizer.zero_grad()

        # Generate a high resolution images from low resolution input
        out_images = generator(lr_images)

        # Measure pixel-wise content loss against ground truth image
        loss = content_loss(out_images, hr_images)
        content_loss_metric.update(loss.item(), lr_images.size(0))

        # Backpropagate gradients and go to next optimizer and scheduler step
        loss.backward()
        optimizer.step()
        scheduler.step()

        # Log processed images and results
        if (epoch_i % 200 == 0 or epoch_i == 1 or epoch_i == num_epoch) and i == 0:
            # logger.log_image_transforms(epoch, "train", transforms)
            logger.log_images("train", lr_images, out_images, hr_images)

    # Log metrics
    tqdm.write(
        f"TRAIN METRICS [{epoch_i}/{num_epoch}]:\r\n"
        f"  - {str(content_loss_metric)}\r\n"
    )
    logger.log_metrics(
        "PSNR-driven", "train", {
            "content_loss": content_loss_metric.avg,
        }
    )


def validate_model(dataloader: DataLoader, stage: str, epoch_i: int, num_epoch: int):
    # Switch generator and discriminator model to evaluation mode
    generator.eval()
    discriminator.eval()

    # Reset metrics
    content_loss_metric.reset()
    psnr_metric.reset()
    ssim_metric.reset()

    # Disable gradient propagation
    with torch.no_grad():
        # Iterate over validation image batches for this epoch
        for i, (lr_images, hr_images) in enumerate(tqdm(dataloader, desc="[VALIDATION]", leave=False)):

            # Move images to device
            lr_images = lr_images.to(device)
            hr_images = hr_images.to(device)

            # Generate a high resolution images from low resolution input
            out_images = generator(lr_images)
            # Make sure that images are between the range [0, 1]
            out_images = torch.clamp(out_images, min=0, max=1)

            # Measure pixel-wise content loss against ground truth image (Pixel-wise loss)
            c_loss = content_loss(out_images, hr_images)
            content_loss_metric.update(c_loss.item(), lr_images.size(0))

            # Measure perceptual loss against ground truth image (VGG-based loss)
            p_loss = perceptual_loss(out_images, hr_images)
            perceptual_loss_metric.update(p_loss.item(), hr_images.size(0))

            # Measure PSNR and SSIM metric against ground truth image
            _measure_psnr_ssim_metrics(hr_images, out_images)

            # Log processed images and results
            if (epoch_i % 100 == 0 or epoch_i == 1 or epoch_i == num_epoch) and i == 0:
                logger.log_images("validation", lr_images, out_images, hr_images)

    # Log metrics
    tqdm.write(
        f"VALIDATION METRICS [{epoch_i}/{num_epoch}]:\r\n"
        f"  - {str(content_loss_metric)}\r\n"
        f"  - {str(perceptual_loss_metric)}\r\n"
        f"  - {str(psnr_metric)}\r\n"
        f"  - {str(ssim_metric)}\r\n"
    )
    logger.log_metrics(
        stage, "validation", {
            "content_loss": content_loss_metric.avg,
            "perceptual_loss": perceptual_loss_metric.avg,
            "PSNR": psnr_metric.avg,
            "SSIM": ssim_metric.avg
        }
    )


def exec_pretraining_stage(num_epoch: int, cr_patch_size: Tuple[int, int], lr: float,
                           sched_step: int, sched_gamma: float, train_aug_transforms: List,
                           train_datasets_list: Iterable[datasets.ImagePairDataset],
                           val_datasets_list: Iterable[datasets.ImagePairDataset],
                           start_epoch_i: int = 1, store_checkpoint: bool = True):
    # Define optimizer and scheduler for pre-training stage
    optimizer = torch.optim.Adam(generator.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=sched_step, gamma=sched_gamma)

    # Setup train and validation datasets and its corresponding loader
    train_dataloader, val_dataloader = _setup_stage_datasets(
        train_datasets_list, val_datasets_list, cr_patch_size, train_aug_transforms
    )

    # Set stage start time
    start_ts = int(time.time())
    # Define checkpoint file path
    checkpoint_file_path = f"saved_models/{start_ts}_RRDB_PSNR_x{hparams['scale_factor']}.pth"

    print()
    print(">>> Pre-training stage (PSNR driven)")
    print()
    print("-" * 64)
    print()

    # Train model for specified number of epoch
    for epoch_i in tqdm(range(start_epoch_i, num_epoch+1), desc="[PRE-TRAINING (PSNR)]"):

        # Train model
        pretraining_stage_train(train_dataloader, optimizer, scheduler, epoch_i, num_epoch)
        # Validate model
        validate_model(val_dataloader, "PSNR-driven", epoch_i, num_epoch)

        # Print metrics after this epoch
        tqdm.write("-" * 64 + "\r\n")

        # Go to next logger step
        logger.step()

        # Store checkpoint
        if store_checkpoint:
            checkpoint = {
                "model_state_dict": generator.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "scheduler_state_dict": scheduler.state_dict(),
                "hparams": hparams,
                "epoch_i": epoch_i
            }
            torch.save(checkpoint, checkpoint_file_path)

    # Store model data after pre-training stage
    pretrain_model_data = {
        "model_state_dict": generator.state_dict(),
        "hparams": hparams,
    }
    torch.save(
        pretrain_model_data, f"saved_models/{start_ts}_RRDB_PSNR_x{hparams['scale_factor']}.pth"
    )
    # Remove last checkpoint
    if os.path.exists(checkpoint_file_path):
        os.remove(checkpoint_file_path)


def training_stage_train(dataloader: DataLoader, g_optimizer: torch.optim.Optimizer, d_optimizer: torch.optim.Optimizer,
                         g_scheduler: torch.optim.lr_scheduler.MultiStepLR,
                         d_scheduler: torch.optim.lr_scheduler.MultiStepLR,
                         g_adversarial_loss_scaling: float, g_content_loss_scaling: float,
                         epoch_i: int, num_epoch: int):
    # Switch generator and discriminator models to train mode
    generator.train()
    discriminator.train()

    # Reset metrics
    content_loss_metric.reset()
    perceptual_loss_metric.reset()
    g_adversarial_loss_metric.reset()
    g_total_loss_metric.reset()
    d_adversarial_loss_metric.reset()

    # Iterate over train image batches for this epoch
    for i, (lr_images, hr_images) in enumerate(tqdm(dataloader, desc="[TRAINING]", leave=False)):

        # Move images to device
        lr_images = lr_images.to(device)
        hr_images = hr_images.to(device)

        ###################
        # Train Generator #
        ###################

        # Disable discriminator gradients
        for p in discriminator.parameters():
            p.requires_grad = False

        # Set optimizer gradients to zero
        g_optimizer.zero_grad()

        # Generate a high resolution images from low resolution input
        out_images = generator(lr_images)

        # Measure perceptual loss against ground truth image (VGG-based loss)
        p_loss = perceptual_loss(out_images, hr_images)
        perceptual_loss_metric.update(p_loss.item(), hr_images.size(0))

        # Measure pixel-wise content loss against ground truth image (Pixel-wise loss)
        c_loss = content_loss(out_images, hr_images)
        content_loss_metric.update(c_loss.item(), hr_images.size(0))

        # Evaluate generated images with the discriminator
        g_pred_real = discriminator(hr_images).detach()
        g_pred_fake = discriminator(out_images)

        # Calculate generator adversarial loss (relativistic GAN loss)
        g_a_loss_real = adversarial_loss(g_pred_real - g_pred_fake.mean(0, keepdim=True), target_is_real=False)
        g_a_loss_fake = adversarial_loss(g_pred_fake - g_pred_real.mean(0, keepdim=True), target_is_real=True)
        g_a_loss = (g_a_loss_fake + g_a_loss_real) / 2
        g_adversarial_loss_metric.update(g_a_loss.item(), hr_images.size(0))

        g_total_loss = p_loss + (c_loss * g_content_loss_scaling) + (g_a_loss * g_adversarial_loss_scaling)
        g_total_loss_metric.update(g_total_loss.item(), hr_images.size(0))

        # Backpropagate gradients and go to next optimizer and scheduler step
        g_total_loss.backward()
        g_optimizer.step()
        g_scheduler.step()

        #######################
        # Train Discriminator #
        #######################

        # Enable discriminator gradients
        for p in discriminator.parameters():
            p.requires_grad = True

        # Set optimizer gradients to zero
        d_optimizer.zero_grad()

        # Evaluate real and generated images with the discriminator
        d_pred_real = discriminator(hr_images)
        d_pred_fake = discriminator(out_images.detach())

        loss_real = adversarial_loss(d_pred_real - d_pred_fake.mean(0, keepdim=True), target_is_real=True)
        loss_fake = adversarial_loss(d_pred_fake - d_pred_real.mean(0, keepdim=True), target_is_real=False)

        # Total loss
        d_loss = (loss_real + loss_fake) / 2
        d_adversarial_loss_metric.update(d_loss.item(), hr_images.size(0))

        # Go to next optimizer and scheduler step
        d_loss.backward()
        d_optimizer.step()
        d_scheduler.step()

        ###########
        # Logging #
        ###########

        # Log processed images and results
        if (epoch_i % 100 == 0 or epoch_i == 1 or epoch_i == num_epoch) and i == 0:
            # logger.log_image_transforms(epoch, "train", transforms)
            logger.log_images("train", lr_images, out_images, hr_images)

    # Log metrics
    tqdm.write(
        f"TRAIN METRICS [{epoch_i}/{num_epoch}]:\r\n"
        f"  - {str(content_loss_metric)}\r\n"
        f"  - {str(perceptual_loss_metric)}\r\n"
        f"  - {str(g_adversarial_loss_metric)}\r\n"
        f"  - {str(g_total_loss_metric)}\r\n"
        f"  - {str(d_adversarial_loss_metric)}\r\n"
    )
    logger.log_metrics(
        "GAN-based", "train", {
            "content_loss": content_loss_metric.avg,
            "perceptual_loss": perceptual_loss_metric.avg,
            "g_adversarial_loss": g_adversarial_loss_metric.avg,
            "g_total_loss": g_total_loss_metric.avg,
            "d_adversarial_loss": d_adversarial_loss_metric.avg,
        }
    )


def exec_training_stage(num_epoch: int, cr_patch_size: Tuple[int, int], g_lr: float, d_lr: float,
                        g_sched_steps: List[int], g_sched_gamma: float, d_sched_steps: List[int], d_sched_gamma: float,
                        g_adversarial_loss_scaling: float, g_content_loss_scaling: float,
                        train_datasets_list: Iterable[datasets.ImagePairDataset],
                        val_datasets_list: Iterable[datasets.ImagePairDataset],
                        train_aug_transforms: List, store_checkpoint: bool = True):
    # Define optimizers for training stage
    g_optimizer = torch.optim.Adam(generator.parameters(), lr=g_lr, betas=(0.9, 0.99))
    d_optimizer = torch.optim.Adam(discriminator.parameters(), lr=d_lr, betas=(0.9, 0.99))
    # Define schedulers for training stage
    g_scheduler = torch.optim.lr_scheduler.MultiStepLR(g_optimizer, milestones=g_sched_steps, gamma=g_sched_gamma)
    d_scheduler = torch.optim.lr_scheduler.MultiStepLR(d_optimizer, milestones=d_sched_steps, gamma=d_sched_gamma)

    # Setup train and validation datasets and its corresponding loader
    train_dataloader, val_dataloader = _setup_stage_datasets(
        train_datasets_list, val_datasets_list, cr_patch_size, train_aug_transforms
    )

    # Set stage start time
    start_ts = int(time.time())
    # Define checkpoint file path
    checkpoint_file_path = f"saved_models/{start_ts}_RRDB_ESRGAN_x{hparams['scale_factor']}.pth"

    print()
    print(">>> Training stage (GAN based)")
    print()
    print("-" * 64)
    print()

    # Train model for specified number of epoch
    for epoch_i in tqdm(range(1, num_epoch+1), desc="[TRAINING (GAN)]"):

        # Train model
        training_stage_train(
            train_dataloader, g_optimizer, d_optimizer, g_scheduler, d_scheduler,
            g_adversarial_loss_scaling, g_content_loss_scaling,
            epoch_i, num_epoch
        )
        # Validate model
        validate_model(val_dataloader, "GAN-based", epoch_i, num_epoch)

        # Print metrics after this epoch
        tqdm.write("-" * 64 + "\r\n")

        # Go to next logger step
        logger.step()

        # Store checkpoint
        if store_checkpoint:
            checkpoint = {
                "g_model_state_dict": generator.state_dict(),
                "d_model_state_dict": discriminator.state_dict(),
                "g_optimizer_state_dict": g_optimizer.state_dict(),
                "d_optimizer_state_dict": d_optimizer.state_dict(),
                "g_scheduler_state_dict": g_scheduler.state_dict(),
                "d_scheduler_state_dict": d_scheduler.state_dict(),
                "hparams": hparams,
                "epoch_i": epoch_i
            }
            torch.save(checkpoint, checkpoint_file_path)

    # Store model data after pre-training stage
    train_model_data = {
        "g_model_state_dict": generator.state_dict(),
        "d_model_state_dict": discriminator.state_dict(),
        "hparams": hparams,
    }
    torch.save(
        train_model_data, f"saved_models/{start_ts}_RRDB_ESRGAN_x{hparams['scale_factor']}.pth"
    )
    # Remove last checkpoint
    if os.path.exists(checkpoint_file_path):
        os.remove(checkpoint_file_path)


if __name__ == '__main__':
    # parser = argparse.ArgumentParser()
    # parser.add_argument("--start-epoch", type=int, default=0, help="epoch to start training from")
    # opt = parser.parse_args()

    def debugger_is_active() -> bool:
        import sys
        """Return if the debugger is currently active"""
        gettrace = getattr(sys, 'gettrace', lambda: None)
        return gettrace() is not None

    device = torch.device("cuda" if torch.cuda.is_available() and not debugger_is_active() else "cpu")
    n_cpu = os.cpu_count()

    # Enable cudnn benchmarking if available
    if torch.backends.cudnn.is_available() and "cuda" in str(device):
        torch.backends.cudnn.benchmark = True

    # Create saved models directory if not exist
    os.makedirs("saved_models", exist_ok=True)

    hparams_file_path = "configs/pretraining_192_patch_size.json"

    with open(hparams_file_path, "r") as f:
        hparams = json.load(f)

    # Define train dataset augmentation transforms
    # Spatial transforms. Must be applied to both images (LR + HR)
    spatial_transforms = datasets.ImagePairDataset.PairedCompose([
        A.OneOf([
            A.Flip(p=0.75),
            A.Transpose(p=0.25)
        ], p=0.5)
    ])
    # Hard transforms. Must be applied only to LR images.
    hard_transforms = A.Compose([
        A.CoarseDropout(max_holes=8, max_height=2, max_width=2, p=0.25),
        A.ImageCompression(quality_lower=65, p=0.25),
    ])

    # Initialize generator and discriminator models
    generator = RRDBNet(
        img_channels=hparams["img_channels"], scale_factor=hparams["scale_factor"], **hparams["generator"]
    ).to(device)
    discriminator = VGGStyleDiscriminator(
        img_channels=hparams["img_channels"], **hparams["discriminator"]
    ).to(device)

    # # Transfer learning from pre-trained official model
    # data = torch.load("saved_models/ESRGAN_PSNR_SRx4_DF2K_official-150ff491.pth")
    # original_params = list(data["params"].items())
    # generator_state_dict = []
    # i = 0
    # for name, param in generator.named_parameters():
    #     if not name.endswith(".residual_scaling"):
    #         generator_state_dict.append((name, original_params[i][1]))
    #         i += 1
    #     else:
    #         generator_state_dict.append((name, param.data))
    # generator.load_state_dict(collections.OrderedDict(generator_state_dict))

    # Define datasets to use:
    # BSDS500
    bsds500_train_dataset = datasets.BSDS500(target='train', scale_factor=hparams["scale_factor"])
    bsds500_val_dataset = datasets.BSDS500(target='val', scale_factor=hparams["scale_factor"], download=False)
    # DIV2K
    div2k_train_dataset = datasets.DIV2K(target='train', scale_factor=hparams["scale_factor"])
    div2k_val_dataset = datasets.DIV2K(target='val', scale_factor=hparams["scale_factor"])

    # Define losses used during training
    content_loss = ContentLoss(**hparams["content_loss"]).to(device)
    perceptual_loss = PerceptualLoss(**hparams["perceptual_loss"]).to(device)
    adversarial_loss = AdversarialLoss().to(device)

    # Initialize logging interface
    logger = WandbLogger(
        proj_name='ESRGAN', entity_name="esrgan-aidl-2022", task='training', generator=generator
    )

    # Define metrics
    content_loss_metric = AverageMeter("Generator Content Loss", ":.4e")
    perceptual_loss_metric = AverageMeter("Generator Perceptual Loss", ":.4e")
    g_adversarial_loss_metric = AverageMeter("Generator Adversarial Loss", ":.4e")
    g_total_loss_metric = AverageMeter("Generator Total Loss", ":.4e")
    d_adversarial_loss_metric = AverageMeter("Discriminator Adversarial Loss", ":.4e")
    psnr_metric = AverageMeter("PSNR", ":.4f")
    ssim_metric = AverageMeter("SSIM", ":.4f")

    ####################################
    # Pre-training stage (PSNR driven) #
    ####################################

    start_epoch = 0
    logger.set_current_step(start_epoch + 1)

    # Define datasets to use
    train_datasets = [bsds500_train_dataset, div2k_train_dataset]
    val_datasets = [bsds500_val_dataset, div2k_val_dataset]

    # Execute supervised pre-training stage
    exec_pretraining_stage(
        **hparams["pretraining"], start_epoch_i=start_epoch+1,
        train_datasets_list=train_datasets, val_datasets_list=val_datasets,
        train_aug_transforms=[spatial_transforms, hard_transforms]
    )

    ##############################
    # Training stage (GAN based) #
    ##############################

    # Define datasets to use
    train_datasets = [div2k_train_dataset]
    val_datasets = [bsds500_val_dataset, div2k_val_dataset]

    # Execute supervised pre-training stage
    exec_training_stage(
        **hparams["training"],
        train_datasets_list=train_datasets, val_datasets_list=val_datasets,
        train_aug_transforms=[spatial_transforms]
    )

    #########################
    # Network Interpolation #
    #########################
