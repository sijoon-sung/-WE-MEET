# WE-MEET: Docker 기반 경량 분산 런타임 및 GPU 자원 제어 프로젝트

본 프로젝트는 단일 로컬 PC 환경에서 **Docker Compose**를 이용해 가상 분산 노드 클러스터를 구축하고, **gRPC** 기반의 통신망 설계, **cGroup**을 활용한 자원 격리, 그리고 **Task Lineage**에 기반한 자동 장애 복구를 통해 분산 컴퓨팅의 코어 시스템 소프트웨어 작동 방식을 이해하고 연구하는 프로젝트입니다.

상용 클라우드 서비스의 상위 API만 단순 호출하는 데 그치지 않고, 인프라의 근간을 직접 설계하고 PyTorch AI 연산 부하 조건 하에서 성능 및 안정성을 실증합니다.

---

## 📋 핵심 연구 및 구현 기능
1. **gRPC 기반 분산 통신망 직접 구축**: Head Node와 Worker Node 간의 실시간 생존 신호 수집(Heartbeat) 및 AI 작업 지시(Task Assignment) 파이프라인 설계
2. **NVIDIA GPU 공유 및 cGroup 자원 격리**: 물리 GPU(RTX 4060)를 공유하면서도 cGroup을 통해 컨테이너별 CPU/Memory 자원 사용 한계를 독립적으로 강제 격리하여 이기종 성능 비대칭성 모사
3. **자원 인지형 동적 스케줄링**: 각 노드의 실시간 자원 상태 및 대기 작업을 감지해 부하를 동적으로 나누어주는 오프로딩(버스팅) 스케줄러 구현
4. **Task Lineage 기반 장애 극복**: Worker 노드 중단 시 글로벌 제어 저장소(GCS)의 작업 족보(DAG)를 역추적해 누락된 연산만 자동으로 재계산해 복원하는 Auto Healing 파이프라인 구현

---

## ⚙️ 개발 및 실행 환경 설정 가이드

본 프로젝트를 구동하기 위한 개발 환경 세팅 가이드입니다. 

### 1. 호스트 PC 요구사항 (Windows)
* **OS**: Windows 10/11 (WSL2 백엔드 필수 활성화)
* **GPU**: NVIDIA GeForce RTX 4060 Laptop GPU (8GB VRAM) 이상
  - 호스트 PC에 최신 NVIDIA 그래픽 드라이버 설치가 완료되어 있어야 합니다.
* **RAM**: 16GB 이상 권장

### 2. Docker Desktop 설정 (GPU 파스스루 연동)
1. **Docker Desktop**을 실행하고 우측 상단의 톱니바퀴 아이콘(**Settings**)을 클릭합니다.
2. **General** 탭에서 `Use the WSL 2 based engine` 옵션이 체크되어 있는지 확인합니다.
3. **Resources ➔ WSL integration** 탭으로 이동하여 `Enable integration with my default WSL distro` 옵션을 켭니다.
4. 호스트에 최신 NVIDIA 드라이버가 존재하면 WSL2 환경에서 자동으로 NVIDIA GPU 인식이 가능합니다.

### 3. 컨테이너 내부 런타임 환경
* **베이스 이미지**: `pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime` (PyTorch CUDA 지원 공식 배포판)
* **의존성 라이브러리**:
  - `grpcio` & `grpcio-tools` (gRPC 통신 스택)
  - `psutil` (자원 모니터링 데몬)
  - `torch` (CUDA 가속 AI 연산 및 스레드 조율)
  - `pandas`, `matplotlib` (벤치마크 결과 그래프 시각화)
