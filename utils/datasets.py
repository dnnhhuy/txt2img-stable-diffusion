import torch
from torch import nn
from typing import Tuple
from pathlib import Path
import numpy as np
import os, sys
from torchvision import transforms
from torch.utils.data import DataLoader
from PIL import Image

sys.path.append("..")

def scale_img(x: torch.Tensor, old_range, new_range, clamp=False):
    old_min, old_max = old_range
    new_min, new_max = new_range
    x -= old_min
    x *= (new_max - new_min) / (old_max - old_min)
    x += new_min
    if clamp:
        x = torch.clamp(x, new_min, new_max)
    return x

class CustomDataset(torch.utils.data.Dataset):
    def __init__(self, data_dir: str, img_size: Tuple[int, int]):
        super().__init__()
        self.imgs, self.labels = self.load_data(data_dir)
        self.num_classes = len(self.labels)
        self.img_size = img_size
        
    def load_data(self, data_dir: str):
        img_paths = os.path.join(data_dir, 'sprites.npy')
        label_paths = os.path.join(data_dir, 'sprites_labels.npy')
        imgs = np.load(img_paths)
        labels = np.load(label_paths)
        return imgs, labels

    def get_image(self, index: int):
        img = Image.fromarray(self.imgs[index])
        img = img.resize(self.img_size)
        img = np.array(img)
        img = torch.tensor(img, dtype=torch.float32)
        img = scale_img(img, (0, 255), (-1, 1))
        img = img.permute(2, 0, 1)
        return img

    def get_label(self, index: int):
        return self.labels[index]
        
    def __len__(self):
        return self.imgs.shape[0]

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, str]:
        img = self.get_image(index)        
        label = self.get_label(index)
        return img, label
    
    
class DreamboothDataset(torch.utils.data.Dataset):
    def __init__(self, data_dir: str, img_size: Tuple[int, int]):
        super().__init__()
        self.imgs_path, self.labels = self.load_data(data_dir)
        self.img_size = img_size
        
    def load_data(self, data_dir: str):
        generated_img_paths = (Path(data_dir) / "generated_data").glob("*.jpg")
        train_img_paths = (Path(data_dir) / "train_data").glob("*.jpg")
        with open((Path(data_dir) / "generated_data/label.txt"), "r") as f:
            generated_img_label = f.read()
        with open((Path(data_dir) / "train_data/label.txt"), "r") as f:
            train_img_label = f.read()
        imgs = []
        labels = []
        for img in generated_img_paths:
            imgs.append(img)
            labels.append(generated_img_label)
        
        for img in train_img_paths:
            imgs.append(img)
            labels.append(train_img_label)
            
        return imgs, labels

    def get_image(self, index: int):
        img = Image.open(self.imgs_path[index])
        img = img.resize(self.img_size)
        img = np.array(img)
        img = torch.tensor(img, dtype=torch.float32)
        img = scale_img(img, (0, 255), (-1, 1))
        img = img.permute(2, 0, 1)
        return img

    def get_label(self, index: int):
        return self.labels[index]
        
    def __len__(self):
        return len(self.imgs_path)

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, str]:
        img = self.get_image(index)        
        label = self.get_label(index)
        return img, label
    
def create_dataloaders(data_dir, 
                       train_test_split: float,
                       batch_size: int, 
                       num_workers: int,
                      img_size: Tuple[int, int]):

    dataset = DreamboothDataset(data_dir, img_size=img_size)

    
    train_size = int(train_test_split * len(dataset))
    test_size = len(dataset) - train_size
    train_dataset, test_dataset = torch.utils.data.random_split(dataset, [train_size, test_size])

    train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=num_workers)
    test_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=False, num_workers=num_workers)

    return train_dataloader, test_dataloader
    
    
        
        