# Worker Node 기술 명세서: gRPC 통신 서버 및 하트비트 송신 메커니즘

이 문서는 Baby Ray 분산 컴퓨팅 시스템의 작업 수행 주체인 **Worker Node**의 아키텍처, gRPC 서비스 서버 사양, 그리고 Head Node와의 통신 프로토콜을 정의한 기술 명세서입니다.

---

## 1. 개요 및 역할

Worker Node는 Head Node의 제어 명령을 수신하여 실제 머신러닝 연산(PyTorch 또는 Fallback 더미 연산)을 백그라운드 스레드에서 구동하고, 자신의 상태 및 자원 사용량 메트릭을 주기적으로 Head Node에 보고하는 역할을 담당합니다.

```
┌────────────────────────────────────────────────────────┐
│                      Worker Node                       │
│  ┌───────────────────────┐    ┌─────────────────────┐  │
│  │     gRPC 서비스 서버  │    │   하트비트 송신기   │  │
│  │    (worker_server)    │    │ (heartbeat_sender)  │  │
│  └───────────────────────┘    └─────────────────────┘  │
│              ▲                           │             │
│              │ (AssignTask)              │ (Send       │
│              │                           │  Heartbeat) │
│              ▼                           ▼             │
│   ┌─────────────────────┐                               │
│   │  PyTorchTaskRunner  │                               │
│   │ (gpu_simulator.py)  │                               │
│   └─────────────────────┘                               │
└────────────────────────────────────────────────────────┘
```

---

## 2. 주요 구성 컴포넌트

Worker Node는 구동 시 메인 스레드와 백그라운드 스레드가 결합하여 독립적인 두 영역의 통신 채널을 가집니다.

1. **gRPC 서비스 수신 서버 (Server)**: Head Node가 전달하는 작업 지시(`AssignTask`) 및 모니터링 폴링(`GetTaskStatus`) 요청을 수신 및 처리합니다.
2. **하트비트 송신 클라이언트 (Client)**: 1.0초 주기로 자신의 자원 메트릭과 상태를 헤드 노드의 GCS에 지속해서 신고합니다.

---

## 3. Worker Node gRPC 서비스 핸들러 명세

Worker Node는 내부적으로 `BabyRayServiceServicer` 프로토콜을 상속받아 서버 측 기능을 처리합니다.

### ① `AssignTask(self, request, context)`
- **역할**: 신규 머신러닝 연산 요청을 접수받아 유효성을 검사한 뒤 백그라운드 스레드에서 시뮬레이터를 가동합니다.
- **매개변수 (`request`)**: `TaskAssignment` 프로토콜 버퍼 메시지
- **응답 (`TaskResult`)**:
  - `task_id` (str): 작업 고유 ID
  - `status` (str): 즉시 반환될 승인 상태 (`RUNNING` 또는 `FAILED`)
  - `execution_time` (float): 초기 리턴값 `0.0`
  - `message` (str): 접수 처리 세부 정보
- **상세 동작**:
  - `threading.Lock`을 활용한 동시성 락을 획득합니다.
  - 현재 워커에서 실행 중인 태스크가 존재하는지 체크합니다 (`self.current_task_id is not None` 및 `self.runner.status == "RUNNING"` 조건 검사).
  - 이미 연산이 활성화된 상태라면 중복 할당 거절 메시지와 함께 `FAILED` 상태를 즉시 반환하여 작업 간섭을 원천 방지합니다.
  - 신규 작업인 경우 `PyTorchTaskRunner` 인스턴스를 바인딩하고, 메인 gRPC 스레드가 대기(Block)하지 않도록 별도의 비동기 데몬 스레드를 분기하여 실행합니다.

### ② `GetTaskStatus(self, request, context)`
- **역할**: 실행 중인 학습 작업의 상태, 진행 백분율, 누적 로그를 Head Node의 모니터링 요청에 맞추어 즉시 회신합니다.
- **매개변수 (`request`)**: `TaskStatusRequest` (대상 `task_id`)
- **응답 (`TaskStatusResponse`)**:
  - `status` (str): 실행기 내부 상태 (`RUNNING`, `SUCCESS`, `FAILED`, `NOT_FOUND`)
  - `progress` (float): 학습 진행 백분율 (0.0% ~ 100.0%)
  - `logs` (str): 각 Epoch 마다 누적되어 저장된 로깅 문자열
- **상세 동작**:
  - 요청된 `task_id`가 현재 바인딩된 `self.runner.task_id`와 다를 경우 상태를 `"NOT_FOUND"`로 회신합니다.
  - 일치할 경우 실행기 객체에서 실시간 수집된 로그(`\n` 구분자로 병합) 및 진행 지표를 전달합니다.

### ③ `ResizeResources(self, request, context)`
- **역할**: 컨테이너의 하드웨어 할당 제어 명령에 맞추어 내부 환경 설정을 변경하기 위한 가상 API입니다.
- **매개변수 (`request`)**: `ResizeRequest` (목표 CPU 코어, Memory 바이트)
- **응답 (`ResizeResponse`)**: 성공 여부 메시지

---

## 4. 하트비트 생존 신고 및 상태 리포트 메커니즘

워커가 기동되면 `heartbeat_sender_loop` 함수가 독립된 데몬 스레드로 즉시 실행되어 아래 단계를 순차적으로 밟아 나갑니다.

### 가. 워커 라이프사이클 흐름 (State Machine)
```mermaid
stateDiagram-v2
    [*] --> Init : Worker 부팅
    Init --> Register : RegisterWorker 호출 (3초 간격 재시도)
    Register --> Active : 등록 승인 (success = True)
    
    state Active {
        [*] --> SendHeartbeat : SendHeartbeat 호출 (1.0초 주기)
        SendHeartbeat --> Delay : DEFAULT_HEARTBEAT_INTERVAL 대기
        Delay --> SendHeartbeat
    }
    
    Active --> Terminated : KeyboardInterrupt / 종료 시그널
    Terminated --> Deregister : DeregisterWorker 전송 (GCS 자원 해제)
    Deregister --> [*]
```

### 나. 세부 동작 구조
1. **서버 예비 부팅 대기**: 자체 gRPC 서비스 서버가 포트 바인딩 및 부팅이 완전히 끝날 수 있도록 1.0초간 지연(`time.sleep(1.0)`) 후 동작을 실행합니다.
2. **Head Node 자동 등록 (Register)**:
   - Head Node의 IP 및 포트로 채널을 열어 `RegisterWorker` 원격 호출을 전송합니다.
   - 연결 거부나 통신 지연(`RpcError`) 발생 시 무한 루프 내에서 **3.0초** 간격으로 계속해서 등록 요청을 재시도하여 노드 기동 순서에 따른 장애를 방지합니다.
3. **주기적 생존 보고 및 자원 계측 (Heartbeat)**:
   - 등록 완료 시, `DEFAULT_HEARTBEAT_INTERVAL` (기본값: 1.0초) 주기로 루프를 수행합니다.
   - `SendHeartbeat` 원격 호출을 통해 워커 식별자, 실시간 CPU 사용률 메트릭 및 메모리 점유 메트릭 정보를 전송합니다.
   - 전송 중 예외가 감지되면 콘솔에 경고 로그를 출력하되 스레드를 중단하지 않고 대기 후 다음 주기에 전송을 지속 재시도합니다.
4. **우아한 종료 (Graceful Shutdown)**:
   - 워커 노드 기동 스크립트에 `KeyboardInterrupt` 시그널이 도달하면 소멸 소환 루프가 동작합니다.
   - Head Node로 `DeregisterWorker` 요청을 전달하여 마스터 노드의 레지스트리(GCS)에서 자신을 안전하게 파기하고 메모리를 회수하게 한 후 gRPC 서버를 완전히 종료합니다.
