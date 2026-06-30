# Head Node 기술 명세서: 분산 제어, GCS 레지스트리 및 패키지 아키텍처

이 문서는 Baby Ray 분산 시스템의 통제 역할을 수행하는 **Head Node**의 리팩토링된 디렉토리 구조, 모듈 간 상호작용 및 gRPC API 규격을 정의한 기술 명세서입니다.

---

## 1. 개요 및 모듈 패키지 구조

Head Node는 클러스터의 마스터 노드로서 전역 공유 메타데이터를 유지하는 GCS와 리소스 감시 가드, 그리고 각 스케줄링 위임 모듈들로 세분화되어 관리됩니다.

```
head/
  ├── head.py              # [인프라] gRPC 서버 부팅 및 백그라운드 스케줄러 스레드 개시
  ├── state.py             # [인프라] worker_registry 및 task_status 전역 데이터 정의
  ├── cluster_manager.py   # [인프라] Docker SDK 컨테이너 조작 및 WSL2 가용 RAM 가드
  ├── scheduler/           # 기본 스케줄러 계층 패키지
  │     ├── __init__.py
  │     ├── core.py        # 중앙 제어 스레드 루프 (Backfilling 스케줄링 정책 실행)
  │     ├── static.py      # Static 스케줄러 스텝 함수
  │     └── dynamic.py     # Dynamic 스케줄러 스텝 함수
  └── q_learning/          # 지능형 Q-Learning 최적화 의사결정 패키지
        ├── __init__.py
        ├── agent.py       # QLearningAgent 클래스 (Aging 누적 지연 페널티 적용)
        ├── scheduler.py   # Q-Learning 스케줄러 스텝 의사결정 함수
        └── q_table.json   # 강화학습 경험치 테이블 JSON 영속 파일
```

---

## 2. 모듈간 데이터 흐름 및 상호작용

```
[ head.py (gRPC) ] ──(하트비트 수신)──> [ state.py (GCS 캐시) ]
        │                                       ▲
        └──────(스레드 실행 개시)───────────┐      │ (인메모리 자원 참조)
                                            ▼      │
                                    [ scheduler/core.py ] 
                                            │
                             ┌──────────────┼──────────────┐
                             ▼              ▼              ▼
                        [ static.py ]  [ dynamic.py ]  [ q_learning/scheduler.py ]
                                                           └──> [ agent.py ]
```

1.  **gRPC 인프라 (`head.py`)**: 워커 노드들의 생존 신고를 받아 `state.py`의 `worker_registry`에 하트비트 시각과 CPU/MEM을 실시간 업데이트합니다.
2.  **중앙 스케줄러 (`scheduler/core.py`)**: 백그라운드 스레드로 돌며 대기열에 작업이 유입되면 현재 활성화된 스케줄러 모드(`SCHEDULER_MODE = "dynamic"`)에 맞춰 해당 패키지 파일의 step 함수로 의사결정을 위임합니다.
3.  **지능형 의사결정 (`q_learning/`)**: Q-Learning 모드 기동 시, `agent.py`가 4차원 상태(태스크 프로파일, 활성 인스턴스 정보, 예산 잔량)를 평가하고 Bellman Equation에 맞춰 Q-Table을 갱신합니다.

---

## 3. 핵심 gRPC 서비스 API 명세

Head Node는 포트 `50051`에서 `BabyRayServiceServicer`를 가동하여 다음 RPC 통신을 수신 처리합니다.

### ① `RegisterWorker(RegisterRequest) -> RegisterResponse`
*   **역할**: 최초 기동된 워커 노드를 GCS 레지스트리에 `status="IDLE"`로 등록합니다.
*   **상세**: `context.peer()`를 역산하여 도커 가상 네트워크 브릿지 내의 워커 실제 IP 주소를 동적으로 감지하여 세팅합니다.

### ② `SendHeartbeat(HeartbeatRequest) -> HeartbeatResponse`
*   **역할**: 워커로부터 실시간 CPU/MEM 점유율을 1초 주기로 받아 `last_heartbeat` 타임스탬프를 갱신합니다. 
*   **상세**: 15초간 하트비트가 끊어진 노드는 DEAD 노드로 격리 분류하고 Docker SDK를 통해 즉시 컨테이너를 강제 Stop/Remove 처리합니다.
