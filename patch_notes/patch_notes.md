# 📝 Baby Ray 프로젝트 패치 노트 (Patch Notes & Error Logs)

본 폴더와 문서는 **Baby Ray** Docker 기반 분산 런타임 프로젝트의 구축 및 테스트 과정에서 발생한 핵심 시스템 오류들과 이를 해결하기 위한 패치 내역을 체계적으로 기록한 문서입니다.

---

## 📅 2026-06-27 패치 내역

### 1. [오토스케일링/일관성] 초기 스케일 변수와 docker-compose 실기동 대수 간 정합성 에러 패치
* **발생 현상 & 배경:**
  - `docker-compose.yml`에는 Spot 워커(`worker-2`, `worker-3`)가 주석 처리되어 서비스 기동 시 실제 기동되는 대수가 **0대**였습니다.
  - 그러나 `head.py` 내의 스케줄러 루프에서는 이 스케일 초기 변수(`current_worker_2_scale`, `current_worker_3_scale`)를 `1`로 정의하여 사용하고 있었습니다.
  - 이로 인해, 스케일아웃으로 Spot 워커가 최초 1대 띄워졌을 때 변수값은 `2`가 되고, 이후 부하 감소로 스케일인이 작동할 때 변수가 `2`에서 `1`로 감소하며 유일하게 켜져 있던 1대의 Spot 워커 컨테이너를 삭제하게 됩니다.
  - 그 결과, 실제 동작하는 컨테이너는 0대임에도 변수값은 `1`로 유지되어 더 이상 최소 보장 조건(`scale > 1`)에 의해 스케일인이 정상적으로 작동하지 않고 자원 카운트의 정합성이 꼬이는 결함이 존재했습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 초기 스케일 변수 값을 실제 구동 환경에 부합하도록 `0`으로 수정했습니다 (`current_worker_2_scale = 0`, `current_worker_3_scale = 0`).
  - 스케일인 감축 판단 기준을 `> 1`에서 `> 0`으로 완화하여, 부하가 없을 때 모든 동적 Spot 워커가 완전히 0대로 스케일인(안전 회수 및 파괴) 되도록 수식 정합성을 패치하였습니다.

### 2. [빌드/복구] `compile_proto.py` 복구 및 임포트 치환 패턴 불일치 예외 패치
* **발생 현상 & 배경:**
  - 개발 툴 오조작으로 `compile_proto.py`가 유실되어 빌드 시 exit code 2가 떴던 문제를 해결하고자 1차 복구하였으나, 새로 제너레이션된 `babyray_pb2_grpc.py` 내부의 구문(`import babyray_pb2 as babyray__pb2`)이 1차 작성한 `compile_proto.py`의 탐색 패턴(`import babyray_pb2 as proto_dot_babyray__pb2`)과 불일치하여 패치가 누락되는 상황이 발견되었습니다. 이로 인해 컨테이너 구동 시 `ModuleNotFoundError: No module named 'babyray_pb2'` 크래시가 발생했습니다.
* **해결 및 패치 내용:**
  - [compile_proto.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/compile_proto.py)를 전면 보강하여 두 가지 패턴(`import babyray_pb2 as babyray__pb2` 및 `import babyray_pb2 as proto_dot_babyray__pb2`)을 모두 탐색해 유연하게 치환하도록 개선했습니다.
  - 이를 통해 컨테이너 내부에서도 모듈 탐색 오류 없이 GCS 및 워커 통신이 완벽하게 초기화되도록 조치했습니다.

### 3. [시뮬레이터/부하 강화] 스케일링 검증을 위한 난수 멀티태스킹 워크로드 강화
* **발생 현상 & 배경:**
  - 기존 부하 모델은 4초당 70% 확률로 1~3개 수준의 작업이 넉넉한 기한으로 들어와 병목이 쉽게 풀리거나 Q-Learning 에이전트가 3대 이상의 스케일아웃을 수행할 필요성을 체감하지 못했습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 주기적 가상 작업 생성 로직을 개편했습니다.
  - 매 주기마다 80% 확률로 **1~5개의 태스크가 동시에 쏟아지도록(난수 강화)** 유입 수량을 대폭 증가시켰습니다.
  - 태스크 연산 에포크 수도 5~12 범위로 상향 난수화하고, 마감기한은 15~35초로 타이트하게 조여 동적 Spot 컨테이너가 3대 이상으로 적극 확장되는 스트레스 테스트 시나리오를 탑재했습니다.

### 4. [오토스케일링/모니터링] 순차 네이밍(인덱스 재사용) 적용 및 어지러운 `@` ID 접미사 전면 삭제
* **발생 현상 & 배경:**
  - 기존의 동적 Spot 워커들은 `worker-2-4206`과 같이 무작위 4자리 난수 네이밍을 지정하고 기동하였으며, GCS 내 중복 식별 방지를 위해 `worker-2-1@fa5a067dda4e` 처럼 컨테이너 ID를 `@` 뒤에 꼬리표로 엮어서 사용했습니다.
  - 이 방식은 콘솔 모니터링 로그를 어수선하게 만들 뿐 아니라 가독성과 직관성을 떨어뜨리는 한계가 있었습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 `scale_out_worker`를 개편하여 현재 가동 중인 워커들의 인덱스를 스캔해 비어 있는 가장 작은 순차 인덱스(1번부터 시작)를 부여하고, 스케일인으로 소멸한 번호는 우선 재활용(Index Recycling)하도록 정교화했습니다.
  - 순차 명칭(`worker-2-1` 등) 자체로 고유 식별이 완전 보장되므로 [worker/worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py)에서 `@socket.gethostname()` 접미사를 전면 삭제했습니다.
  - Head Node의 `SendHeartbeat` 및 `scale_in_specific_worker`에서도 `@` 파싱 코드를 소거하고, `f"babyray-{worker_id}"` 명명 규칙을 통해 Docker SDK가 직접 컨테이너를 식별 및 제어하도록 아키텍처를 간결하게 가다듬었습니다.

### 5. [자원가드/안정성] Head 가동 시 호스트 잔존 좀비 컨테이너 일괄 청소 (Startup Cleanup)
* **발생 현상 & 배경:**
  - 사용자가 분산 시스템을 Ctrl+C 등으로 비정상 종료 시, Docker SDK를 통해 호스트에 독립 기동한 Spot 워커 컨테이너들은 함께 자동 종료되지 않고 좀비 상태로 유지되었습니다.
  - 이는 호스트의 메모리 부족(0.81 GB로 잠식) 현상을 유발하여 다음 테스트 기동 시 스케일아웃 기동을 전면 마비시키는 원인이 되었습니다.
* **해결 및 패치 내용:**
  - Head 노드가 가동될 때 호스트 상의 이전 사이클 동적 워커 컨테이너들을 찾아내 소거하는 `cleanup_zombie_containers()` 메소드를 구현하고, gRPC 서버의 부팅 속도를 블로킹하지 않도록 `serve()` 상단에서 **비동기 데몬 스레드**로 구동되도록 처리했습니다.
  - 이를 통해 기동 시마다 좀비 컨테이너들을 깨끗하게 일괄 정지/삭제(자원 회수)하여 호스트 메모리 누수를 원천 방어하고 부팅 지연 현상을 완벽히 해소했습니다.

### 6. [자원가드/성능] 사용자 물리 자원 확보 피드백 기반 메모리 임계치 2.0 GB로 상향 보정
* **발생 현상 & 배경:**
  - 호스트 자원 가드의 가용 메모리 경계선이 1.0 GB로 작동할 때, 사용자 컴퓨터의 가용 메모리가 1.0 GB 미만으로 밀려나면서 스케일아웃이 계속 안전 거부되어 교착상태에 빠졌습니다.
* **해결 및 패치 내용:**
  - 사용자가 호스트 시스템의 2.0 GB 이상 가용 공간을 책임지고 항상 확보하는 튜닝 피드백을 적용함에 따라, [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py) 내 `is_host_resource_sufficient()`의 차단 임계치를 `2.0 GB`로 상향 보정하여, 2GB 이상의 가용 메모리가 확보된 상태에서 오토스케일링이 강력하고 안정적으로 작동하도록 일치시켰습니다.

---

## 📅 2026-06-26 패치 내역

### 1. [아키텍처/오토스케일링] Docker SDK 기반 동적 클러스터 스케일링 전면 개편 (멘토 피드백 2-1 반영)
* **발생 현상 & 배경:**
  - 기존에는 `docker compose --scale` 명령을 사용하여 스팟 워커들의 활성 대수를 변경하고 있었습니다.
  - 이 경우, 축소(Scale-In)가 일어날 때 Docker가 임의의 컨테이너를 종료하므로 **실제 작업을 수행 중인(BUSY) 컨테이너가 중단되는 작업 유실 위험**이 있었으며, 형상관리가 무작위화되는 한계가 존재했습니다.
* **해결 및 패치 내용:**
  - `docker-compose.yml` 서비스 정의에서 스케일링 대상인 `worker-2` 및 `worker-3` 설정을 완전히 비활성화(주석 처리)하여 정적 배포 영역과 동적 스케일링 영역을 분리했습니다.
  - Head 노드의 [head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)에 Docker SDK(`DOCKER_CLIENT.containers.run` 및 `containers.get`)를 직접 호출하는 `scale_out_worker(node_type)` 및 `scale_in_specific_worker(node_type)`를 새로 구현했습니다.
  - **안전한 Scale-In (타겟별 회수):** 스케일인 시 GCS `worker_registry`에서 상태가 `"IDLE"`인 워커만 골라내어 고유 컨테이너 ID를 파싱한 뒤, Docker SDK로 해당 컨테이너만 지정 정지 및 제거(`container.stop` 및 `remove`)하도록 조치하여 태스크 무중단 가용성을 확보했습니다.

---

### 2. [모니터링/자원 가드] 호스트 시스템 물리 자원 관리 레이어 구축 (멘토 피드백 2-4 반영)
* **발생 현상 & 배경:**
  - 클러스터 전체 및 호스트의 물리 자원 한계치를 감지하지 못하고 무조건적인 스케일아웃을 감행할 경우, 호스트 PC 자체가 자원 부족(OOM)으로 크래시가 발생할 수 있는 위험이 있었습니다.
* **해결 및 패치 내용:**
  - Head 노드 내에 호스트 시스템의 물리 자원 상황을 감시하는 보호막 함수 `is_host_resource_sufficient()` 및 `get_gpu_free_memory()`를 추가했습니다.
  - `psutil`을 활용해 호스트의 가용 메모리가 **1.0 GB 미만**이거나, `nvidia-smi` 덤프를 통해 가용 GPU VRAM이 **500 MiB 미만**인 경우, 스케일아웃(`scale_out_worker`)을 사전에 차단하고 예외 보호 로그를 출력하는 **Global Resource Guard** 레이어를 완성했습니다.

---

### 3. [고가용성/결함 주입] 가상 OOM 이상 상황 모사 및 선제 회피 (Failure Injection & Avoidance) (멘토 피드백 2-2 반영)
* **발생 현상 & 배경:**
  - 실제 개발 장비의 하드웨어를 강제로 셧다운하거나 물리 OOM을 터뜨려 테스트하는 것은 장비 수명과 안전성에 위험하므로, 소프트웨어적으로 이상 징후를 모사하고 극복하는 모의 결함 테스트베드가 필요했습니다.
* **해결 및 패치 내용:**
  - [gpu_simulator.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/gpu_simulator.py) 및 [worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 내부에 **가상 OOM Failure Injector**를 이식했습니다.
  - `"LSTM"` 연산 작업 수행 시 15%의 확률로(혹은 태스크 ID에 `fail` 문구 포함 시) 가상 OOM을 강제 유발하고 작업 상태를 `FAILED`로 보고하며, 하트비트 시 자원 점유율을 **99.9%**로 속여서 송신하도록 구현했습니다.
  - **OOM 선제 회피 제어:** Head 노드에서 워커에 태스크를 배정할 때, GCS 정보 상 메모리가 **90% 이상** 점유된 노드는 자동으로 선택지에서 배제하도록 구현하여 이상 워커로의 할당을 선제적으로 예방했습니다.
  - **자가 자원 회수 연동:** 과부하/오버로드된 IDLE 워커는 룰 기반 Scale-In 루프에서 **1순위 감축 대상**으로 자동 필터링되어 호스트로부터 신속하게 삭제 정리되도록 자가 치유 라이프사이클을 연계했습니다.

---

### 4. [오토스케일링] 룰 기반(Rule-based) Scale-In 감지 루프 및 타이머 설계 (멘토 피드백 2-3 반영)
* **발생 현상 & 배경:**
  - 기존 Q-learning 에이전트는 스케일아웃만 선택할 수 있었고, 스케일인의 부재로 인해 한 번 늘어난 컨테이너들이 영원히 소멸되지 않아 비용 인지에 역행했습니다.
* **해결 및 패치 내용:**
  - [head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 `scheduler_loop()` 내부에 대기열 크기가 0개이고 평균 CPU 사용률이 20% 미만인 상태를 체크하는 타이머 `empty_queue_duration`을 탑재했습니다.
  - 해당 저부하 상태가 **10.0초 이상 지속**될 경우, 기동 중인 Spot 워커를 1대씩 순차적으로 감축(`scale_in_specific_worker`)하고 1대 초과분을 자동으로 회수하도록 룰 기반 탄력 오토스케일러를 연동시켰습니다.

---

## 📅 2026-06-25 패치 내역

### 1. [네트워크] Worker 컨테이너의 Head 노드 연결 실패 이슈
* **발생 현상:**
  ```text
  babyray-worker-1  | [Heartbeat] Head 서버 연결 시도: localhost:50051...
  babyray-worker-1  | [Heartbeat] Head 서버 연결 지연. 3초 후 재시도...
  ```
* **원인 분석:**
  - Docker Compose 가상 브리지 네트워크 환경에서 `localhost` 혹은 `127.0.0.1`은 호스트 PC나 타 컨테이너가 아니라 **Worker 컨테이너 자기 자신**을 가리킵니다.
  - 따라서 Worker 노드는 자기 자신 내부의 50051 포트로 gRPC 연결을 시도하게 되어 통신 실패(연결 지연)가 무한 반복되었습니다.
* **해결 및 패치 내용:**
  - `docker-compose.yml` 서비스 정의에 기재된 `HEAD_HOST=head` 및 `HEAD_PORT=50051` 환경 변수를 Worker가 정상적으로 읽어오도록 [worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 최하단 `argparse` 기본값을 패치했습니다.
  ```python
  # 기존 코드
  parser.add_argument("--head-host", type=str, default="localhost")
  
  # 수정 코드 (환경 변수를 우선적으로 참조하도록 바인딩)
  parser.add_argument("--head-host", type=str, default=os.environ.get("HEAD_HOST", "localhost"))
  ```

---

### 2. [인프라 제어] Head 노드 내부 Docker CLI 명령어 부재 이슈
* **발생 현상:**
  ```text
  babyray-head      | [Scheduler Action] SCALE_OUT 트리거 -> worker-2 (Spot-A) 대수 증설 지시 (2대)
  babyray-head      | [Docker SDK CLI 에러] worker-2 스케일링 실패: [Errno 2] No such file or directory: 'docker'
  ```
* **원인 분석:**
  - Head Node의 스케줄러는 부하 상황 감지 시 `subprocess`를 통해 `docker compose` 명령을 직접 내려 컨테이너를 동적으로 스케일링하도록 구현되어 있습니다.
  - 그러나 베이스 이미지(`pytorch/pytorch`)는 PyTorch 구동에 특화된 런타임 이미지이므로, 컨테이너 내부에 `docker` 클라이언트 툴이나 `compose` 플러그인이 깔려 있지 않아 명령 실행 자체가 실패했습니다.
* **해결 및 패치 내용:**
  - [docker/Dockerfile](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/docker/Dockerfile) 빌드 명령어 스펙에 초경량 static `docker-cli` 패키지 및 `docker-compose` v2 플러그인을 직접 다운로드하여 설치하는 레이어를 추가했습니다.
  ```dockerfile
  # Docker CLI 및 Docker Compose CLI 플러그인 빌드 타임 자동 설치
  RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-24.0.7.tgz | tar -xz -C /tmp \
      && mv /tmp/docker/docker /usr/local/bin/ \
      && rm -rf /tmp/docker
  RUN mkdir -p /usr/local/lib/docker/cli-plugins \
      && curl -SL https://github.com/docker/compose/releases/download/v2.24.5/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose \
      && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  ```

---

### 3. [런타임] 컨테이너 실행 환경의 임포트 경로 에러 (`sys.path`)
* **발생 현상:**
  ```text
  babyray-head  | ModuleNotFoundError: No module named 'q_learning'
  babyray-worker-2  | ModuleNotFoundError: No module named 'gpu_simulator'
  ```
* **원인 분석:**
  - Docker 컨테이너 구동 시 작업 디렉토리(`/app`)를 루트로 하여 모듈을 실행(`python -m head.head`)하므로 파이썬의 `sys.path` 상단에는 `/app`만 들어가게 됩니다.
  - 이에 따라 `head/head.py` 내부에서 같은 디렉토리의 `q_learning.py`를 `from q_learning import ...` 형태로 임포트할 때 경로를 탐색하지 못하는 패키지 격리 에러가 발생했습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py) 및 [worker/worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 최상단에 현재 실행 중인 파일의 절대 경로 폴더를 `sys.path`에 추가하도록 조치했습니다.
  ```python
  sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))) # 루트 경로
  sys.path.append(os.path.abspath(os.path.dirname(__file__)))                    # 개별 패키지 경로 추가
  ```

---

### 4. [인프라 제어/고가용성] Head 컨테이너 무한 재구성 및 중단 이슈 (DooD 재귀 루프)
* **발생 현상:**
  - Auto-Scaling 시점에 Head 컨테이너가 갑자기 종료되고 `exited with code 137`이 발생하며, Worker들이 `경고: 생존 신고 전송 실패`를 무한히 출력하고 연결을 체결하지 못했습니다.
* **원인 분석:**
  - Head 노드 내부에서 `docker compose up -d --scale` 명령을 실행할 때, Docker Compose가 호스트 상에 구동 중인 전체 컨테이너 세트를 검사하여 형상 일치 여부를 판별합니다.
  - 이 과정에서 Docker Compose가 `babyray-head` 컨테이너의 최신 사양이 맞지 않거나 업데이트가 필요하다고 오판하여 **자기 자신(`babyray-head`)을 죽이고 재생성(Recreate)** 하였습니다.
  - 이로 인해 Head 프로세스는 종료(137)되고, 새로 시작된 Head는 기존 GCS 레지스트리를 잃은 채 기동 $\rightarrow$ 또다시 스케일아웃 발생 $\rightarrow$ 자기 자신을 다시 죽이고 재생성하는 **무한 재귀 OOM/137 루프**에 빠졌습니다.
* **해결 및 패치 내용:**
  - [head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py) 내부 `scale_workers` 함수 내의 Docker Compose 실행 인자값에 `--no-recreate` 옵션을 추가하고, 마지막 타겟으로 특정 `service_name`(예: `worker-2`)만 지정하도록 수정하여 Head 컨테이너를 건드리지 않도록 차단했습니다.
  ```python
  cmd = [
      "docker", "compose",
      "-f", compose_path,
      "up", "-d",
      "--no-recreate",
      "--scale", f"{service_name}={target_count}",
      service_name
  ]
  ```

---

### 5. [인프라 제어/확장성] 동적 스케일아웃 적용 시 컨테이너명 충돌 및 중복 등록 이슈 (Auto Scaling 확장성 패치)
* **발생 현상:**
  - `docker compose --scale` 실행 시 고정된 컨테이너명 설정으로 인해 스케일링이 차단되거나, 여러 대 구동 시 동일한 ID(`worker-2`)로 헤드 노드에 중복 등록되어 기존 세션 정보를 덮어쓰고 통신이 어긋나는 이슈가 있었습니다.
* **원인 분석:**
  - `docker-compose.yml` 내부에 `container_name: babyray-worker-2`와 같이 고정된 컨테이너명이 설정되어 있으면, Docker Compose는 이를 2대 이상으로 확장하여 띄울 수 없습니다 (이름 충돌 방지 차원).
  - 또한, `--id worker-2` 옵션으로 실행되는 모든 스케일링 인스턴스들이 동일한 ID로 헤드에 `RegisterWorker`를 호출하여 레지스트리 맵의 키를 덮어쓰는 문제가 있었습니다.
* **해결 및 패치 내용:**
  - [docker/docker-compose.yml](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/docker/docker-compose.yml) 파일에서 스케일링 대상인 `worker-2` 및 `worker-3` 서비스의 고정 `container_name` 설정을 제거하여 도커가 고유 번호 기반의 다중 컨테이너를 가동할 수 있도록 허용했습니다.
  - [worker/worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 최하단 구동부에서 `socket.gethostname()` (컨테이너 ID)을 추출하여 기존 ID 뒤에 `@` 구분자로 결합함으로써 고유 ID를 보장했습니다 (`worker-2@<container_id>`).
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py) 내부 `SendHeartbeat`에서 이 `@` 구분값에서 컨테이너 ID를 파싱하여 도커 SDK의 개별 리소스 메트릭을 추적하도록 처리하고, 스케줄러 루프에서 특정 ID가 아닌 `node_type`과 `IDLE` 상태 기준으로 가용한 워커를 탐색·할당하도록 범용성을 패치했습니다.

---

### 6. [모니터링] `psutil` 및 `cgroups` 연동을 통한 실시간 실제 자원 사용량 리포팅 패치
* **발생 현상:**
  - 기존에는 Worker 노드 기동 시 Heartbeat 전송부에서 실시간 점유율이 아닌 하드코딩된 더미 메트릭 값(`cpu=12.5%`, `mem=40.0%`)을 송신하고 있었습니다.
* **원인 분석:**
  - 로컬 노드 기동 및 컨테이너 환경의 격리 시 실제 연산 부하가 스케줄러로 피드백되지 않아 Q-learning 에이전트의 상태(State) 판단에 왜곡이 생길 수 있었습니다.
* **해결 및 패치 내용:**
  - [worker/worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 최상단에 `psutil` 라이브러리를 임포트하고, 하트비트 루프 기동 전 CPU 수집 캘리브레이션을 수행하도록 구현했습니다.
  - 리눅스 컨테이너 격리 메모리(cgroup v1/v2)를 우선 탐색하는 경로 파싱 로직(`memory.usage_in_bytes`, `memory.current` 등)을 탑재하여 격리 제한 대비 실제 사용 비중을 산출하고, 예외 발생 시 `psutil.virtual_memory().percent`로 자동 폴백(Fallback) 처리하여 gRPC 통신으로 실시간 데이터를 전송하도록 연동을 완수했습니다.

---

## 📅 2026-06-24 패치 내역

### 1. Protobuf 컴파일 외부 임포트 시 경로 불일치 이슈
* **발생 현상:**
  - `grpc_tools.protoc` 컴파일러가 생성한 `babyray_pb2_grpc.py` 내부에 `import babyray_pb2 as babyray__pb2`가 선언되어, 외부 모듈에서 `import proto.babyray_pb2_grpc` 형태로 패키지 접근 시 의존 관계가 깨져 임포트가 실패했습니다.
* **해결 및 패치 내용:**
  - [compile_proto.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/compile_proto.py) 내부 컴파일 완료 코드 블록에 임포트 경로 자동 치환(Patch) 논리를 적용하여 컴파일 직후 파일 내 `import babyray_pb2` 구문을 `from proto import babyray_pb2`로 문자열 치환 패치하도록 수정하여 모듈 구조를 정상화시켰습니다.
