import os
import torch
import torch.nn as nn
import torchvision
import torchvision.transforms as transforms
from worker.models.base import BaseTask

# CNN 학습 하이퍼파라미터
CNN_BATCH_SIZE = 4
CNN_NUM_BATCHES = 3
CNN_IMAGE_SIZE = 28
CNN_NUM_CLASSES = 10

class CNNModel(nn.Module):
    """3-Layer Conv + BatchNorm + Dropout 기반 이미지 분류 합성곱 신경망 (CNN)"""
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 32, kernel_size=3, padding=1)
        self.bn1 = nn.BatchNorm2d(32)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=3, padding=1)
        self.bn2 = nn.BatchNorm2d(64)
        self.conv3 = nn.Conv2d(64, 128, kernel_size=3, padding=1)
        self.bn3 = nn.BatchNorm2d(128)
        self.pool = nn.MaxPool2d(2, 2)
        self.dropout = nn.Dropout(0.3)
        self.fc1 = nn.Linear(128 * 3 * 3, 128)
        self.fc2 = nn.Linear(128, CNN_NUM_CLASSES)

    def forward(self, x):
        x = self.pool(torch.relu(self.bn1(self.conv1(x))))
        x = self.pool(torch.relu(self.bn2(self.conv2(x))))
        x = self.pool(torch.relu(self.bn3(self.conv3(x))))
        x = x.view(-1, 128 * 3 * 3)
        x = self.dropout(x)
        x = torch.relu(self.fc1(x))
        return self.fc2(x)

class CNNTask(BaseTask):
    """CNN 이미지 분류 학습 및 추론 Task 클래스"""
    def __init__(self):
        super().__init__()

    def get_model(self):
        return CNNModel()

    def get_criterion(self):
        return nn.CrossEntropyLoss()

    def train_epoch(self, model, optimizer, criterion, device):
        data_loader = None
        try:
            transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
            train_dataset = torchvision.datasets.MNIST(root='./data_mnist', train=True, download=True, transform=transform)
            data_loader = torch.utils.data.DataLoader(train_dataset, batch_size=CNN_BATCH_SIZE, shuffle=True)
            data_iter = iter(data_loader)
        except Exception:
            data_iter = None

        loss_val = 0.0
        for _ in range(CNN_NUM_BATCHES):
            if data_iter is not None:
                try:
                    inputs, targets = next(data_iter)
                    inputs, targets = inputs.to(device), targets.to(device)
                except (StopIteration, Exception):
                    inputs = torch.randn(CNN_BATCH_SIZE, 1, CNN_IMAGE_SIZE, CNN_IMAGE_SIZE, device=device)
                    targets = torch.randint(0, CNN_NUM_CLASSES, (CNN_BATCH_SIZE,), device=device)
            else:
                inputs = torch.randn(CNN_BATCH_SIZE, 1, CNN_IMAGE_SIZE, CNN_IMAGE_SIZE, device=device)
                targets = torch.randint(0, CNN_NUM_CLASSES, (CNN_BATCH_SIZE,), device=device)

            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            loss_val = loss.item()
            
        return loss_val

    def infer(self, model, device):
        model.eval()
        try:
            with torch.no_grad():
                test_img = torch.randn(1, 1, CNN_IMAGE_SIZE, CNN_IMAGE_SIZE, device=device)
                out = model(test_img)
                prob = torch.softmax(out, dim=1)
                conf, pred = torch.max(prob, dim=1)
                return f"[CNN Inference Done] 이미지 분석 결과 -> 예측 클래스: {pred.item()} (신뢰도: {conf.item()*100:.2f}%)"
        except Exception as e:
            return f"[CNN Inference Error] {str(e)}"
