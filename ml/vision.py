import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

import torchvision.transforms as transforms
from torchvision.datasets import ImageFolder

import timm

model = timm.create_model(
    "mobilenetv3_small_100",  # or "efficientnet_b0"
    pretrained=True,
    num_classes=2  # keep vs discard
)   