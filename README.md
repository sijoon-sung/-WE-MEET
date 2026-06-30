# WE-MEET: Docker 기반 이기종 ML 분산 학습 제어 엔진 (GCS & Scheduler)

본 프로젝트는 단일 Windows 호스트 PC 환경(WSL2 기반 Docker) 하에서 **gRPC**, **리눅스 cGroup 자원 격리**, 그리고 **강화학습(Q-Learning) 및 OS 스케줄링 이론**을 융합하여 이기종 분산 인프라 상의 머신러닝 학습 연산을 실시간으로 제어하고 탄력적으로 스케일링하는 분산 학습 제어 엔진입니다.

---

## 📂 프로젝트 패키지 디렉토리 구조

프로젝트의 전반적인 모듈은 결합도를 낮추고 유지 보수성을 높이기 위해 다음과 같이 역할군에 따라 패키지화되었습니다.

```
WE-MEET/
  ├── head/                      # GCS 및 마스터(Head) 노드 패키지
  │     ├── head.py              # [인프라] gRPC 마스터 서버 기동 엔트리포인트
  │     ├── state.py             # [인프라] GCS 전역 인메모리 공유 상태 정의
  │     ├── cluster_manager.py   # [인프라] WSL2 리소스 가드 및 Docker SDK 스케일 제어
  │     ├── scheduler/           # 스케줄러 계층 패키지
  │     │     ├── __init__.py
  │     │     ├── core.py        # 중앙 스케줄러 스레드 루프 (Backfilling 탑재)
  │     │     ├── static.py      # Static (정적 룰 스텝) 스케줄러
  │     │     └── dynamic.py     # Dynamic (동적 부하 스텝) 스케줄러
  │     ├── q_learning/          # 지능형 의사결정 Q-Learning 패키지
  │     │     ├── __init__.py
  │     │     ├── agent.py       # Q-Learning Agent 클래스 (Aging 수식 탑재)
  │     │     ├── scheduler.py   # Q-Learning 의사결정 스텝 스케줄러
  │     │     └── q_table.json   # 강화학습 경험 축적 파일 (패키지 내 고정)
  │     └── dashboard/           # 모니터링 대시보드 웹 서비스 패키지
  │           ├── __init__.py
  │           └── server.py      # 실시간 대시보드 HTTP 서버 (Port: 8080)
  ├── worker/                    # 분산 학습 연산 워커(Worker) 노드 패키지
  │     ├── worker.py            # 워커 gRPC 서비서 및 하트비트 클라이언트
  │     └── gpu_simulator.py     # CNN/RNN/LSTM 연산 속도 및 하드웨어 점유 시뮬레이터
  ├── common/                    # 공유 라이브러리 및 하이퍼파라미터 설정
  │     ├── config.py
  │     └── cost_model.yaml      # 이기종 인스턴스 요금 및 GPU 성능 스펙 파일
  ├── proto/                     # gRPC 인터페이스 버퍼 정의 및 컴파일 스크립트
  │     ├── babyray.proto
  │     └── compile_proto.py
  ├── references/                # 학술적 레퍼런스 분석서
  │     └── mentoring_ref.md     # 선행 연구 분석 및 극복 방향 기술
  ├── project_proposal.md        # 시스템 설계 및 스케줄링 이론 종합 제안서
  └── docker/                    # 컨테이너화 빌드 및 compose 설정 디렉토리
        ├── Dockerfile.head      # Head Node용 도커 빌드 이미지 명세
        ├── Dockerfile.worker    # Worker Node용 도커 빌드 이미지 명세
        └── docker-compose.yml   # 이기종 클러스터 실증용 Compose 파일
```

---

## 🚀 기동 및 실행 가이드

### 1. Docker Compose 기반 클러스터 기동 (권장)
Head Node와 온디맨드 Worker-1 컨테이너를 가상 네트워크상에 빌드 및 자동 연계하여 띄웁니다.
```bash
# 1. 클러스터 전체 빌드 및 백그라운드 가동
docker-compose -f docker/docker-compose.yml up --build -d

# 2. 실행 로그 모니터링 (실시간 출력)
docker-compose -f docker/docker-compose.yml logs -f
```

*   **실시간 모니터링 웹 대시보드**: 브라우저를 열어 [http://localhost:8080](http://localhost:8080) 에 접속하면 현재 큐 상태, 활성 노드 수, 그리고 가상 예산 소모량을 시각적으로 감시할 수 있습니다.
*   **클러스터 중단 및 완전 회수**:
    ```bash
    docker-compose -f docker/docker-compose.yml down
    ```

### 2. 로컬 가상환경 수동 개별 기동
디버깅 목적 등으로 터미널에서 각각 프로세스를 띄워 테스트할 수 있습니다.
```bash
# 터미널 1: Head Node (GCS 및 스케줄러 기동)
python head/head.py

# 터미널 2: Worker Node (포트 50052번에 수동 가동 및 마스터 연결)
python worker/worker.py --id worker-1 --type on_demand --port 50052 --head-host localhost --head-port 50051
```

---

## 🛠️ 주요 기능 요약

1.  **3대 AI 모형 부하 시뮬레이션**: CNN(연산 지향), RNN(균형), LSTM(메모리 지향) 모형의 Epoch 연산 특징에 따른 물리 리소스 점유 시뮬레이터 구동.
2.  **이기종 자원 격리 (cGroup)**: 컨테이너의 CPU/MEM 자원 크기를 격리하여 모형의 자원 압박 수준 실증.
3.  **OS 스케줄링 기법 접목**: 선두 차단(HOL Blocking) 해결을 위한 **Backfilling** 스케줄러 및 자원 기아(Starvation)를 방지하기 위한 **Aging** 보상 인자 수식 도입.
4.  **탄력성 & 고가용성**: 하트비트 단절 감시를 통한 노드 장애 격리 및 Lineage 기반 태스크 복구 메커니즘 제공.
