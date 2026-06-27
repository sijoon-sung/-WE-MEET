import time
import random

# 가상 OOM 시뮬레이션 플래그
oom_simulated = False 

# PyTorch 라이브러리 임포트 시도 (에러 발생 시 더미 연산 Fallback을 사용하기 위함)
try:
    import torch 
    import torch.nn as nn
    import torch.optim as optim     # 최적화 함수
    HAS_TORCH = True                # True로 설정하여 아래 코드에서 정상적으로 연산하도록 함
except ImportError:     
    HAS_TORCH = False               # False로 설정하여 아래 코드에서 더미 연산으로 넘어감


# --- 0. 부하 제어 하이퍼파라미터 ---

# CNN 학습 루프 설정
CNN_BATCH_SIZE = 128          # 미니배치 크기 (한 번에 처리할 이미지 128장)
CNN_NUM_BATCHES = 100         # 에포크당 미니배치 반복 횟수 (1에포크에 128 * 100 = 12,800장 처리)
CNN_IMAGE_SIZE = 28           # 입력 이미지 크기 (28x28 해상도, MNIST 데이터셋 규격)
CNN_NUM_CLASSES = 10          # 분류할 최종 정답의 개수 (예: 0~9 숫자 분류)

# RNN/LSTM 학습 루프 설정
SEQ_BATCH_SIZE = 128          # 시계열 데이터의 미니배치 크기
SEQ_NUM_BATCHES = 100         # 에포크당 반복 횟수
SEQ_LENGTH = 30               # 시퀀스 길이 (한 번에 입력되는 시간 흐름의 길이, 기존 15에서 30으로 증가)
SEQ_FEATURE_SIZE = 32         # 입력 피처 차원 (각 시점마다 입력되는 데이터의 특성 개수)
SEQ_HIDDEN_SIZE = 64          # 은닉 상태(Hidden State) 차원 크기 (메모리 역할)
SEQ_NUM_CLASSES = 10          # 분류 클래스 수

# LSTM Embedding 설정
LSTM_VOCAB_SIZE = 1000        # 어휘 사전 크기
LSTM_EMBED_DIM = 32           # 임베딩 벡터 차원

# Fallback 더미 연산 설정
FALLBACK_DUMMY_LOOP = 500000  # CPU 부하 모사 루프 횟수 (기존 200000 → 500000)
FALLBACK_SLEEP = 0.05         # Fallback 시 최소 대기 시간(초)


# --- 1. PyTorch 모델 정의 (CNN, RNN, LSTM) ---

if HAS_TORCH:
    class CNNModel(nn.Module):
        """3-Layer Conv + BatchNorm + Dropout 기반 이미지 분류 합성곱 신경망 (CNN)
        
        구조:
            Conv2d(1→32) → BatchNorm → ReLU → MaxPool(2×2)   [28×28 → 14×14]
            Conv2d(32→64) → BatchNorm → ReLU → MaxPool(2×2)  [14×14 → 7×7]
            Conv2d(64→128) → BatchNorm → ReLU → MaxPool(2×2) [7×7 → 3×3]
            Flatten → Dropout(0.3) → FC(128*3*3 → 128) → ReLU → FC(128 → 10)
        """
        def __init__(self):
            super().__init__()
            # 제1 합성곱 블록: 1채널 → 32채널
            self.conv1 = nn.Conv2d(1, 32, kernel_size=3, padding=1)
            self.bn1 = nn.BatchNorm2d(32)

            # 제2 합성곱 블록: 32채널 → 64채널
            self.conv2 = nn.Conv2d(32, 64, kernel_size=3, padding=1)
            self.bn2 = nn.BatchNorm2d(64)
            
            # 제3 합성곱 블록: 64채널 → 128채널
            self.conv3 = nn.Conv2d(64, 128, kernel_size=3, padding=1)
            self.bn3 = nn.BatchNorm2d(128)

            # MaxPool(2×2): 입력 크기를 절반으로 축소
            self.pool = nn.MaxPool2d(2, 2)
        
            # Dropout(0.3): 과적합 방지를 위해 30% 확률로 뉴런 비활성화
            self.dropout = nn.Dropout(0.3)
            
            # 28→14→7→3 (MaxPool 3회), 128채널 × 3 × 3 = 1152
            self.fc1 = nn.Linear(128 * 3 * 3, 128)
            self.fc2 = nn.Linear(128, CNN_NUM_CLASSES)

        def forward(self, x):
            # 블록 1: Conv → BN → ReLU → Pool  [B,1,28,28] → [B,32,14,14]
            x = self.pool(torch.relu(self.bn1(self.conv1(x))))
            
            # 블록 2: Conv → BN → ReLU → Pool  [B,32,14,14] → [B,64,7,7]
            x = self.pool(torch.relu(self.bn2(self.conv2(x))))
            
            # 블록 3: Conv → BN → ReLU → Pool  [B,64,7,7] → [B,128,3,3]
            x = self.pool(torch.relu(self.bn3(self.conv3(x))))
            
            # Flatten → Dropout → FC1 → ReLU → FC2
            x = x.view(-1, 128 * 3 * 3)
            x = self.dropout(x)
            x = torch.relu(self.fc1(x))
            return self.fc2(x)

    class RNNModel(nn.Module):
        """2-Layer Stacked RNN + Dropout 기반 시퀀스 분류 순환 신경망 (RNN)
        
        구조:
            nn.RNN(input=32, hidden=64, num_layers=2, dropout=0.3)
            → 마지막 시점 출력 → FC(64 → 10)
        """
        def __init__(self):
            super().__init__()
            self.rnn = nn.RNN(
                input_size=SEQ_FEATURE_SIZE,
                hidden_size=SEQ_HIDDEN_SIZE,
                num_layers=2,          # 2층 스택 RNN
                batch_first=True,
                dropout=0.3            # 다층 RNN 간 드롭아웃
            )
            self.fc = nn.Linear(SEQ_HIDDEN_SIZE, SEQ_NUM_CLASSES)

        def forward(self, x):
            out, _ = self.rnn(x)
            # 마지막 시점의 출력을 분류기에 입력
            return self.fc(out[:, -1, :])

    class LSTMModel(nn.Module):
        """2-Layer Stacked LSTM + Embedding 기반 시퀀스 분류 장단기 메모리 신경망 (LSTM)
        
        구조:
            nn.Embedding(vocab=1000, dim=32)
            → nn.LSTM(input=32, hidden=64, num_layers=2, dropout=0.3)
            → 마지막 시점 출력 → FC(64 → 10)
        """
        def __init__(self):
            super().__init__()
            # Embedding 레이어: 정수 토큰 → 연속 벡터 변환
            self.embedding = nn.Embedding(LSTM_VOCAB_SIZE, LSTM_EMBED_DIM)
            self.lstm = nn.LSTM(
                input_size=LSTM_EMBED_DIM,
                hidden_size=SEQ_HIDDEN_SIZE,
                num_layers=2,          # 2층 스택 LSTM
                batch_first=True,
                dropout=0.3            # 다층 LSTM 간 드롭아웃
            )
            self.fc = nn.Linear(SEQ_HIDDEN_SIZE, SEQ_NUM_CLASSES)

        def forward(self, x):
            # x: (batch, seq_len) 정수 인덱스 → Embedding → (batch, seq_len, embed_dim)
            x = self.embedding(x)
            out, _ = self.lstm(x)
            # 마지막 시점의 출력을 분류기에 입력
            return self.fc(out[:, -1, :])


"""
optimizer.zero_grad()            # 1. 기울기 초기화
outputs = model(inputs)          # 2. 순전파 (Forward Pass)
loss = criterion(outputs, targets) # 3. 손실(Loss) 계산
loss.backward()                  # 4. 역전파 (Backward Pass - 기울기 계산)
optimizer.step()                 # 5. 가중치 업데이트
"""

def run_pytorch_epoch(model_type, device):
    """각 모델 타입에 맞춰 1에포크 분량의 부하 연산을 구동하고 오차를 계산합니다."""
    if not HAS_TORCH:
        return 0.0

    if model_type.upper() == "CNN": #CNN인 경우
        model = CNNModel().to(device)
        optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
        criterion = nn.CrossEntropyLoss()
        
        # 연산 부하 확보를 위한 미니배치 루프
        loss_val = 0.0
        for _ in range(CNN_NUM_BATCHES):
            # Tensor Shape: [Batch, Channel, Height, Width] 형태의 4D 텐서.
            inputs = torch.randn(CNN_BATCH_SIZE, 1, CNN_IMAGE_SIZE, CNN_IMAGE_SIZE, device=device)
            targets = torch.randint(0, CNN_NUM_CLASSES, (CNN_BATCH_SIZE,), device=device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets) #임의의 target으로 오차계산으로 실제 연산과 유사하게 만듦.
            loss.backward()
            optimizer.step()
            loss_val = loss.item()
        return loss_val

    elif model_type.upper() == "RNN":
        model = RNNModel().to(device)
        optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
        criterion = nn.CrossEntropyLoss()
        
        loss_val = 0.0
        for _ in range(SEQ_NUM_BATCHES):
            # Tensor Shape: [Batch, Sequence Length, Feature Size] 형태의 3D 텐서.
            # 각 시점(Time Step)마다 SEQ_FEATURE_SIZE개의 연속형 피처를 가지는 길이 SEQ_LENGTH의 시계열 데이터를 모사한다.
            inputs = torch.randn(SEQ_BATCH_SIZE, SEQ_LENGTH, SEQ_FEATURE_SIZE, device=device)
            targets = torch.randint(0, SEQ_NUM_CLASSES, (SEQ_BATCH_SIZE,), device=device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            loss_val = loss.item()
        return loss_val

    elif model_type.upper() == "LSTM":
        model = LSTMModel().to(device)
        optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
        criterion = nn.CrossEntropyLoss()
        
        loss_val = 0.0
        for _ in range(SEQ_NUM_BATCHES):
            # LSTM은 Embedding 레이어를 사용하므로 정수 인덱스 입력 생성
            # Tensor Shape: [Batch, Sequence Length] 형태의 2D 텐서.
            inputs = torch.randint(0, LSTM_VOCAB_SIZE, (SEQ_BATCH_SIZE, SEQ_LENGTH), device=device)
            targets = torch.randint(0, SEQ_NUM_CLASSES, (SEQ_BATCH_SIZE,), device=device)
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
        
# 작업 아이디, 모델 종류, 반복 에포크 수, 워커 노드의 종류를 인자로 받아 초기화
        self.task_id = task_id
        self.model_type = model_type
        self.epochs = epochs
        self.worker_type = worker_type
        
        # 노드 타입에 매핑되는 속도 성능 계수 ===========> (필요가 있나)
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
        start_time = time.time() # 전체 작업 시작 시간
        
        # 가상 OOM 장애 유발 로직
        is_oom_trigger = False
        # OOM -> 1.0 실험
        if self.model_type.upper() == "LSTM" and random.random() < 1.0:
            is_oom_trigger = True

        elif "fail" in self.task_id.lower():
            is_oom_trigger = True
            
        if is_oom_trigger:
            print(f"\n[Worker Simulation] !!! 가상 OOM 장애 유입 감지 !!! (Task: {self.task_id})")
            global oom_simulated
            oom_simulated = True # 전역 OOM 상태를 True로 변경
            self.status = "FAILED"
            self.logs.append("OOM Exception simulated: cGroup memory limit exceeded.")
            
            print("[Worker Simulation] cGroup 메모리 제한 초과로 강제 실패 처리 완료.")
            return

        # 디바이스 결정 (CUDA 사용 가능 여부 확인) - GPU/CPU 판단
        device = "cuda" if (HAS_TORCH and torch.cuda.is_available()) else "cpu"
        print(f"[Worker Task] 구동 디바이스: {device}")
        
        # Epoch 반복
        for epoch in range(self.epochs):
            epoch_start = time.time()
            
            # 1. PyTorch 연산 구동 (Fallback 예외 처리 포함)
            if HAS_TORCH:
                try:
                    # 실제 연산 수행
                    loss = run_pytorch_epoch(self.model_type, device)
                except Exception as e:
                    print(f"[Worker Task] 연산 중 경고 발생 (Fallback 대체): {e}")
                    loss = 1.0 / (epoch + 1)
                    time.sleep(FALLBACK_SLEEP)
            else:
                # PyTorch 라이브러리가 없을 때의 모사 연산 -> CPU 부하
                loss = 1.0 / (epoch + 1)
                # CPU 연산 시간 모사를 위한 더미 루프
                dummy_sum = 0
                for x in range(FALLBACK_DUMMY_LOOP):
                    dummy_sum += x
                time.sleep(FALLBACK_SLEEP)
                
            # 2. GPU 비동기 연산 완료 동기화
            if HAS_TORCH and device == "cuda":
                torch.cuda.synchronize()
                
            actual_time = time.time() - epoch_start # 1epoch당 시간
            
            # 3. 속도 지연(시뮬레이션) 계산
            # 목표 소요 시간 = 실제 소요 시간 / 속도 계수
            # 예: 0.5초가 걸렸고 spot_b(0.3 성능)라면, 목표 시간은 0.5 / 0.3 = 약 1.66초
            target_time = actual_time / self.speed_factor
            delay = target_time - actual_time
            
            if delay > 0:
                time.sleep(delay) #계산된 시간만큼 멈춰서 성능 저하 시뮬레이션
                
            epoch_total_time = actual_time + max(0.0, delay) #실제 연산시간 + 시뮬레이션 지연시간

            # 로그 기록 및 진행률 갱신
            log_line = f"Epoch {epoch+1}/{self.epochs} - Loss: {loss:.4f} - 연산시간: {actual_time:.4f}초 (지연: {max(0.0, delay):.4f}초, 총 {epoch_total_time:.2f}초)"
            self.logs.append(log_line)
            print(f"[Worker Task] {self.task_id} | {log_line}")
            
            # 진행률 업데이트
            self.progress = ((epoch + 1) / self.epochs) * 100.0

        # 모든 epoch가 끝나면    
        self.execution_time = time.time() - start_time
        self.status = "SUCCESS"
        print(f"[Worker Task] 작업 완료: {self.task_id} (총 소요 시간: {self.execution_time:.2f}초)\n")
