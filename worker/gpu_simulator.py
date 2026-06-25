import time

# PyTorch 라이브러리 임포트 시도 (에러 발생 시 더미 연산 Fallback을 사용하기 위함)
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

# --- 1. PyTorch 모델 정의 (RNN, CNN, LSTM) ---

if HAS_TORCH:
    class CNNModel(nn.Module):
        """간단한 이미지 분류용 합성곱 신경망 (CNN)"""
        def __init__(self):
            super().__init__()
            self.conv = nn.Conv2d(1, 16, kernel_size=3, padding=1)
            self.pool = nn.MaxPool2d(2, 2)
            self.fc = nn.Linear(16 * 14 * 14, 10)

        def forward(self, x):
            x = self.pool(torch.relu(self.conv(x)))
            x = x.view(-1, 16 * 14 * 14)
            return self.fc(x)

    class RNNModel(nn.Module):
        """간단한 시퀀스 분류용 순환 신경망 (RNN)"""
        def __init__(self):
            super().__init__()
            self.rnn = nn.RNN(input_size=10, hidden_size=20, batch_first=True)
            self.fc = nn.Linear(20, 10)

        def forward(self, x):
            out, _ = self.rnn(x)
            # 마지막 시점의 출력을 분류기에 입력
            return self.fc(out[:, -1, :])

    class LSTMModel(nn.Module):
        """간단한 시퀀스 분류용 장단기 메모리 신경망 (LSTM)"""
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(input_size=10, hidden_size=20, batch_first=True)
            self.fc = nn.Linear(20, 10)

        def forward(self, x):
            out, _ = self.lstm(x)
            # 마지막 시점의 출력을 분류기에 입력
            return self.fc(out[:, -1, :])


def run_pytorch_epoch(model_type, device):
    """각 모델 타입에 맞춰 1에포크 분량의 더미 연산을 구동하고 오차를 계산합니다."""
    if not HAS_TORCH:
        return 0.0

    if model_type.upper() == "CNN":
        model = CNNModel().to(device)
        optimizer = optim.SGD(model.parameters(), lr=0.01)
        criterion = nn.CrossEntropyLoss()
        
        # 연산 시간 확보를 위한 미니배치 루프
        loss_val = 0.0
        for _ in range(50):
            inputs = torch.randn(64, 1, 28, 28, device=device)
            targets = torch.randint(0, 10, (64,), device=device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            loss_val = loss.item()
        return loss_val

    elif model_type.upper() in ["RNN", "LSTM"]:
        if model_type.upper() == "RNN":
            model = RNNModel().to(device)
        else:
            model = LSTMModel().to(device)
            
        optimizer = optim.SGD(model.parameters(), lr=0.01)
        criterion = nn.CrossEntropyLoss()
        
        loss_val = 0.0
        for _ in range(50):
            inputs = torch.randn(64, 15, 10, device=device)  # sequence length=15, feature size=10
            targets = torch.randint(0, 10, (64,), device=device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            loss_val = loss.item()
        return loss_val
    else:
        # 알 수 없는 모델 타입의 경우 0.1초 지연 처리
        time.sleep(0.1)
        return 0.0


# --- 2. 이종 GPU 시뮬레이터 실행기 (PyTorch Task Runner) ---

class PyTorchTaskRunner:
    """실제 PyTorch 연산을 돌리면서 워커 성능 등급에 맞춰 연산 완료 시간을 제어하는 실행기"""
    def __init__(self, task_id, model_type, epochs, worker_type):
        self.task_id = task_id
        self.model_type = model_type
        self.epochs = epochs
        self.worker_type = worker_type
        
        # 노드 타입에 매핑되는 속도 성능 계수
        type_factors = {
            "on_demand": 1.0,
            "spot_a": 0.6,
            "spot_b": 0.3
        }
        
        self.speed_factor = type_factors.get(worker_type.lower(), 1.0)
        self.progress = 0.0
        self.status = "RUNNING"
        self.logs = []
        self.execution_time = 0.0

    def run(self):
        print(f"\n[Worker Task] 작업 시작: {self.task_id} (모델: {self.model_type}, 노드타입: {self.worker_type}, 속도배수: {self.speed_factor})")
        start_time = time.time()
        
        # 디바이스 결정 (CUDA 사용 가능 여부 확인)
        device = "cuda" if (HAS_TORCH and torch.cuda.is_available()) else "cpu"
        print(f"[Worker Task] 구동 디바이스: {device}")
        
        for epoch in range(self.epochs):
            epoch_start = time.time()
            
            # 1. PyTorch 연산 구동 (Fallback 예외 처리 포함)
            if HAS_TORCH:
                try:
                    loss = run_pytorch_epoch(self.model_type, device)
                except Exception as e:
                    print(f"[Worker Task] 연산 중 경고 발생 (Fallback 대체): {e}")
                    loss = 1.0 / (epoch + 1)
                    time.sleep(0.05)
            else:
                # PyTorch 라이브러리가 없을 때의 모사 연산
                loss = 1.0 / (epoch + 1)
                # CPU 연산 시간 모사를 위한 더미 루프
                dummy_sum = 0
                for x in range(200000):
                    dummy_sum += x
                time.sleep(0.05)
                
            # 2. GPU 비동기 연산 완료 동기화
            if HAS_TORCH and device == "cuda":
                torch.cuda.synchronize()
                
            actual_time = time.time() - epoch_start
            
            # 3. 속도 지연(시뮬레이션) 계산
            # 목표 소요 시간 = 실제 소요 시간 / 속도 계수
            target_time = actual_time / self.speed_factor
            delay = target_time - actual_time
            
            if delay > 0:
                time.sleep(delay)
                
            epoch_total_time = actual_time + max(0.0, delay)
            log_line = f"Epoch {epoch+1}/{self.epochs} - Loss: {loss:.4f} - 연산시간: {actual_time:.4f}초 (지연: {max(0.0, delay):.4f}초, 총 {epoch_total_time:.2f}초)"
            self.logs.append(log_line)
            print(f"[Worker Task] {self.task_id} | {log_line}")
            
            # 진행률 업데이트
            self.progress = ((epoch + 1) / self.epochs) * 100.0
            
        self.execution_time = time.time() - start_time
        self.status = "SUCCESS"
        print(f"[Worker Task] 작업 완료: {self.task_id} (총 소요 시간: {self.execution_time:.2f}초)\n")
