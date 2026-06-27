# ==============================================================================
# WE-MEET: Head Node gRPC 서버 및 Q-Learning 스케줄러 통합 구현 (head/head.py)
# ==============================================================================

import grpc
from concurrent import futures
import time
import os
import sys
import psutil
import threading
import random
import subprocess

# 실행 시 프로젝트 루트 디렉토리 및 현재 디렉토리를 sys.path에 추가하여 패키지들을 정상적으로 찾을 수 있도록 설정합니다.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from proto import babyray_pb2
from proto import babyray_pb2_grpc
from common.config import DEFAULT_HEAD_PORT

# Q-Learning 스케줄링 에이전트 가져오기
from q_learning import QLearningAgent

# Docker SDK 임포트 
# -> import 오류가 자주 나서 try-except로 감싸줌
try:
    import docker
    # docker.from_env()는 호스트의 Docker Daemon 소켓(/var/run/docker.sock)과 자동으로 채널을 수립합니다.
    DOCKER_CLIENT = docker.from_env()
    print("[Docker SDK] 호스트 도커 데몬 연결 성공.")
except Exception as e:
    DOCKER_CLIENT = None
    print(f"[Docker SDK 경고] 도커 데몬 연결 실패 (예외 안전 모드 가동): {e}")


# --- 1. 전역 리소스 상태 및 GCS (Global Control Store) 정의 ---

# 워커 관리용 인메모리 GCS 레지스트리
# worker_id -> { "node_type": str, "ip": str, "port": int, "last_heartbeat": float, "cpu": float, "mem": float, "status": str }
worker_registry = {} # 등록된 모든 worker 노드의 상태 정보 등록
registry_lock = threading.Lock()

# 가상 태스크 대기열 (Task Queue)
task_queue = []
queue_lock = threading.Lock()

# 전역 가상 자산 관리 변수
virtual_budget = 100.0  # 초기 예산 $100.0달러
task_counter = 0 # 고유한 TASK ID 생성을 위한 카운터 변수

# Q-Learning 에이전트
# cost.yaml의 경로를 찾음 (head에서 ../common/cost_model.yaml -> 부모 디렉토리 common 폴더의 cost_model.yaml)
COST_MODEL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../common/cost_model.yaml'))
agent = QLearningAgent(cost_model_path=COST_MODEL_PATH)
# 에이전트는 cost_model.yaml에 정의된 비용 모델을 참고해서 학습하거나 행동을 결정하게 됩니다.

# --- 2. Docker 가상 클러스터 동적 통제 API (Docker SDK & CLI) ---

def get_gpu_free_memory():
    """
    [Global Host Resource Manager]
    nvidia-smi 명령어를 호출하여 호스트 GPU의 가용 VRAM 용량(MiB)을 획득합니다.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,nounits,noheader"],
            capture_output=True, text=True, check=True
        )
        free_vram = int(result.stdout.strip())
        return free_vram
    except Exception:
        # GPU 드라이버 미인식 시 모니터링 불가 상태로 간주 (-1 반환)
        return -1


def cleanup_zombie_containers():
    """
    [Docker SDK Startup Clean]
    Head 노드 기동 시, 호스트에 남겨진 이전 라이프사이클의 
    동적 Spot 워커 컨테이너(babyray-worker-2-*, babyray-worker-3-*)들을 일괄 정리하여 자원을 회수합니다.
    """
    if DOCKER_CLIENT is None:
        return
        
    print("[Docker GCS Startup] 기존 잔존 동적 컨테이너 청소 작업을 시작합니다...")
    try:
        containers = DOCKER_CLIENT.containers.list(all=True)
        cleaned_count = 0
        for container in containers:
            c_name = container.name
            if c_name.startswith("babyray-worker-2-") or c_name.startswith("babyray-worker-3-"):
                print(f"[Docker GCS Startup] 잔존 컨테이너 감지 및 정리: {c_name}")
                try:
                    container.stop(timeout=2)
                except Exception:
                    pass
                try:
                    container.remove(force=True)
                    cleaned_count += 1
                except Exception as e:
                    print(f"[Docker GCS Startup] 컨테이너 {c_name} 제거 실패: {e}")
        print(f"[Docker GCS Startup] 총 {cleaned_count}개의 잔존 컨테이너가 정리되었습니다.\n")
    except Exception as e:
        print(f"[Docker GCS Startup] 잔존 컨테이너 조회 중 에러 발생: {e}\n")


def is_host_resource_sufficient():
    """
    [Global Host Resource Manager]
    호스트 시스템의 실시간 물리 메모리 가용량을 점검하여 자원 임계치 안전 여부를 판정합니다.
    """
    try:
        mem = psutil.virtual_memory()
        available_gb = mem.available / (1024 ** 3)
        if available_gb < 2.0:
            print(f"[Global Resource Guard] 가용 물리 메모리 부족 경고: {available_gb:.2f} GB < 2.0 GB")
            return False
        return True
    except Exception as e:
        print(f"[Global Resource Guard] 자원 점검 중 예외 발생: {e}")
        return True


def scale_out_worker(node_type):
    """
    [Docker SDK Container Run API]
    Docker SDK를 사용하여 node_type에 기반한 신규 Spot 워커 노드를 동적으로 가동합니다.
    - cGroup 격리 제한(cpus, memory limit), 네트워크 자동 매핑 및 볼륨 마운트가 이식됩니다.
    """
    if DOCKER_CLIENT is None:
        print("[Docker SDK] 도커 데몬과 연결되어 있지 않아 동적 스케일아웃을 스킵합니다.")
        return False

    try:
        spec = {
            "spot_a": {
                "nano_cpus": 1000000000, # 1.0 Core
                "mem_limit": "1024m",
                "port": 50060
            }
        }

        if node_type not in spec:
            print(f"[Docker SDK] 알 수 없는 노드 유형: {node_type}")
            return False

        node_spec = spec[node_type]

        # 1. 호스트 자원 가드
        if not is_host_resource_sufficient():
            print("[Docker SDK] 호스트 물리 메모리 부족으로 스케일아웃 기동을 안전하게 거부합니다.")
            return False

        free_vram = get_gpu_free_memory()
        if free_vram != -1 and free_vram < 500:
            print(f"[Global Resource Guard] 가용 GPU VRAM 부족 ({free_vram} MiB < 500 MiB). 스케일아웃을 보류합니다.")
            return False

        # 2. 네트워크 자동 감지
        network_name = "babyray-net"
        try:
            head_container = DOCKER_CLIENT.containers.get("babyray-head")
            networks = head_container.attrs.get("NetworkSettings", {}).get("Networks", {})
            if networks:
                network_name = list(networks.keys())[0]
                print(f"[Docker SDK] 자동 감지된 클러스터 네트워크: {network_name}")
        except Exception as e:
            print(f"[Docker SDK] 네트워크 자동 감지 실패, 기본값 '{network_name}' 사용: {e}")

        # 3. 중복 포트 회피
        with registry_lock:
            existing_ports = [info["port"] for info in worker_registry.values()]

        candidate_port = node_spec["port"]
        while candidate_port in existing_ports:
            candidate_port += 1

        # 4. 고유 ID 및 컨테이너명 생성 (비어있는 가장 작은 순차 번호 탐색 및 재사용)
        base_id = "worker-2"
        
        with registry_lock:
            # 현재 GCS에 등록된 해당 노드 타입의 순차 인덱스 추출
            existing_indices = []
            for wid in worker_registry.keys():
                name_part = wid.split("@")[0] if "@" in wid else wid
                if name_part.startswith(base_id + "-"):
                    try:
                        idx = int(name_part.split("-")[-1])
                        existing_indices.append(idx)
                    except ValueError:
                        pass
            
            # 1부터 시작하여 비어있는 가장 작은 번호(인덱스) 탐색 (Index Recycling)
            candidate_index = 1
            while candidate_index in existing_indices:
                candidate_index += 1
                
        worker_id = f"{base_id}-{candidate_index}"
        container_name = f"babyray-{worker_id}"

        # 5. 실행 커맨드
        cmd = [
            "python", "-m", "worker.worker",
            "--id", worker_id,
            "--type", node_type,
            "--port", str(candidate_port),
            "--head-host", "babyray-head",
            "--head-port", "50051"
        ]

        # GPU 요청 객체 빌드
        device_requests = []
        try:
            device_requests = [
                docker.types.DeviceRequest(count=-1, capabilities=[['gpu']])
            ]
        except Exception:
            device_requests = []

        # 6. 컨테이너 동적 생성 및 실행 (분리된 Worker 이미지 사용)
        DOCKER_CLIENT.containers.run(
            image="babyray-worker-image:latest",
            name=container_name,
            command=cmd,
            detach=True,
            network=network_name,
            nano_cpus=node_spec["nano_cpus"],
            mem_limit=node_spec["mem_limit"],
            device_requests=device_requests,
            environment={
                "NODE_TYPE": node_type,
                "HEAD_HOST": "babyray-head",
                "HEAD_PORT": "50051",
                "PYTHONUNBUFFERED": "1"
            }
        )

        print(f"[Docker SDK] 신규 Spot 컨테이너 가동 완료: ID='{worker_id}' | Name='{container_name}' | Port={candidate_port}")
        return True

    except Exception as e:
        print(f"[Docker SDK 에러] 동적 스케일아웃 실행 실패: {e}")
        return False


def scale_in_specific_worker(node_type):
    """
    [Docker SDK Container Stop/Remove API]
    GCS worker_registry에서 지정된 node_type 중 IDLE 상태인 워커를 선별하여 안전하게 종료 및 삭제합니다.
    - OOM 이상 징후(메모리 사용률 90% 이상)가 감지된 노드가 있을 경우 우선적으로 회수 후보로 삼습니다.
    """
    if DOCKER_CLIENT is None:
        print("[Docker SDK] 도커 데몬 미연결로 스케일인을 스킵합니다.")
        return False

    try:
        target_worker_id = None
        with registry_lock:
            # IDLE 상태인 해당 타입의 워커들을 필터링
            idle_workers = [
                (wid, info) for wid, info in worker_registry.items()
                if info["node_type"] == node_type and info["status"] == "IDLE"
            ]
            if idle_workers:
                # 메모리 사용률이 높은 순(오버로드 상태)으로 정렬하여 1순위로 회수
                idle_workers.sort(key=lambda x: x[1].get("mem", 0.0), reverse=True)
                target_worker_id = idle_workers[0][0]

        if target_worker_id is None:
            print(f"[Docker SDK] 감축 경고: 회수 가능한 IDLE 상태의 {node_type} 워커가 존재하지 않습니다.")
            return False

        # worker-id format: worker-2-1 -> container name: babyray-worker-2-1
        container_ref = f"babyray-{target_worker_id}"

        # 1. Docker SDK를 통한 컨테이너 중지 및 제거
        try:
            container = DOCKER_CLIENT.containers.get(container_ref)
            print(f"[Docker SDK] IDLE 컨테이너 회수 시작: {target_worker_id} (컨테이너 ID: {container_ref})")
            container.stop(timeout=5)
            container.remove()
            print(f"[Docker SDK] IDLE 컨테이너 회수 성공: {target_worker_id}")
        except Exception as e:
            print(f"[Docker SDK 경고] 컨테이너 직접 조작 실패 ({e}), GCS 레지스트리만 소거 처리 진행.")

        # 2. GCS 레지스트리에서 제거
        with registry_lock:
            if target_worker_id in worker_registry:
                del worker_registry[target_worker_id]

        return True

    except Exception as e:
        print(f"[Docker SDK 에러] 스케일인 수행 실패: {e}")
        return False


def get_container_metrics(container_name):
    """
    [Docker SDK Resource Monitor API]
    Docker SDK 객체를 통해 해당 워커 컨테이너의 실시간 메모리/CPU 사용률 메트릭을 도출합니다.
    """

    # Docker Daemon = 실제 실행 하는 엔진
    # Docker Client Object = 개발자가 Docker Engine을 쉽게 조작할 수 있게 해주는 파이썬 도구
    if DOCKER_CLIENT is None:
        return 0.0, 0.0
        
    try:

        # 호스트 도커 데몬으로부터 가동 중인 컨테이너 정보를 로드합니다.
        container = DOCKER_CLIENT.containers.get(container_name)
        # 1회성 스냅샷 메트릭 수집
        # stream=True로 설정하면 실시간 데이터 스트림을 받을 수 있음
        stats = container.stats(stream=False)
        
        # stats 딕셔너리 구조
        # stats['cpu_stats']['cpu_usage']['total_usage']: 컨테이너의 CPU 총 사용량
        # stats['cpu_stats']['system_cpu_usage']: 호스트의 CPU 총 사용량
        # stats['memory_stats']['usage']: 컨테이너의 메모리 사용량
        # stats['memory_stats']['limit']: 컨테이너의 메모리 한계

        # CPU 연산률 산출
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})
        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
        num_cpus = cpu_stats.get("online_cpus", 1)
        
        # 호스트 시스템 전체가 CPU를 쓰는 동안, 
        # 그중 컨테이너가 얼마만큼의 비율을 차지했는지 %로 환산하는 표준 Docker CPU 계산

        cpu_util = 0.0
        if system_delta > 0 and cpu_delta > 0:
            cpu_util = (cpu_delta / system_delta) * num_cpus * 100.0
            
        # Memory 사용률 산출
        mem_stats = stats.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_limit = mem_stats.get("limit", 1)
        # 메모리를 몇 % 사용 중인지 계산
        mem_util = (mem_usage / mem_limit) * 100.0 if mem_limit > 0 else 0.0
        
        return round(cpu_util, 1), round(mem_util, 1)
    except Exception:
        # 컨테이너 미발견 혹은 윈도우 도커 비호환 대비 대체 더미값 반환
        return random.uniform(10.0, 30.0), random.uniform(30.0, 50.0)


# --- 3. Head Node gRPC 통신 서비스 핸들러 ---

# [Worker가 Head에 request를 보내는 흐름] (worker.py 참조)
#
# Worker 부팅 (docker-compose 실행)
#   │
#   ├─ 1. argparse로 실행 인자 파싱 (worker.py line 212~220)
#   │     --id worker-1 --type on_demand --port 50052
#   │
#   ├─ 2. socket.gethostname()으로 고유 ID 생성 (worker.py line 222)
#   │     "worker-1@컨테이너ID"
#   │
#   ├─ 3. 자기 자신의 gRPC 서버 기동 (worker.py line 178~183)
#   │     포트 50052에서 Head의 명령(AssignTask/GetTaskStatus)을 대기
#   │
#   └─ 4. heartbeat_sender_loop 스레드 시작 (worker.py line 187~192)
#         │
#         ├─ Head에 gRPC 채널 연결 (worker.py line 103)
#         │     head:50051
#         │
#         ├─ ★ stub.RegisterWorker() 호출 (worker.py line 111~115)
#         │     RegisterRequest {
#         │       worker_id: "worker-1@abc123",
#         │       node_type: "on_demand",
#         │       port: 50052           ← Worker가 자기 gRPC 서버 포트를 알려줌
#         │     }
#         │     ip는 Worker가 보내는 것이 아니라, Head가 context.peer()에서 직접 파싱
#         │
#         ├─ ★ 등록 성공 후 → 1초 간격 stub.SendHeartbeat() 반복 (worker.py line 132~172)
#         │     HeartbeatRequest { worker_id, cpu_utilization, memory_utilization }
#         │
#         └─ ★ Worker 종료(Ctrl+C) 시 → stub.DeregisterWorker() 호출 (worker.py line 204~206)
#               DeregisterRequest { worker_id }  → GCS에서 자기 자신을 해제
#

class BabyRayHeadServicer(babyray_pb2_grpc.BabyRayServiceServicer):
    def RegisterWorker(self, request, context):
        # peer  변수 = worker 노드가 접속해 온 네트워크 정보 (IPv4, IPv6)
        peer = context.peer()
        
        # IPv4 환경일 때: "ipv4:192.168.0.5:50051"
        # IPv6 환경일 때: "ipv6:[2001:db8::1]:50051"


        # gRPC peer IP 주소 파싱 (IPv4 및 IPv6 호환)
        if peer.startswith("ipv4:"):
            ip = peer.split(":")[1]
        elif peer.startswith("ipv6:"):
            last_colon = peer.rfind(":")
            ip = peer[5:last_colon]
            ip = ip.replace("%5B", "").replace("%5D", "").replace("[", "").replace("]", "")
        # 포멧을 알 수 없음
        else:
            ip = "127.0.0.1"
            
        # IPv6 로컬호스트 [::1]일 경우 [IP_ADDRESS]로 변경
        if ip == "::1":
            ip = "127.0.0.1"

            
        with registry_lock:
            worker_registry[request.worker_id] = {
                "node_type": request.node_type.lower(),
                "ip": ip,
                "port": request.port, # Worker가 자기 gRPC 서버 포트를 알려줌 
                "last_heartbeat": time.time(),
                "cpu": 0.0,
                "mem": 0.0,
                "status": "IDLE"
            }
            print(f"[Head Registry] 워커 신규 등록: ID='{request.worker_id}' | 주소: {ip}:{request.port} | 타입: {request.node_type}")
        
        # 등록이 성공적으로 되었다면    
        return babyray_pb2.RegisterResponse(
            success=True, 
            message=f"Worker '{request.worker_id}' registered successfully on Head GCS."
        )

    def DeregisterWorker(self, request, context):
        # mutual exclusion 보장
        with registry_lock:
            
            if request.worker_id in worker_registry:
                # 인메모리 캐시에서 지우기
                del worker_registry[request.worker_id]
                print(f"[Head Registry] 워커 정상 퇴장: ID='{request.worker_id}'")
                return babyray_pb2.DeregisterResponse(success=True, message="Deregistered.")
            return babyray_pb2.DeregisterResponse(success=False, message="Worker not found.")

    def SendHeartbeat(self, request, context):
        # worker_id를 사용해 직접 도커 컨테이너 식별 (f"babyray-{worker_id}")
        container_name = f"babyray-{request.worker_id}"
        real_cpu, real_mem = get_container_metrics(container_name)
        
        with registry_lock:
            if request.worker_id in worker_registry:
                worker_registry[request.worker_id]["last_heartbeat"] = time.time() # 마지막에 체크한 시간 변경
                # SDK 실시간 자원량 값 주입 (실패 시 하트비트 전송자가 송신한 더미 값 반영)
                worker_registry[request.worker_id]["cpu"] = real_cpu if real_cpu > 0 else request.cpu_utilization
                worker_registry[request.worker_id]["mem"] = real_mem if real_mem > 0 else request.memory_utilization
                
                # 수신된 메트릭 로그 출력
                print(f"[Head GCS] Heartbeat 수신 | ID: '{request.worker_id}' | CPU: {worker_registry[request.worker_id]['cpu']}%, Mem: {worker_registry[request.worker_id]['mem']}%")
                
        return babyray_pb2.HeartbeatResponse(ack=True)


# --- 4. 태스크 스레드 핸들러 및 Q-Learning 상태 피드백 루프 ---

def run_task_on_worker(worker_id, worker_info, task, state, action):
    """
    [Task 실행 및 강화학습 피드백 스레드]
    특정 워커에 작업을 할당하여 gRPC로 실행 지시를 내리고 완료 모니터링 후 보상(Reward)을 계산하여 Q-Table을 갱신합니다.
    """
    global virtual_budget
    # worker_registry -> worker_info를 뽑아서 줌
    ip = worker_info['ip']
    worker_address = f"[{ip}]:{worker_info['port']}" if ":" in ip else f"{ip}:{worker_info['port']}"
    task_id = task["task_id"]
    model_type = task["model_type"]
    epochs = task["epochs"]
    worker_type = worker_info["node_type"]
    
    print(f"\n[Scheduler Action] >>> 작업 할당 실행: {task_id} ({model_type}) -> 워커 '{worker_id}' ({worker_type})")
    
    # 워커 상태를 BUSY로 마킹하여 중복 할당 방지
    with registry_lock:
        if worker_id in worker_registry:
            worker_registry[worker_id]["status"] = "BUSY"
            
    success = False
    execution_time = 0.0
    
    try:
        # 워커 gRPC 채널 오픈 / stub 파일 생성

        # 워커의 주소(IP:Port)로 gRPC 통신을 위한 '파이프(채널)'를 연결
        channel = grpc.insecure_channel(worker_address)
        # 내 PC에 있는 함수처럼 쉽게 호출할 수 있게 해주는 '리모컨(Stub)' 객체를 생성
        stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
        
        # 1. 작업 개시 전송
        start_time = time.time()
        result = stub.AssignTask(babyray_pb2.TaskAssignment(
            task_id=task_id,
            model_type=model_type,
            dataset_path=f"data/{model_type.lower()}_dataset.pt",
            epochs=epochs
        ))
        
        if result.status == "RUNNING":
            # 2. 완료 여부 실시간 폴링 감시
            while True:
                time.sleep(1.5)
                
                # 워커가 죽어 오프라인 처리된 경우 통신 예외 발생 유도
                with registry_lock:
                    if worker_id not in worker_registry:
                        raise grpc.RpcError("Worker node went offline during task execution.")
                
                # 작업을 진행률을 받기
                status_res = stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=task_id))
                # worker가 응답한 상태를 체크
                if status_res.status in ["SUCCESS", "COMPLETED"]:
                    success = True
                    execution_time = time.time() - start_time
                    print(f"[Scheduler Feedback] 작업 {task_id} 완료 성공! (실제 수행 시간: {execution_time:.2f}초)")
                    break
            
                elif status_res.status == "FAILED":
                    success = False
                    execution_time = time.time() - start_time
                    print(f"[Scheduler Feedback] 경고: 작업 {task_id} 연산 실패 리포트 수신.")
                    break
        else:
            print(f"[Scheduler Feedback] 작업 개시 거부당함: {result.message}")
            
    except grpc.RpcError as e:
        print(f"[Scheduler Feedback 에러] 워커 '{worker_id}' 실행 중 통신 크래시 감지: {e}")
        success = False
        execution_time = time.time() - task["enqueue_time"]
    finally:
        # GCS 워커 노드 상태 복구
        with registry_lock:
            if worker_id in worker_registry:
                worker_registry[worker_id]["status"] = "IDLE"
                
        # --- 3. Q-Learning 보상 산출 및 Q-Table 업데이트 피드백 단계 ---
        end_time = time.time()
        delay_time = max(0.0, end_time - task["deadline"])
        deadline_exceeded = end_time > task["deadline"]
        
        # 보상 수식 적용
        reward = agent.calculate_reward(
            success=success,
            execution_time=execution_time,
            worker_type=worker_type,
            delay_time=delay_time,
            deadline_exceeded=deadline_exceeded
        )
        
        # 가상 예산 차감
        cost_profile = agent.nodes_config.get(worker_type, {"cost_per_hour": 0.0})
        cost_per_hour = cost_profile.get("cost_per_hour", 0.0)
        task_cost = cost_per_hour * (execution_time / 3600.0)
        virtual_budget -= task_cost
        
        # 큐 상태 갱신 후 다음 상태 추출
        with queue_lock:
            q_len_next = min(len(task_queue), 10)
            
        with registry_lock:
            w1_act = 1 if any(info["node_type"] == "on_demand" for info in worker_registry.values()) else 0
            w2_act = 1 if any(info["node_type"] == "spot_a" for info in worker_registry.values()) else 0
            active_bitmap_next = (w1_act * 1) + (w2_act * 2)
            
        budget_level_next = 0 if virtual_budget < 20.0 else (1 if virtual_budget < 70.0 else 2)
        next_state = (q_len_next, active_bitmap_next, budget_level_next)
        
        # Bellman Equation에 입각해 Q-Value 업데이트
        agent.update_q_value(state, action, reward, next_state)
        agent.save_q_table()
        
        print(f"[Q-Learning Update] State={state} | Action={action} | Reward={reward:.4f} | NextState={next_state}")
        print(f"[Q-Learning Update] 잔여 가상 예산: ${virtual_budget:.4f}달러\n")
        
        # 만약 실패했다면 Task Lineage 자가 복구를 위해 큐 최전방에 작업을 재삽입
        if not success:
            print(f"[장애 복구] 작업 {task_id} 장애 유실 감지 -> 복구를 위해 대기 큐 재할당.")
            with queue_lock:
                task_queue.insert(0, task)


# --- 5. 백그라운드 Q-Learning 스케줄러 핵심 루프 ---

def scheduler_loop():
    global task_counter, virtual_budget
    print("[Scheduler] Q-Learning 비용/SLA 인지형 의사결정 엔진 가동 성공.")
    
    model_types = ["CNN", "RNN", "LSTM"]
    
    # 최대 동적 워커 스케일 상한선 (Spot-A 단일 타입 최대 30대 제한)
    MAX_SPOT_SCALE = 30
    
    # 초기 컨테이너 대수 세팅 (Compose 기본 스펙 기준)
    # docker-compose.yml에서 spot 워커(worker-2, 3)는 주석 처리되어 있으므로 초기 기동 대수는 0대입니다.
    current_worker_2_scale = 0
    
    # 룰 기반 스케일인 타이머
    empty_queue_duration = 0.0
    
    while True:
        time.sleep(1.0)  # 1초 주기 의사결정 루프
        
        # --- 1. DEAD 노드 헬스체크 및 격리 제거 ---
        current_time = time.time()
        dead_workers = []
        with registry_lock:
            # wid = worker id / info 정보 내역 중에서 last_heartbeat 값 참고
            for wid, info in list(worker_registry.items()):
                # 하트비트 수신이 15.0초 동안 끊어지면 사망 판정
                if current_time - info["last_heartbeat"] > 15.0:
                    dead_workers.append(wid)
            for wid in dead_workers:
                print(f"[Scheduler GCS] [DEAD 노드 감지] {wid} 노드가 오프라인 처리되었습니다.")
                del worker_registry[wid] # dead 된 워커의 정보를 레지스트리에서 완전히 삭제 

        # --- 2. 주기적 랜덤 가상 태스크 자동 생성 및 큐 투입 (시뮬레이터 구동용) ---
        # 매 루프마다 80% 확률로 1~5개의 대량 태스크가 난수로 유입되어 부하를 극대화합니다.
        if random.random() < 0.8:
            num_new_tasks = random.randint(1, 5)
            with queue_lock:
                # 큐 최대 제한을 50개로 확장하여 지속적인 병목 상태 유도
                if len(task_queue) < 50:
                    for _ in range(num_new_tasks):
                        task_counter += 1
                        task_id = f"task-{task_counter:04d}"
                        model = random.choice(model_types)
                        epochs = random.randint(5, 12)  # 에포크 수 난수화 (5~12 Epochs)
                        timeout = random.randint(15, 35)  # 15~35초의 짧은 마감기한으로 스케일아웃을 강력 유도
                        deadline = time.time() + timeout
                        task_queue.append({
                            "task_id": task_id,
                            "model_type": model,
                            "epochs": epochs,
                            "deadline": deadline,
                            "enqueue_time": time.time()
                        })
                        print(f"[Task 유입] {task_id} ({model}, {epochs} Epochs) 큐 적재 완료. (마감기한: {timeout}초 후)")

        # --- 2.5. 룰 기반 Scale-In 감지 (Queue=0 & Avg CPU < 20% 지속) ---
        with queue_lock:
            q_len_for_scale_in = len(task_queue)
            
        if q_len_for_scale_in == 0:
            with registry_lock:
                active_workers = list(worker_registry.values())
                if active_workers:
                    avg_cpu = sum(info.get("cpu", 0.0) for info in active_workers) / len(active_workers)
                else:
                    avg_cpu = 0.0
            
            if avg_cpu < 20.0:
                empty_queue_duration += 1.0
            else:
                empty_queue_duration = 0.0
        else:
            empty_queue_duration = 0.0
            
        if empty_queue_duration >= 10.0:
            # 10초 지속 시 스케일 인 작동 (자원 완전 회수)
            if current_worker_2_scale > 0:
                print(f"[Auto Scaling] 룰 기반 Scale-In 작동 -> worker-2 (Spot-A) 1대 감축 시도")
                if scale_in_specific_worker("spot_a"):
                    current_worker_2_scale -= 1
                    empty_queue_duration = 0.0

        # --- 3. 다중 배정(Multi-Dispatch) 의사결정 서브 루프 ---
        while True:
            with queue_lock:
                q_len_real = len(task_queue)
            if q_len_real == 0:
                break
                
            q_len = min(q_len_real, 10)
            with registry_lock:
                w1_act = 1 if any(info["node_type"] == "on_demand" for info in worker_registry.values()) else 0
                w2_act = 1 if any(info["node_type"] == "spot_a" for info in worker_registry.values()) else 0
                active_bitmap = (w1_act * 1) + (w2_act * 2)
                
            # 예산 레벨 이산화
            budget_level = 0 if virtual_budget < 20.0 else (1 if virtual_budget < 70.0 else 2)
            state = (q_len, active_bitmap, budget_level)

            # --- 4. 행동 공간(Action Space) 가용 액션 필터링 ---
            available_actions = [3]  # HOLD(3)는 상시 가용
            
            with registry_lock:
                # 각 노드 타입별로 IDLE 상태인 워커가 최소 1개 이상 존재하고 메모리가 정상인 경우에만 허용
                if any(info["node_type"] == "on_demand" and info["status"] == "IDLE" and info.get("mem", 0.0) < 90.0 for info in worker_registry.values()):
                    available_actions.append(0)
                if any(info["node_type"] == "spot_a" and info["status"] == "IDLE" and info.get("mem", 0.0) < 90.0 for info in worker_registry.values()):
                    available_actions.append(1)
                    
            # 최대 스케일 한도 내에서 SCALE_OUT(4) 기동 허용
            if current_worker_2_scale < MAX_SPOT_SCALE:
                available_actions.append(4)

            # 가용한 행동이 오직 HOLD(3) 뿐이라면 더 분배할 자원이 없으므로 루프를 즉시 탈출
            if available_actions == [3]:
                break

            # Q-Learning 에이전트 액션 결정
            action = agent.choose_action(state, available_actions)

            # --- 5. 의사결정 액션 실행 제어 ---
            if action in [0, 1, 2]:
                # 태스크 할당 처리 (2번 액션이 들어올 경우 안전하게 spot_a로 우회 처리)
                target_type = ["on_demand", "spot_a", "spot_a"][action]
                with queue_lock:
                    target_task = task_queue.pop(0)  # FIFO 큐 선입선출
                    
                worker_id = None
                worker_info = None
                with registry_lock:
                    # 해당 타입에 해당하고 IDLE 상태이며 자원 한계 미만인 워커를 찾아 선점 (OOM 선제 회피)
                    for wid, info in worker_registry.items():
                        if info["node_type"] == target_type and info["status"] == "IDLE":
                            if info.get("mem", 0.0) >= 90.0:
                                print(f"[OOM 선제 회피] 워커 '{wid}'의 메모리가 임계치를 초과({info['mem']}%)하여 할당에서 차단합니다.")
                                continue
                            worker_id = wid
                            worker_info = info.copy()
                            worker_registry[wid]["status"] = "BUSY"
                            break
                
                if worker_info:
                    # 비동기 스레드를 실행하여 gRPC 작업 전달 및 갱신 수행
                    threading.Thread(
                        target=run_task_on_worker,
                        args=(worker_id, worker_info, target_task, state, action),
                        daemon=True
                    ).start()
                else:
                    # 노드가 갑자기 끊겼거나 할당 불가능한 상황인 경우 작업을 다시 큐로 반환하고 루프 탈출
                    with queue_lock:
                        task_queue.insert(0, target_task)
                    break
                
            elif action == 3:
                # HOLD 액션: 대기 페널티를 받으며 틱 마감
                print(f"[Scheduler Action] HOLD 상태 선택 (대기열 크기: {q_len} | 대기 페널티 발생 가능)")
                break
                
            elif action == 4:
                # SCALE_OUT 액션: Docker SDK를 통한 Spot 컨테이너 동적 증설 트리거 (워커 구동을 기다리기 위해 루프 탈출)
                if current_worker_2_scale < MAX_SPOT_SCALE:
                    print(f"[Scheduler Action] SCALE_OUT 트리거 -> worker-2 (Spot-A) 대수 증설 지시")
                    if scale_out_worker("spot_a"):
                        current_worker_2_scale += 1
                break


# --- 6. Head Node 메인 구동 루프 ---

def serve():
    # 0. 잔존 좀비 컨테이너 비동기 청소 (부팅 블로킹 방지 및 자원 회수 보장)
    threading.Thread(target=cleanup_zombie_containers, daemon=True).start()
    
    port = os.environ.get("HEAD_PORT", str(DEFAULT_HEAD_PORT))
    
    # gRPC 서버 기동 (동시 접속 스레드풀 설정)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    babyray_pb2_grpc.add_BabyRayServiceServicer_to_server(BabyRayHeadServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"=== [Head] Baby Ray 마스터 Node gRPC 서버 기동 완료 (포트: {port}) ===")
    
    # 백그라운드 Q-Learning 의사결정 스케줄러 스레드 기동
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()
    
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        print("[Head] 서버 종료 시퀀스를 구동합니다...")
        server.stop(0)

if __name__ == '__main__':
    serve()
