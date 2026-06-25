# Worker 노드 기술 명세서: 이종 GPU 성능 시뮬레이터 & PyTorch 모델 연동

이 문서는 Worker 노드에 추가된 **이종 GPU 성능 시뮬레이터** 및 **PyTorch 신경망 모델(CNN, RNN, LSTM)** 연동 구조와 구현 내역에 대한 기술 명세서입니다.

---

## 1. 개요 및 추가 목적
분산 클라우드 환경에서 다양한 GPU 사양(이종 GPU) 및 요금제 모델(On-Demand, Spot 인스턴스)에 따른 실질적인 학습 속도 편차를 모사하기 위함입니다. 이를 통해 실제 비싼 다수의 물리 GPU 장비 없이도 복수의 워커 성능 격차에 따른 스케줄링 알고리즘 및 로드 밸런싱의 동작 유효성을 로컬 개발 환경에서 검증할 수 있습니다.

---

## 2. 추가된 주요 구성 요소 및 구조
모든 PyTorch 연산 및 시뮬레이터 핵심 로직은 모듈화를 위해 `worker/gpu_simulator.py`에 격리되어 있습니다.

```
[Head Node] (작업 지시) 
    │
    ▼ (gRPC: AssignTask)
[Worker Node (worker.py)]
    │ 
    └───► [GPU Simulator (gpu_simulator.py)] 
               │
               ├── PyTorch 학습 1Epoch 연산 (CNN/RNN/LSTM)
               │     └── CUDA 동기화 (torch.cuda.synchronize)
               │
               └── 이종 GPU 속도 시뮬레이션 지연 알고리즘 (time.sleep)
```

### ① PyTorch 기반 신경망 모델 (RNN, CNN, LSTM)
실제 텐서 연산을 통해 CPU/GPU 부하를 발생시킬 수 있는 신경망 구조입니다.
* **CNNModel**: `nn.Conv2d` 합성곱망과 `nn.Linear` 완전연결망을 사용한 2D 이미지 분류 모사 모델
* **RNNModel**: `nn.RNN`과 시퀀스 출력을 다루는 다층 순환 신경망 모델
* **LSTMModel**: `nn.LSTM`과 장단기메모리 게이트 연산을 모사하는 시퀀스 모델
* **더미 배치 루프**: 1에포크당 미니배치 50회 크기의 임의 텐서 순전파(Forward), 역전파(Backward), 최적화(Optimizer Step)를 구동하여 유의미한 연산 소요 시간을 생성합니다.

### ② 이종 GPU 성능 시뮬레이션 제어 알고리즘
각 워커의 등급(`worker_type`)에 따라 정의된 **성능 계수(Speed Factor)**를 바탕으로 학습 소요 시간을 인위적으로 지연시킵니다.
* **성능 계수 정의**:
  * `on_demand`: $1.0$ (지연 없음, 100% 성능)
  * `spot_a`: $0.6$ (지연 발생, 60% 성능)
  * `spot_b`: $0.3$ (지연 발생, 30% 성능)

* **동기화 및 딜레이 계산 공식**:
  1. 에포크 연산 종료 직후 `torch.cuda.synchronize()`를 호출하여 GPU 비동기 연산의 최종 완료를 보장하고 시간 측정을 동기화합니다.
  2. 실제 물리 연산 시간($T_{\text{actual}}$)을 계산합니다:
     $$T_{\text{actual}} = \text{Epoch 종료 시각} - \text{Epoch 시작 시각}$$
  3. 등급별 속도 계수($F_{\text{speed}}$)에 맞춘 목표 총 시간($T_{\text{target}}$)을 구합니다:
     $$T_{\text{target}} = \frac{T_{\text{actual}}}{F_{\text{speed}}}$$
  4. 인위적으로 주입해야 하는 지연 시간($D_{\text{delay}}$)을 산출하여 스레드를 일시 정지시킵니다:
     $$D_{\text{delay}} = T_{\text{target}} - T_{\text{actual}} = T_{\text{actual}} \times \left(\frac{1}{F_{\text{speed}}} - 1\right)$$
     *(단, $D_{\text{delay}} > 0$ 인 경우에만 `time.sleep(D_{\text{delay}})` 수행)*

### ③ 의존성 Fallback 예외 처리
개발 환경에 PyTorch 라이브러리가 존재하지 않거나 GPU 드라이버 DLL 바인딩 오류(예: c10.dll 로드 에러 등)가 발생할 경우를 대비하여 **Fallback 안전 모드**를 구축했습니다.
* `ImportError` 발생 시 `HAS_TORCH = False`로 설정됩니다.
* 이 모드에서는 CPU 연산 루프 및 기본 sleep 기능으로 모사 연산을 대체 수행하여 gRPC 통신이 비정상 중단되는 현상을 원천 방지합니다.

---

## 3. 리팩토링 효과
* **단일 책임 원칙 (SRP) 준수**: `worker.py`는 gRPC 통신 채널 제어 및 스케줄러와의 하트비트 교환 역할에만 집중하며, 실제 연산 및 속도 제어 로직은 `gpu_simulator.py`가 완전히 전담하도록 구조적 분리를 완료했습니다.
* **코드 가독성 향상**: `worker.py`가 약 350줄에서 약 180줄로 50% 수준으로 경량화되어 유지보수성이 극대화되었습니다.
