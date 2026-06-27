# Head Node 기술 명세서: 분산 제어, GCS 레지스트리 및 스케줄러

이 문서는 Baby Ray 분산 컴퓨팅 시스템의 중앙 통제 역할을 수행하는 **Head Node**의 아키텍처, 데이터 구조, gRPC 서버 명세 및 스케줄링 메커니즘을 정의한 기술 명세서입니다.

---

## 1. 개요 및 역할

Head Node는 클러스터의 마스터 노드로서 분산 시스템 내 모든 메타데이터를 유지 및 동기화하는 **GCS(Global Control Store)** 역할을 겸하며, 워커들의 생존 상태를 감시하고 최적의 워커 노드에 연산 작업을 분배하는 지능형 스케줄러를 가동합니다.

```
                  ┌──────────────────────────────────────────┐
                  │                 Head Node                │
                  │  ┌──────────────┐      ┌──────────────┐  │
                  │  │   gRPC 서버   │      │  스케줄러    │  │
                  │  │ (Heartbeat,  │ <──> │  및 모니터링 │  │
                  │  │  Register)   │      │   루프       │  │
                  │  └──────────────┘      └──────────────┘  │
                  │          ▲                     │         │
                  │          │ (인메모리 갱신)      │ (Assign)│
                  │          ▼                     ▼         │
                  │  ┌──────────────────────────────────┐    │
                  │  │   worker_registry (인메모리 GCS)   │    │
                  │  └──────────────────────────────────┘    │
                  └──────────────────────────────────────────┘
                               ▲                   │
                    Heartbeat  │                   │ AssignTask
                    (매 1.0초) │                   │ (gRPC)
                               │                   ▼
                     [ Worker Containers (cgroup 격리) ]
```

---

## 2. 주요 데이터 구조

Head Node의 메모리 내에서 가상 분산 노드의 모든 메타데이터를 보존하고 다중 스레드 안전성을 확보하기 위해 다음 변수들을 활용합니다.

| 변수명 | 데이터 타입 | 설명 |
| :--- | :--- | :--- |
| `worker_registry` | `dict` | 등록된 모든 활성 워커의 상태 정보를 관리하는 인메모리 GCS. key는 `worker_id` (str)입니다. |
| `registry_lock` | `threading.Lock` | gRPC 요청 처리 스레드들과 스케줄러 백그라운드 스레드 간의 `worker_registry` 동시 접근(Race Condition)을 방지하기 위한 뮤텍스 락. |
| `task_counter` | `int` | 고유한 태스크 ID 생성을 위한 전역 카운터 변수. |

### `worker_registry` 내부 구조 예시
```json
{
  "worker-01": {
    "node_type": "on_demand",
    "ip": "172.18.0.2",
    "port": 50052,
    "last_heartbeat": 1719323456.78,
    "cpu": 12.5,
    "mem": 40.0,
    "status": "IDLE"
  }
}
```

---

## 3. gRPC 서비스 API 명세

Head Node는 워커 노드들의 등록, 퇴장 및 생존 신고(하트비트)를 처리하기 위해 `BabyRayServiceServicer`를 상속받은 gRPC 서버를 포트 `50051`에서 가동합니다.

### ① `RegisterWorker` (RPC)
- **역할**: 구동된 Worker Node의 최초 등록을 처리하고 GCS 레지스트리에 초기 세팅을 반영합니다.
- **요청 메시지 (`RegisterRequest`)**:
  - `worker_id` (str): 워커 식별자
  - `node_type` (str): 노드 등급 (`on_demand`, `spot_a`)
  - `port` (int32): 워커가 수신 대기 중인 gRPC 포트 번호
- **응답 메시지 (`RegisterResponse`)**:
  - `success` (bool): 등록 성공 여부 (`True`/`False`)
  - `message` (str): 완료 혹은 에러 메시지
- **상세 동작**:
  - `context.peer()`를 파싱하여 호출한 워커의 실제 IP 주소(IPv4 또는 IPv6)를 동적으로 추출합니다. (WSL2 및 Docker 가상 네트워크 환경 대응)
  - `registry_lock`을 획득한 후 해당 워커 정보를 `status="IDLE"`, `cpu=0.0`, `mem=0.0`으로 초기화하여 `worker_registry`에 적재합니다.

### ② `DeregisterWorker` (RPC)
- **역할**: 워커가 프로세스 종료 시 보내는 퇴장 요청을 처리하여 클러스터 활성 노드 풀에서 즉시 제거합니다.
- **요청 메시지 (`DeregisterRequest`)**: `worker_id` (str)
- **응답 메시지 (`DeregisterResponse`)**: `success` (bool), `message` (str)

### ③ `SendHeartbeat` (RPC)
- **역할**: 각 워커로부터 실시간으로 전송되는 성능 및 자원 메트릭을 수신하여 갱신합니다.
- **요청 메시지 (`HeartbeatRequest`)**:
  - `worker_id` (str): 워커 식별자
  - `cpu_utilization` (float): 현재 워커 컨테이너의 실시간 CPU 사용률 (%)
  - `memory_utilization` (float): 현재 워커 컨테이너의 실시간 메모리 사용률 (%)
- **응답 메시지 (`HeartbeatResponse`)**: `ack` (bool)
- **상세 동작**:
  - `worker_registry`에서 해당 워커의 `last_heartbeat` 타임스탬프를 호출 시점의 `time.time()`으로 갱신합니다.
  - 전송받은 `cpu` 및 `mem` 사용률 수치를 레지스트리에 업데이트하여 스케줄러가 참조할 수 있게 합니다.

---

## 4. 백그라운드 스케줄러 및 모니터링 메커니즘

Head Node 구동 시, 메인 스레드와 별개로 두 가지 핵심 흐름(장애 감지 및 작업 스케줄링)을 가진 `scheduler_loop`가 백그라운드 스레드로 상시 실행됩니다.

```mermaid
flowchart TD
    Start([scheduler_loop 기동]) --> Sleep[1초 대기]
    Sleep --> Lock[registry_lock 획득]
    Lock --> CheckDead[1. DEAD 노드 탐색]
    
    CheckDead -->|현재 시간 - last_heartbeat > 3.0초| MarkDead[GCS에서 워커 제거 및 로그 출력]
    CheckDead -->|정상 생존| FindIdle[2. IDLE 워커 탐색]
    
    MarkDead --> FindIdle
    FindIdle -->|가용 워커가 존재하고 대기 큐가 있음| DispatchLoop[Multi-Dispatch: 1틱 내 가용 워커 전원 연속 할당]
    FindIdle -->|가용한 워커 없음| ReleaseLock[Lock 해제 후 대기 상태로 회귀]
    
    DispatchLoop --> Assign[3. run_task_on_worker 백그라운드 스레드 가동]
    Assign --> ReleaseLock
    ReleaseLock --> Sleep
```

### ① 장애 감지 및 자원 정리 루프
- **판정 기준**: 현재 시간(`time.time()`)에서 워커가 마지막으로 보낸 하트비트 시각(`last_heartbeat`)을 뺀 값이 **3.0초**를 초과하는 경우 해당 노드를 `DEAD` 상태로 판정합니다.
- **사후 처리**: `worker_registry`에서 해당 워커 정보를 완전 격리 삭제하고 경고 로그를 출력합니다. 이후 스케줄러는 해당 노드에 작업을 배정하지 않습니다.

### ② 자원 인지형 동적 스케줄링 및 다중 배정 (Multi-Dispatch)
- **가용 풀 탐색**: `worker_registry` 내부의 워커 상태 변수인 `status`가 `"IDLE"`인 워커 노드들만 필터링합니다.
- **다중 배정 (Multi-Dispatch)**: 1초 주기 루프 내에서 Q-Learning 학습 정책에 근거하여 액션을 판단하고, **가용한 IDLE 워커와 대기 작업이 존재하는 한 연속적으로 작업을 싹 꺼내어 배정**함으로써 1틱 내 큐 병목을 완벽히 소화합니다.
- **OOM 선제 회피 및 최적 노드 선정**: 실시간 메모리 사용률이 `90%` 이상인 워커 노드는 OOM 회비를 위해 할당 후보에서 배제하며, 조건 만족 시 CPU 사용률이 낮은 노드에 작업을 배정합니다.
- **비동기 작업 실행**: 스케줄러의 루프가 블로킹되는 것을 방지하기 위해, 선정된 노드에 대한 작업 전송 및 완료 대기는 별도의 스레드(`run_task_on_worker`)를 통해 비동기 처리됩니다.

### ③ 실시간 진행 모니터링 (`run_task_on_worker`)
작업이 배정된 워커의 수명 주기 및 진행률을 실시간으로 추적하는 독립 실행 스레드입니다.
1. **상태 변경**: 대상 워커의 GCS 상태를 `"BUSY"`로 즉시 변경하여 추가적인 중복 할당을 방지합니다.
2. **AssignTask 요청**: 해당 워커의 gRPC 엔드포인트로 `AssignTask` 원격 호출을 보내어 모델 종류와 총 에포크 수를 지시합니다.
3. **상태 폴링 (Polling)**: 
   - 2초 간격으로 `stub.GetTaskStatus(...)`를 원격 호출하여 워커의 현재 진행률(`progress`)과 학습 로그(`logs`)를 실시간으로 받아옵니다.
   - 응답받은 상태 코드가 `SUCCESS`, `FAILED`, `COMPLETED` 중 하나에 해당하면 모니터링 루프를 해제합니다.
4. **자원 반환**: 태스크 처리가 완료되거나 예외 상황(gRPC 연결 끊김 등)이 발생하면 워커 노드의 상태를 다시 `"IDLE"`로 변경하여 다음 작업을 대기시킵니다.

---

## 5. 고가용성 및 클러스터 안전 가드 메커니즘

Head Node는 단순 분산 스케줄링을 넘어 호스트 머신의 안전한 하드웨어 동작 상태를 보호하고 가용성을 올리기 위해 다음과 같은 안전 장치들을 탑재하고 있습니다.

### ① 비동기 GCS 좀비 컨테이너 클리너 (`cleanup_zombie_containers`)
- **개념**: 시스템이 비정상 중단되었을 경우 호스트 PC 상에 `babyray-worker-2-x` 형태의 동적 컨테이너들이 메모리를 점유한 채 방치되는 문제를 방지합니다.
- **동작**: Head Node가 가동되는 즉시, 기존 좀비 컨테이너를 강제 정지 및 삭제(`remove`)합니다.
- **비동기화의 이점**: 구버전 컨테이너 삭제 작업이 수 초간 지속될 때 gRPC 서버 포트 바인딩 및 부팅이 지연되는 병목을 해결하기 위해, 이 클리너를 **비동기 데몬 스레드로 구동**하여 Head 부팅 속도를 극대화했습니다.

### ② 호스트 가용 물리 메모리 가드 (`is_host_resource_sufficient`)
- **개념**: 스케일아웃 시 컨테이너가 늘어남에 따라 호스트 메모리가 고갈되어 전체 시스템이 다운되는 현상을 방지합니다.
- **동작**: 신규 Spot 워커를 가동하기 전에 호스트 OS의 가용 실제 메모리를 실시간 스캔하여, **2.0 GB 미만**일 경우 동적 노드 가동을 보류 및 거부합니다.

### ③ GPU VRAM 가드 (`get_gpu_free_memory`)
- **개념**: 호스트 물리 GPU의 비디오 메모리(VRAM)가 고갈되어 PyTorch가 CUDA Out of Memory 커널 예외로 사망하는 현상을 막기 위함입니다.
- **동작**: `nvidia-smi` 혹은 시뮬레이션을 호출해 가용 VRAM 공간을 실시간 체크하고, **500 MiB 미만**인 경우 추가 증설을 차단합니다.

### ④ 다중 배정 큐 롤백 방어 (Task Rollback Guard)
- **개념**: 스케줄러가 작업을 1초 내에 동시 다발적으로 분배(Multi-Dispatch)할 때, 특정 워커 노드와 매칭이 지연되거나 네트워크 단절로 유실되는 것을 막습니다.
- **동작**: 작업 배정이 성공하지 못했을 경우 해당 태스크를 드롭하지 않고 큐의 최우선 순위(`insert(0, task)`)로 즉시 안전 롤백시킵니다.

### ⑤ 식별 번호 재사용 (Index Recycling)
- **개념**: 동적으로 스케일아웃 및 스케일인이 수행될 때 컨테이너 ID 및 포트 관리를 체계화합니다.
- **동작**: `worker-2-1`부터 30까지 돌며 회수되어 비어 있는 가장 작은 인덱스 번호를 탐색하고 재선점하여 바인딩함으로써 식별 중복과 포트 충돌을 완벽히 배제합니다.

