import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import time
import logging

logging.basicConfig(level=logging.INFO, format="[WORKER-EXECUTOR] %(asctime)s - %(levelname)s - %(message)s")

class SimpleLSTM(nn.Module):
    def __init__(self, input_dim=1, hidden_dim=64, output_dim=1, num_layers=1):
        super(SimpleLSTM, self).__init__()
        self.lstm = nn.LSTM(input_dim, hidden_dim, num_layers, batch_first=True)
        self.linear = nn.Linear(hidden_dim, output_dim)

    def forward(self, x):
        out, _ = self.lstm(x)
        out = self.linear(out[:, -1, :])
        return out

def run_lstm_training(task_id, sequence_length, hidden_dim, epochs, learning_rate, thread_limit, gpu_scale_factor, status_callback=None):
    """
    PyTorch LSTM 학습 워크로드를 구동하고 가상 GPU 스케일러 및 스레드 제한을 적용하는 함수
    """
    # 1. CPU 연산 스레드 제한 설정 (cGroup CPU 제한 연계)
    torch.set_num_threads(thread_limit)
    logging.info(f"Set CPU thread limit to {thread_limit} threads")

    # 2. 연산 디바이스 선택 (GPU CUDA 가속 활성화)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logging.info(f"Using execution device: {device}")

    # 3. 모델 및 옵티마이저 초기화
    model = SimpleLSTM(input_dim=1, hidden_dim=hidden_dim, output_dim=1).to(device)
    criterion = nn.MSELoss()
    optimizer = optim.Adam(model.parameters(), lr=learning_rate)

    # 4. 합성 데이터셋(Synthetic Sine Wave) 생성
    # 의도적으로 노드 내부 메모리에서 즉석 생성하여 외부 네트워크 지연을 배제
    x_data = np.sin(np.linspace(0, 100, 1000))
    inputs = []
    targets = []
    for i in range(len(x_data) - sequence_length - 1):
        inputs.append(x_data[i:i+sequence_length])
        targets.append(x_data[i+sequence_length])
        
    inputs = torch.tensor(np.array(inputs), dtype=torch.float32).unsqueeze(-1)
    targets = torch.tensor(np.array(targets), dtype=torch.float32).unsqueeze(-1)

    # 5. 미니배치 분할
    dataset = torch.utils.data.TensorDataset(inputs, targets)
    loader = torch.utils.data.DataLoader(dataset, batch_size=64, shuffle=True)

    logging.info(f"Started training loop for task '{task_id}': epochs={epochs}, scale_factor={gpu_scale_factor}")

    # 6. 학습 루프 (GPU 이기종 모사 포함)
    for epoch in range(epochs):
        epoch_start_time = time.time()
        running_loss = 0.0
        
        for batch_inputs, batch_targets in loader:
            step_start_time = time.time()
            
            # 데이터를 연산 디바이스로 이동
            batch_inputs = batch_inputs.to(device)
            batch_targets = batch_targets.to(device)
            
            optimizer.zero_grad()
            outputs = model(batch_inputs)
            loss = criterion(outputs, batch_targets)
            loss.backward()
            optimizer.step()
            
            running_loss += loss.item()

            # GPU 연산 대기가 필요한 경우 동기화 수행
            if device.type == "cuda":
                torch.cuda.synchronize()

            step_elapsed = time.time() - step_start_time
            
            # 가상 GPU 성능 Throttling (RTX 4060 Laptop GPU의 성능 비대칭 편차 시뮬레이션)
            if gpu_scale_factor < 1.0 and gpu_scale_factor > 0:
                # scale_factor가 0.3이면, 실행 속도가 30% 수준으로 떨어지므로
                # 총 소요 시간은 elapsed / 0.3이 되어야 함. 따라서 sleep은 elapsed * (1/0.3 - 1)
                delay = step_elapsed * (1.0 / gpu_scale_factor - 1.0)
                if delay > 0:
                    time.sleep(delay)

        epoch_loss = running_loss / len(loader)
        progress = (epoch + 1) / epochs
        epoch_elapsed = time.time() - epoch_start_time
        
        logging.info(f"Epoch {epoch+1}/{epochs} - Loss: {epoch_loss:.6f} - Time: {epoch_elapsed:.3f}s")
        
        # Head에 상태 보고를 위한 콜백 호출
        if status_callback:
            status_callback(progress, epoch_loss)

    logging.info(f"Completed LSTM training for task '{task_id}'")
    return model
