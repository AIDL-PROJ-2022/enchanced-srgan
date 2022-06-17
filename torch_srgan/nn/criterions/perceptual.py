from typing import Dict, Iterable

import torchvision
import torch
from torch import nn
from torch.nn import functional as F
from torch.nn.modules.loss import _Loss


def _layer2index_vgg19(layer: str) -> int:
    """Map name of VGG19 layer to corresponding number in torchvision layer.

    Args:
        layer: name of the layer e.g. ``'conv1_1'``

    Returns:
        Number of layer (in network) with name `layer`.

    Examples:
        >>> _layer2index_vgg19('conv1_1')
        0
        >>> _layer2index_vgg19('pool5')
        36

    """
    block1 = ("conv1_1", "relu1_1", "conv1_2", "relu1_2", "pool1")
    block2 = ("conv2_1", "relu2_1", "conv2_2", "relu2_2", "pool2")
    block3 = ("conv3_1", "relu3_1", "conv3_2", "relu3_2", "conv3_3", "relu3_3", "conv3_4", "relu3_4", "pool3")
    block4 = ("conv4_1", "relu4_1", "conv4_2", "relu4_2", "conv4_3", "relu4_3", "conv4_4", "relu4_4", "pool4")
    block5 = ("conv5_1", "relu5_1", "conv5_2", "relu5_2", "conv5_3", "relu5_3", "conv5_4", "relu5_4", "pool5")
    layers_order = block1 + block2 + block3 + block4 + block5
    vgg19_layers = {n: idx for idx, n in enumerate(layers_order)}

    return vgg19_layers[layer]


class PerceptualLoss(_Loss):
    """
    The Perceptual Loss.

    Calculates loss between features of `model` (VGG19 is used)
    for input (produced by generator) and target (real) images.

    Args:
        layers: Dict of layers names and weights (to balance different layers).
        mean: List of float values used for data standardization.
            If there is no need to normalize data, please use [0., 0., 0.].
        std: List of float values used for data standardization.
            If there is no need to normalize data, please use [1., 1., 1.].
    """

    def __init__(self, layers: Dict[str, float], mean: Iterable[float] = (0.485, 0.456, 0.406),
                 std: Iterable[float] = (0.229, 0.224, 0.225)):
        super(PerceptualLoss).__init__()

        w_sum = sum(layers.values())
        self.layers = {str(_layer2index_vgg19(k)): w / w_sum for k, w in layers.items()}

        last_layer = max(map(_layer2index_vgg19, layers))
        model = torchvision.models.vgg19(pretrained=True)
        network = nn.Sequential(*list(model.features.children())[:last_layer + 1]).eval()
        for param in network.parameters():
            param.requires_grad = False
        self.model = network

        self.mean = torch.tensor(mean).view(1, -1, 1, 1)
        self.std = torch.tensor(std).view(1, -1, 1, 1)

    def _get_features(self, x: torch.Tensor) -> Dict[str, torch.Tensor]:
        # Normalize input tensor
        x = (x - self.mean.to(x)) / self.std.to(x)

        # Extract network features
        features: Dict[str, torch.Tensor] = {}
        for name, module in self.model.named_children():
            x = module(x)
            if name in self.layers:
                features[name] = x

        return features

    def forward(self, fake_data: torch.Tensor, real_data: torch.Tensor) -> torch.Tensor:
        """Forward propagation method for the perceptual loss.

        Args:
            fake_data: Batch of input (fake, generated) images.
            real_data: Batch of target (real, ground truth) images.

        Returns:
            Loss, scalar.
        """
        fake_features = self._get_features(fake_data)
        real_features = self._get_features(real_data)

        # Calculate weighted sum of distances between real and fake features
        loss = torch.tensor(0.0, requires_grad=True).to(fake_data)
        for layer, weight in self.layers.items():
            layer_loss = F.l1_loss(fake_features[layer], real_features[layer])
            loss = loss + weight * layer_loss

        return loss
