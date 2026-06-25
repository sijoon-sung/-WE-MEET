# 📝 Baby Ray 프로젝트 패치 노트 (Patch Notes & Error Logs)

본 폴더와 문서는 **Baby Ray** Docker 기반 분산 런타임 프로젝트의 구축 및 테스트 과정에서 발생한 핵심 시스템 오류들과 이를 해결하기 위한 패치 내역을 체계적으로 기록한 문서입니다.

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

## 📅 2026-06-24 패치 내역

### 1. Protobuf 컴파일 외부 임포트 시 경로 불일치 이슈
* **발생 현상:**
  - `grpc_tools.protoc` 컴파일러가 생성한 `babyray_pb2_grpc.py` 내부에 `import babyray_pb2 as babyray__pb2`가 선언되어, 외부 모듈에서 `import proto.babyray_pb2_grpc` 형태로 패키지 접근 시 의존 관계가 깨져 임포트가 실패했습니다.
* **해결 및 패치 내용:**
  - [compile_proto.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/compile_proto.py) 내부 컴파일 완료 코드 블록에 임포트 경로 자동 치환(Patch) 논리를 적용하여 컴파일 직후 파일 내 `import babyray_pb2` 구문을 `from proto import babyray_pb2`로 문자열 치환 패치하도록 수정하여 모듈 구조를 정상화시켰습니다.
