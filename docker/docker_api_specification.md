# 🐳 Docker 및 Docker Compose 가이드 & 인프라 제어 API 명세서

본 문서는 **WE-MEET** 분산 환경 프로젝트에서 사용하는 Docker와 Docker Compose의 기본 개념, 세팅 방법 및 Python Docker SDK를 통한 가상 클러스터 제어 API 명세를 제공합니다.

---

## 1. Docker Compose 기초 & 세팅 항목 가이드

Docker Compose는 여러 개의 Docker 컨테이너를 정의하고 일괄 실행할 수 있게 해주는 도구입니다. 설정 파일(`docker-compose.yml`)에서 사용하는 핵심 세팅 항목들의 기능과 작성 방법은 다음과 같습니다.

### 핵심 설정 항목 설명

| 항목 (Parameter) | 설명 및 용도 |
| :--- | :--- |
| **`version`** | Docker Compose 파일 규격 버전을 정의합니다 (예: `'3.8'`). 버전에 따라 지원하는 기능이 다릅니다. |
| **`networks`** | 가상 네트워크를 정의합니다. 지정된 컨테이너들은 동일 네트워크 대역 내에서 호스트네임을 사용해 상호 통신할 수 있습니다. |
| **`services`** | 실행할 컨테이너들의 모음입니다. 각 서비스 이름(예: `head`, `worker-1`)은 네트워크 내에서 도메인 이름처럼 사용됩니다. |
| **`build`** | 이미지를 직접 빌드할 때 사용합니다.<br>• `context`: 빌드 명령이 수행될 기준 경로 (상위 폴더인 프로젝트 루트 `..` 로 지정)<br>• `dockerfile`: 빌드에 사용할 Dockerfile의 상대 경로 (`docker/Dockerfile` 로 지정) |
| **`image`** | 컨테이너가 사용할 이미지 이름을 지정합니다. `build`가 선언되어 있다면 빌드된 이미지의 이름이 됩니다. |
| **`container_name`** | 도커 데몬에서 식별할 실제 컨테이너 이름을 명시적으로 지정합니다. |
| **`command`** | 컨테이너가 구동될 때 기본적으로 실행할 명령어(Entrypoint 파라미터)를 지정합니다. |
| **`volumes`** | 호스트 디렉토리나 파일을 컨테이너 내부로 마운트합니다.<br>• `- /var/run/docker.sock:/var/run/docker.sock`: 호스트의 Docker Daemon Socket을 공유하여, 컨테이너 내부에서 도커 제어 명령을 내릴 수 있게(Docker-in-Docker 유사) 설정합니다. |
| **`ports`** | 호스트 포트와 컨테이너 내부 포트를 바인딩(포트 포워딩)합니다 (예: `"외부포트:내부포트"`). |
| **`environment`** | 컨테이너 내부의 프로세스가 읽을 환경 변수를 정의합니다. |
| **`deploy.resources.limits`** | **cGroup 자원 격리**의 핵심 설정입니다. 컨테이너가 사용할 수 있는 최대 리소스를 강제 제한합니다.<br>• `cpus`: 사용할 수 있는 CPU 코어 수 (예: `'1.0'`, `'2.0'`)<br>• `memory`: 최대 메모리 용량 (예: `1024M`, `2048M`) |
| **`deploy.reservations.devices`** | GPU 자원 파스스루(Pass-through)를 설정합니다. WSL2 및 NVIDIA Container Toolkit이 활성화되어 있어야 구동됩니다. |
| **`depends_on`** | 컨테이너 간의 기동 순서 및 의존성을 정의합니다. (예: `head`가 먼저 정상 구동된 후 `worker` 구동) |

---

## 2. Docker SDK 기반 가상 클러스터 제어 API

Head 노드는 호스트의 Docker Daemon과 `/var/run/docker.sock` 채널을 통해 양방향 통신하여 실시간으로 Worker 컨테이너의 라이프사이클을 제어합니다. Python 환경에서는 공식 `docker` 라이브러리를 활용합니다.

```python
# Head 노드에서 Docker 데몬에 연결하는 기본 코드
import docker
client = docker.from_env()
```

### 가. Worker 컨테이너 상태 모니터링
각 Worker 컨테이너의 실시간 상태 정보 및 통계 메트릭을 수집할 수 있습니다.

```python
def get_worker_status(container_name):
    try:
        container = client.containers.get(container_name)
        # 컨테이너 상태 (running, exited, paused 등)
        state = container.status
        # 실시간 자원 사용량 통계 (CPU, Memory, Network I/O)
        stats = container.stats(stream=False)
        
        return {
            "status": state,
            "stats": stats
        }
    except docker.errors.NotFound:
        return {"status": "NOT_FOUND"}
```

### 나. 동적 cGroup 자원 조정 (Resize API)
gRPC 통신을 통해 `ResizeResources` 요청이 들어오면, Head 노드는 해당하는 Worker 컨테이너의 cGroup 자원 사용 제한(CPU/Memory)을 실시간으로 업데이트합니다. **컨테이너를 재시작하지 않고 실시간 격리 수준을 동적으로 바꾸는 핵심 기능**입니다.

```python
def resize_container_resources(container_name, cpu_cores, memory_bytes):
    """
    실시간으로 running 상태인 컨테이너의 cGroup 한계를 재설정합니다.
    """
    try:
        container = client.containers.get(container_name)
        
        # CPU Nano 코어 단위로 환산 (1 Core = 1,000,000,000 nano cpus)
        nano_cpus = int(cpu_cores * 1_000_000_000)
        
        # update() API를 사용하여 cgroup limits 동적 업데이트
        container.update(
            nano_cpus=nano_cpus,
            mem_limit=memory_bytes,
            memswap_limit=memory_bytes # Swap 비활성화
        )
        print(f"[Docker API] Successfully resized {container_name} to CPU: {cpu_cores} Cores, Mem: {memory_bytes} Bytes")
        return True, "Success"
    except Exception as e:
        print(f"[Docker API] Failed to resize {container_name}: {str(e)}")
        return False, str(e)
```

### 다. Auto Scaling (노드 동적 증설 및 회수)
대기 중인 Task가 많거나 CPU 부하가 오래 지속될 경우 새로운 Worker 컨테이너를 복제 및 기동합니다.

```python
import subprocess

def scale_workers(service_name, target_count):
    """
    docker-compose 명령을 subprocess로 호출하여 동적 스케일링을 수행합니다.
    """
    try:
        # 프로젝트 루트의 docker-compose.yml 경로 타겟팅
        cmd = [
            "docker", "compose", 
            "-f", "docker/docker-compose.yml", 
            "up", "-d", 
            "--scale", f"{service_name}={target_count}"
        ]
        
        # 명령어 실행
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"[Docker API] Scaling {service_name} to {target_count} completed.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Docker API] Scale failed: {e.stderr}")
        return False
```

---

## 3. 로컬 인프라 구동 및 관리 CLI 가이드

프로젝트 루트 디렉토리(`WE-MEET/`) 기준에서 사용 가능한 Docker 인프라 관리 명령어 리스트입니다.

### 1) 이미지 빌드 및 컨테이너 시작
```bash
# Docker Compose 백그라운드 구동 및 이미지 강제 빌드
docker compose -f docker/docker-compose.yml up -d --build
```

### 2) 서비스 로그 실시간 모니터링
```bash
# 전체 노드의 표준 출력(stdout) 로그 모니터링
docker compose -f docker/docker-compose.yml logs -f
```

### 3) 클러스터 수동 스케일링 테스트
```bash
# Spot-A (worker-2) 노드를 3개로 확장
docker compose -f docker/docker-compose.yml up -d --scale worker-2=3
```

### 4) 전체 인프라 중지 및 정리
```bash
# 컨테이너 중지 및 가상 네트워크 회수
docker compose -f docker/docker-compose.yml down
```
