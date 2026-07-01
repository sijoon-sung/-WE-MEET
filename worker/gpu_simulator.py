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

# CNN 학습 루프 설정 (연산량 집중)
CNN_BATCH_SIZE = 64          # 미니배치 크기 (메모리 오버헤드 감소를 위해 128에서 64로 축소)
CNN_NUM_BATCHES = 100         # 에포크당 반복 횟수 (CPU 연산량 대폭 상승 제어를 위해 250에서 100으로 조정)
CNN_IMAGE_SIZE = 28           # 입력 이미지 크기
CNN_NUM_CLASSES = 10          # 분류 클래스 수

# RNN/LSTM 학습 루프 설정
SEQ_BATCH_SIZE = 64          # 시계열 데이터의 미니배치 크기 (128에서 64로 축소)
SEQ_NUM_BATCHES = 50         # 에포크당 반복 횟수 (100에서 50으로 축소)
SEQ_LENGTH = 30               # 시퀀스 길이
SEQ_FEATURE_SIZE = 32         # 입력 피처 차원
RNN_HIDDEN_SIZE = 64          # RNN 은닉 차원 (경량형)
LSTM_HIDDEN_SIZE = 128        # LSTM 은닉 차원 (메모리 사용 유도, 256에서 128로 축소)
SEQ_NUM_CLASSES = 10          # 분류 클래스 수

# LSTM Embedding 설정 (메모리 사용량 상승 유도)
LSTM_VOCAB_SIZE = 2000        # 어휘 사전 크기 확장
LSTM_EMBED_DIM = 128          # 임베딩 벡터 차원 대폭 확장 (메모리 할당 증가, 256에서 128로 축소)

# Fallback 더미 연산 설정
FALLBACK_SLEEP = 0.05         # Fallback 시 최소 대기 시간(초)

# 가상 메모리 홀더 (더미 메모리 점유 시뮬레이션용)
dummy_memory_holder = []



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
                hidden_size=RNN_HIDDEN_SIZE,
                num_layers=2,          # 2층 스택 RNN
                batch_first=True,
                dropout=0.3            # 다층 RNN 간 드롭아웃
            )
            self.fc = nn.Linear(RNN_HIDDEN_SIZE, SEQ_NUM_CLASSES)

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
                hidden_size=LSTM_HIDDEN_SIZE,
                num_layers=2,          # 2층 스택 LSTM
                batch_first=True,
                dropout=0.3            # 다층 LSTM 간 드롭아웃
            )
            self.fc = nn.Linear(LSTM_HIDDEN_SIZE, SEQ_NUM_CLASSES)

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

def train_one_epoch(model, optimizer, criterion, model_type, device):
    """
    모델 인스턴스와 옵티마이저를 전달받아 1에포크 분량의 PyTorch 학습 연산을 구동하고 최종 배치 Loss를 반환합니다.
    """
    if not HAS_TORCH:
        return 0.0

    model.train()
    loss_val = 0.0

    if model_type.upper() == "CNN":
        for _ in range(CNN_NUM_BATCHES):
            inputs = torch.randn(CNN_BATCH_SIZE, 1, CNN_IMAGE_SIZE, CNN_IMAGE_SIZE, device=device)
            targets = torch.randint(0, CNN_NUM_CLASSES, (CNN_BATCH_SIZE,), device=device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            loss_val = loss.item()

    elif model_type.upper() == "RNN":
        for _ in range(SEQ_NUM_BATCHES):
            inputs = torch.randn(SEQ_BATCH_SIZE, SEQ_LENGTH, SEQ_FEATURE_SIZE, device=device)
            targets = torch.randint(0, SEQ_NUM_CLASSES, (SEQ_BATCH_SIZE,), device=device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            loss_val = loss.item()

    elif model_type.upper() == "LSTM":
        for _ in range(SEQ_NUM_BATCHES):
            inputs = torch.randint(0, LSTM_VOCAB_SIZE, (SEQ_BATCH_SIZE, SEQ_LENGTH), device=device)
            targets = torch.randint(0, SEQ_NUM_CLASSES, (SEQ_BATCH_SIZE,), device=device)
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            loss_val = loss.item()

    return loss_val


def run_dummy_epoch(model_type):
    """
    PyTorch가 없는 환경 또는 에러 발생 시, 
    모델 종류별로 차별화된 CPU 및 메모리 부하를 모사합니다.

    Args:
        model_type (str): 신경망 모델 유형 ("CNN" / "RNN" / "LSTM").

    Returns:
        float: 모사 손실 값 (0.0 ~ 1.0 사이의 float).
    """
    global dummy_memory_holder
    model_upper = model_type.upper()
    
    if model_upper == "CNN":
        # CNN: 연산 집중형 (높은 CPU 점유율 유도)
        cpu_loop_count = 1500000 
        dummy_sum = 0.0
        for i in range(cpu_loop_count):
            dummy_sum += (i * 0.0001) ** 0.5
        
        dummy_memory_holder = [] # 메모리는 거의 사용하지 않음
        time.sleep(0.02)
        return dummy_sum % 1.0

    elif model_upper == "LSTM":
        # LSTM: 메모리 점유형 (높은 메모리 사용률 유도)
        # 1GB 컨테이너 기준 약 12% (120MB) 임시 점유로 축소 조정 (OOM 방지)
        # float(8 bytes) * 15,000,000 = 약 120MB
        mem_element_count = 15000000 
        dummy_memory_holder = [0.123] * mem_element_count 
        
        cpu_loop_count = 50000
        dummy_sum = 0.0
        for i in range(cpu_loop_count):
            dummy_sum += i
            
        time.sleep(0.08)
        return dummy_sum % 1.0

    else:
        # RNN 및 기본: 균형 잡힌 가벼운 부하
        cpu_loop_count = 400000
        dummy_sum = 0.0
        for i in range(cpu_loop_count):
            dummy_sum += i
        
        # 가벼운 메모리 점유 (약 16MB로 축소)
        dummy_memory_holder = [0.456] * 2000000
        time.sleep(0.05)
        return dummy_sum % 1.0


# --- 2. 이종 GPU 시뮬레이터 실행기 (PyTorch Task Runner) ---

class PyTorchTaskRunner:
    """
    실제 PyTorch 연산을 돌리면서 워커 성능 등급에 맞춰 연산 완료 시간을 제어하는 실행기 클래스입니다.

    Attributes:
        task_id (str): 실행할 태스크 고유 ID.
        model_type (str): 신경망 모델 유형 ("CNN" / "RNN" / "LSTM").
        epochs (int): 학습 Epoch 횟수.
        worker_type (str): 워커 유형 ("on_demand" / "spot_a").
        speed_factor (float): 성능 등급별 속도 계수.
        progress (float): 작업 진행률 (0.0 ~ 100.0 %).
        status (str): 작업 실행 상태 ("RUNNING" / "SUCCESS" / "FAILED").
        logs (list): 연산 진행 로그 목록.
        execution_time (float): 총 소요 수행 시간.
    """
    def __init__(self, task_id, model_type, epochs, worker_type, dataset_path=""):
        """
        PyTorchTaskRunner 인스턴스를 초기화합니다.
        """
        self.task_id = task_id
        self.model_type = model_type
        self.epochs = epochs
        self.worker_type = worker_type
        self.dataset_path = dataset_path
        
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
        """
        지정된 AI 모델 학습 연산(또는 모사 연산)을 수행합니다.
        가상 OOM 장애 유발 시나리오 및 성능 차별화 지연(Sleep)을 시뮬레이션합니다.
        """
        print(f"\n[Worker Task] 작업 시작: {self.task_id} (모델: {self.model_type}, 노드타입: {self.worker_type}, 속도배수: {self.speed_factor})")
        start_time = time.time()
        
        is_oom_trigger = False
        if self.model_type.upper() == "LSTM" and random.random() < 0.08:
            is_oom_trigger = True
        elif "fail" in self.task_id.lower():
            is_oom_trigger = True
            
        if is_oom_trigger:
            print(f"\n[Worker Simulation] !!! 가상 OOM 장애 유입 감지 !!! (Task: {self.task_id})")
            global oom_simulated
            oom_simulated = True
            self.status = "FAILED"
            self.logs.append("OOM Exception simulated: cGroup memory limit exceeded.")
            print("[Worker Simulation] cGroup 메모리 제한 초과로 강제 실패 처리 완료.")
            return

        # 1. FedAvg 결합 연산(Merge) 분기 처리
        if HAS_TORCH and self.dataset_path.startswith("merge:"):
            print(f"[Worker Task] 가중치 FedAvg 병합 연산 수행: {self.task_id}")
            try:
                paths_str = self.dataset_path.split("merge:")[1]
                file_paths = [p.strip() for p in paths_str.split(",") if p.strip()]
                
                state_dicts = [torch.load(p, map_location="cpu") for p in file_paths]
                averaged_sd = {}
                for key in state_dicts[0].keys():
                    tensors = [sd[key] for sd in state_dicts]
                    if tensors[0].dtype in [torch.float16, torch.float32, torch.float64, torch.bfloat16]:
                        averaged_sd[key] = torch.stack(tensors).mean(dim=0)
                    else:
                        averaged_sd[key] = tensors[0]
                
                os.makedirs("data", exist_ok=True)
                output_path = f"data/final_{self.task_id}.pt"
                torch.save(averaged_sd, output_path)
                
                self.execution_time = time.time() - start_time
                self.status = "SUCCESS"
                self.progress = 100.0
                self.logs.append(f"Federated Averaging 병합 완료. 출력 파일: {output_path}")
                print(f"[Worker Task] 가중치 FedAvg 병합 성공! 파일: {output_path}")
                return
            except Exception as e:
                self.status = "FAILED"
                self.logs.append(f"Federated Averaging 병합 에러: {str(e)}")
                print(f"[Worker Task] FedAvg 병합 실패: {e}")
                return

        # 2. 일반 모델 학습 및 이어서 학습 (Checkpoint Load)
        device = "cuda" if (HAS_TORCH and torch.cuda.is_available()) else "cpu"
        print(f"[Worker Task] 구동 디바이스: {device}")

        # [Safety Guard: 물리 VRAM 할당 격리 제한 적용]
        if HAS_TORCH and device == "cuda":
            try:
                total_memory = torch.cuda.get_device_properties(0).total_memory
                # 노드 타입별 가용 VRAM 제한 정의 (on_demand: 4.0GB, spot_a: 2.0GB, spot_b: 1.0GB)
                vram_limits = {
                    "on_demand": 4096 * 1024 * 1024,
                    "spot_a": 2048 * 1024 * 1024,
                    "spot_b": 1024 * 1024 * 1024
                }
                limit_bytes = vram_limits.get(self.worker_type.lower(), 1024 * 1024 * 1024)
                fraction = min(1.0, limit_bytes / total_memory)
                
                # PyTorch 프로세스별 CUDA 메모리 프랙션 가드 설정
                torch.cuda.set_per_process_memory_fraction(fraction, 0)
                print(f"[GPU Guard] 물리 VRAM 제한 적용: {limit_bytes / (1024**2):.1f} MB (비율: {fraction:.4f})")
                self.logs.append(f"[GPU Guard] VRAM Limit applied: {limit_bytes / (1024**2):.1f} MB")
            except Exception as e:
                print(f"[GPU Guard 경고] VRAM 분할 격리 설정 실패: {e}")
                self.logs.append(f"[GPU Guard Warning] VRAM partition failed: {str(e)}")

        model = None
        optimizer = None
        criterion = None
        
        if HAS_TORCH:
            try:
                if self.model_type.upper() == "CNN":
                    model = CNNModel().to(device)
                elif self.model_type.upper() == "RNN":
                    model = RNNModel().to(device)
                elif self.model_type.upper() == "LSTM":
                    model = LSTMModel().to(device)
                
                if model:
                    optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
                    criterion = nn.CrossEntropyLoss()
                    
                    if self.dataset_path and self.dataset_path.endswith(".pt") and os.path.exists(self.dataset_path):
                        print(f"[Worker Task] 이전 체크포인트 로드: {self.dataset_path}")
                        model.load_state_dict(torch.load(self.dataset_path, map_location=device))
                        self.logs.append(f"Loaded checkpoint state_dict from {self.dataset_path}")
            except Exception as e:
                print(f"[Worker Task] 모델 초기화 또는 체크포인트 로딩 중 에러: {e}")
                self.logs.append(f"Model init / checkpoint load failed: {str(e)}")

        # Epoch 루프
        for epoch in range(self.epochs):
            epoch_start = time.time()
            loss = 0.0
            
            if HAS_TORCH and model is not None:
                try:
                    loss = train_one_epoch(model, optimizer, criterion, self.model_type, device)
                    os.makedirs("data", exist_ok=True)
                    checkpoint_path = f"data/checkpoint_{self.task_id}_epoch_{epoch+1}.pt"
                    torch.save(model.state_dict(), checkpoint_path)
                except Exception as e:
                    print(f"[Worker Task] PyTorch 학습 연산 중 경고 발생 (Fallback 대체): {e}")
                    loss = run_dummy_epoch(self.model_type)
            else:
                loss = run_dummy_epoch(self.model_type)

            if HAS_TORCH and device == "cuda":
                torch.cuda.synchronize()

            actual_time = time.time() - epoch_start
            target_time = actual_time / self.speed_factor
            delay = target_time - actual_time
            if delay > 0:
                time.sleep(delay)

            epoch_total_time = actual_time + max(0.0, delay)
            log_line = f"Epoch {epoch+1}/{self.epochs} - Loss: {loss:.4f} - 연산시간: {actual_time:.4f}초 (지연: {max(0.0, delay):.4f}초, 총 {epoch_total_time:.2f}초)"
            self.logs.append(log_line)
            print(f"[Worker Task] {self.task_id} | {log_line}")
            self.progress = ((epoch + 1) / self.epochs) * 100.0

        if HAS_TORCH and model is not None and self.status != "FAILED":
            try:
                os.makedirs("data", exist_ok=True)
                final_path = f"data/final_{self.task_id}.pt"
                torch.save(model.state_dict(), final_path)
                print(f"[Worker Task] 최종 모델 가중치 저장 성공: {final_path}")
                self.logs.append(f"Saved final weights to {final_path}")
            except Exception as e:
                print(f"[Worker Task] 최종 가중치 저장 실패: {e}")

        self.execution_time = time.time() - start_time
        if self.status != "FAILED":
            self.status = "SUCCESS"
        
        # 메모리 시뮬레이션 공간 해제
        global dummy_memory_holder
        dummy_memory_holder = []
        
        print(f"[Worker Task] 작업 완료: {self.task_id} (총 소요 시간: {self.execution_time:.2f}초)\n")
