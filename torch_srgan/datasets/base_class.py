"""
Base class for image pair datasets defined for test, train and validate models.
"""

__author__ = "Marc Bermejo"
__license__ = "MIT"
__version__ = "0.1.0"
__status__ = "Development"

import os
import numpy as np
import cv2
import albumentations as A
import torch

from abc import ABC
from typing import Dict, List, Tuple
from torch.utils.data import Dataset
from albumentations.pytorch import ToTensorV2

from .transforms import PairedRandomCrop, PairedCenterCrop, SimpleNormalize
from ..utils.path_iter import images_in_dir


class ImagePairDataset(Dataset, ABC):
    """
    Image pair dataset base class used by all the datasets defined to train, test and validate SR models.

    Args:
        TODO
    """

    def __init__(self, scale_factor: int = 2, train: bool = False, patch_size: Tuple[int, int] = (96, 96),
                 base_dir: str = "data", hr_img_dir: str = "./", lr_img_dir: str = None,
                 transforms: List[A.BasicTransform] = None):
        # Define class member variables from input parameters
        self.scale_factor = scale_factor
        self.base_dir = base_dir
        self.hr_img_dir = os.path.join(base_dir, hr_img_dir)
        self.img_list: List[Dict[str, str]] = []

        # Check if user provided directories for HR and LR images
        if lr_img_dir:
            self.lr_img_dir = os.path.join(base_dir, lr_img_dir)
        else:
            self.lr_img_dir = None

        # Initialize image pair data dictionary
        self.data: List[Dict[str, str]] = []

        # Initialize transform pipeline
        transform_pipeline = []
        # Define pre-processing transform depending on if it is a train dataset or not and if a patch size was given
        if patch_size:
            if train:
                # Perform a random crop to both HR and LR images during training
                paired_crop = PairedRandomCrop(*patch_size, paired_img_scale=scale_factor, always_apply=True)
            else:
                # Perform a center crop to both HR and LR images during test/validation
                paired_crop = PairedCenterCrop(*patch_size, paired_img_scale=scale_factor, always_apply=True)
            # Append to full pipeline
            transform_pipeline.append(paired_crop)
        # Define user requested transformation pipeline (if any)
        if transforms:
            user_transforms = A.Compose(
                transforms,
                additional_targets={"scaled_image": "image"}
            )
            # Append to full pipeline
            transform_pipeline.append(user_transforms)
        # Define post-processing transforms (normalize image and convert it to Tensor)
        post_transforms = A.Compose(
            [SimpleNormalize(), ToTensorV2()],
            additional_targets={"scaled_image": "image"}
        )
        # Append to full pipeline
        transform_pipeline.append(post_transforms)

        # Define complete transformation pipeline
        self.transform = A.ReplayCompose([transform_pipeline])

    def _set_img_paths_from_folder(self):
        # Retrieve HR images from configured directory
        hr_images = images_in_dir(self.hr_img_dir)
        # Populate data array with HR images
        self.data = [{"hr": image} for image in hr_images]
        # If LR image directory is given, read also paired images from there
        if self.lr_img_dir:
            # Retrieve LR images from configured directory
            lr_images = images_in_dir(self.lr_img_dir)
            # Check that LR images array size matches HR size
            assert len(hr_images) == len(lr_images)
            # Populate data array with HR images
            for i in range(len(lr_images)):
                self.data[i].update({"lr": lr_images[i]})

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, torch.Tensor]:
        # Retrieve image pair from data array
        img_pair = self.data[index]
        # Read HR image from disk
        hr_image = cv2.imread(img_pair["hr"], cv2.IMREAD_UNCHANGED).astype(np.float32)
        # Check if LR image was found in dataset
        if not img_pair.get("lr", None):
            # Calculate LR image resize scaling factor
            lr_resize_scale = 1.0 / float(self.scale_factor)
            # Resize HR image to produce an LR image
            lr_image = cv2.resize(hr_image, None, fx=lr_resize_scale, fy=lr_resize_scale, interpolation=cv2.INTER_AREA)
        else:
            # Read LR image from disk
            lr_image = cv2.imread(img_pair["lr"], cv2.IMREAD_UNCHANGED).astype(np.float32)

        # Convert image color space to RGB
        lr_image = cv2.cvtColor(lr_image, cv2.COLOR_BGR2RGB)
        hr_image = cv2.cvtColor(hr_image, cv2.COLOR_BGR2RGB)

        # Apply transformation pipeline to both images
        # TODO: Log applied image transformations
        transformed = self.transform(image=lr_image, scaled_image=hr_image)

        return transformed["image"], transformed["scaled_image"]

    def __len__(self) -> int:
        """
        Get length of the dataset.

        Returns:
            Length of the dataset.
        """
        return len(self.data)
