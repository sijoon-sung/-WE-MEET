# ==============================================================================
# WE-MEET: Head Node gRPC 서버 및 Q-Learning 스케줄러 통합 구현 (head/head.py)
# ==============================================================================

import grpc
from concurrent import futures
import time
import os
import sys
import threading
import random
import subprocess

# 실행 시 프로젝트 루트 디렉토리를 sys.path에 추가하여 proto 패키지를 정상적으로 찾을 수 있도록 설정합니다.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from proto import babyray_pb2
from proto import babyray_pb2_grpc
from common.config import DEFAULT_HEAD_PORT

# Q-Learning 의사결정 에이전트 가져오기
from q_learning import QLearningAgent

# Docker SDK 임포트 시도 (에러 발생 시 CLI 폴백 및 경고 출력)
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
worker_registry = {}
registry_lock = threading.Lock()

# 가상 태스크 대기열 (Task Queue)
task_queue = []
queue_lock = threading.Lock()

# 전역 가상 자산 관리 변수
virtual_budget = 100.0  # 초기 가상 예산 $100.0달러
task_counter = 0

# Q-Learning 에이전트 인스턴스화
COST_MODEL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../common/cost_model.yaml'))
agent = QLearningAgent(cost_model_path=COST_MODEL_PATH)


# --- 2. Docker 가상 클러스터 동적 통제 API (Docker SDK & CLI) ---

def scale_workers(service_name, target_count):
    """
    [Docker SDK CLI API]
    호스트 PC의 Docker Compose CLI를 호출하여 해당 워커 노드 컨테이너의 활성 대수를 변경합니다.
    - SCALE_OUT 발생 시 target_count를 늘려 새 워커를 가동하고
    - SCALE_IN 발생 시 target_count를 줄여 유휴 노드를 회수합니다.
    """
    try:
        # docker-compose.yml 경로 추출 (head/../docker/docker-compose.yml)
        compose_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../docker/docker-compose.yml'))
        
        # subprocess.run()을 사용하여 백그라운드 쉘에서 도커 컴포즈 동적 스케일링 명령어를 실행시킵니다.
        cmd = [
            "docker", "compose",
            "-f", compose_path,
            "up", "-d",
            "--scale", f"{service_name}={target_count}"
        ]
        
        # subprocess를 이용해 명령 실행 후 출력과 결과를 반환받음
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"[Docker SDK CLI] 스케일링 완료 -> {service_name} 를 {target_count}대로 갱신.")
        return True
    except Exception as e:
        print(f"[Docker SDK CLI 에러] {service_name} 스케일링 실패: {e}")
        return False


def get_container_metrics(container_name):
    """
    [Docker SDK Resource Monitor API]
    Docker SDK 객체를 통해 해당 워커 컨테이너의 실시간 메모리/CPU 사용률 메트릭을 도출합니다.
    """
    if DOCKER_CLIENT is None:
        return 0.0, 0.0
        
    try:
        # 호스트 도커 데몬으로부터 가동 중인 컨테이너 정보를 로드합니다.
        container = DOCKER_CLIENT.containers.get(container_name)
        # 1회성 스냅샷 메트릭 수집
        stats = container.stats(stream=False)
        
        # CPU 연산률 산출
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})
        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
        num_cpus = cpu_stats.get("online_cpus", 1)
        
        cpu_util = 0.0
        if system_delta > 0 and cpu_delta > 0:
            cpu_util = (cpu_delta / system_delta) * num_cpus * 100.0
            
        # Memory 사용률 산출
        mem_stats = stats.get("memory_stats", {})
        mem_usage = mem_stats.get("usage", 0)
        mem_limit = mem_stats.get("limit", 1)
        mem_util = (mem_usage / mem_limit) * 100.0 if mem_limit > 0 else 0.0
        
        return round(cpu_util, 1), round(mem_util, 1)
    except Exception:
        # 컨테이너 미발견 혹은 윈도우 도커 비호환 대비 대체 더미값 반환
        return random.uniform(10.0, 30.0), random.uniform(30.0, 50.0)


# --- 3. Head Node gRPC 통신 서비스 핸들러 ---

class BabyRayHeadServicer(babyray_pb2_grpc.BabyRayServiceServicer):
    def RegisterWorker(self, request, context):
        peer = context.peer()
        # gRPC peer IP 주소 파싱 (IPv4 및 IPv6 호환)
        if peer.startswith("ipv4:"):
            ip = peer.split(":")[1]
        elif peer.startswith("ipv6:"):
            last_colon = peer.rfind(":")
            ip = peer[5:last_colon]
            ip = ip.replace("%5B", "").replace("%5D", "").replace("[", "").replace("]", "")
        else:
            ip = "127.0.0.1"
            
        if ip == "::1":
            ip = "127.0.0.1"
            
        with registry_lock:
            worker_registry[request.worker_id] = {
                "node_type": request.node_type.lower(),
                "ip": ip,
                "port": request.port,
                "last_heartbeat": time.time(),
                "cpu": 0.0,
                "mem": 0.0,
                "status": "IDLE"
            }
            print(f"[Head Registry] 워커 신규 등록: ID='{request.worker_id}' | 주소: {ip}:{request.port} | 타입: {request.node_type}")
            
        return babyray_pb2.RegisterResponse(
            success=True, 
            message=f"Worker '{request.worker_id}' registered successfully on Head GCS."
        )

    def DeregisterWorker(self, request, context):
        with registry_lock:
            if request.worker_id in worker_registry:
                del worker_registry[request.worker_id]
                print(f"[Head Registry] 워커 정상 퇴장: ID='{request.worker_id}'")
                return babyray_pb2.DeregisterResponse(success=True, message="Deregistered.")
            return babyray_pb2.DeregisterResponse(success=False, message="Worker not found.")

    def SendHeartbeat(self, request, context):
        # 도커 SDK를 통해 실시간 실제 컨테이너 리소스 부하를 동적으로 연계 조회하여 오버라이드
        container_name = f"babyray-{request.worker_id}"
        real_cpu, real_mem = get_container_metrics(container_name)
        
        with registry_lock:
            if request.worker_id in worker_registry:
                worker_registry[request.worker_id]["last_heartbeat"] = time.time()
                # SDK 실시간 자원량 값 주입 (실패 시 하트비트 전송자가 송신한 더미 값 반영)
                worker_registry[request.worker_id]["cpu"] = real_cpu if real_cpu > 0 else request.cpu_utilization
                worker_registry[request.worker_id]["mem"] = real_mem if real_mem > 0 else request.memory_utilization
                
        return babyray_pb2.HeartbeatResponse(ack=True)


# --- 4. 태스크 스레드 핸들러 및 Q-Learning 상태 피드백 루프 ---

def run_task_on_worker(worker_id, worker_info, task, state, action):
    """
    [Task 실행 및 강화학습 피드백 스레드]
    특정 워커에 작업을 할당하여 gRPC로 실행 지시를 내리고 완료 모니터링 후 보상(Reward)을 계산하여 Q-Table을 갱신합니다.
    """
    global virtual_budget
    
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
        # 워커 gRPC 채널 오픈
        channel = grpc.insecure_channel(worker_address)
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
                        
                status_res = stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=task_id))
                
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
            w1_act = 1 if "worker-1" in worker_registry else 0
            w2_act = 1 if "worker-2" in worker_registry else 0
            w3_act = 1 if "worker-3" in worker_registry else 0
            active_bitmap_next = (w1_act * 1) + (w2_act * 2) + (w3_act * 4)
            
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
    
    # 초기 컨테이너 대수 세팅 (Compose 기본 스펙 기준)
    current_worker_2_scale = 1
    current_worker_3_scale = 1
    
    while True:
        time.sleep(4.0)  # 4초 주기 의사결정 루프
        
        # --- 1. DEAD 노드 헬스체크 및 격리 제거 ---
        current_time = time.time()
        dead_workers = []
        with registry_lock:
            for wid, info in list(worker_registry.items()):
                # 하트비트 수신이 15.0초 동안 끊어지면 사망 판정
                if current_time - info["last_heartbeat"] > 15.0:
                    dead_workers.append(wid)
            for wid in dead_workers:
                print(f"[Scheduler GCS] [DEAD 노드 감지] {wid} 노드가 오프라인 처리되었습니다.")
                del worker_registry[wid]

        # --- 2. 주기적 랜덤 가상 태스크 자동 생성 및 큐 투입 (시뮬레이터 구동용) ---
        if random.random() < 0.6:  # 60% 확률로 태스크 유입 모사
            with queue_lock:
                if len(task_queue) < 10:
                    task_counter += 1
                    task_id = f"task-{task_counter:04d}"
                    model = random.choice(model_types)
                    # 5에포크 연산 지시, 마감 기한은 넉넉하게 20초~60초 랜덤 할당
                    deadline = time.time() + random.randint(20, 60)
                    task_queue.append({
                        "task_id": task_id,
                        "model_type": model,
                        "epochs": 5,
                        "deadline": deadline,
                        "enqueue_time": time.time()
                    })
                    print(f"[Task 유입] {task_id} ({model}) 큐 적재 완료. (마감기한: {random.randint(20, 60)}초 후)")

        # --- 3. 강화학습 상태(State) 계측 ---
        with queue_lock:
            q_len = min(len(task_queue), 10)
            
        with registry_lock:
            w1_act = 1 if "worker-1" in worker_registry else 0
            w2_act = 1 if "worker-2" in worker_registry else 0
            w3_act = 1 if "worker-3" in worker_registry else 0
            active_bitmap = (w1_act * 1) + (w2_act * 2) + (w3_act * 4)
            
        # 예산 레벨 이산화
        budget_level = 0 if virtual_budget < 20.0 else (1 if virtual_budget < 70.0 else 2)
        state = (q_len, active_bitmap, budget_level)

        if q_len == 0:
            # 대기 중인 작업이 없으면 의사결정 없이 대기
            continue

        # --- 4. 행동 공간(Action Space) 가용 액션 필터링 ---
        # 0: Assign W1, 1: Assign W2, 2: Assign W3, 3: Hold, 4: Scale Out
        available_actions = [3]  # HOLD(3)는 상시 가용
        
        with registry_lock:
            # 각 노드가 레지스트리에 켜져 있고(Active), 상태가 IDLE인 경우에만 태스크 배정 액션 허용
            if "worker-1" in worker_registry and worker_registry["worker-1"]["status"] == "IDLE":
                available_actions.append(0)
            if "worker-2" in worker_registry and worker_registry["worker-2"]["status"] == "IDLE":
                available_actions.append(1)
            if "worker-3" in worker_registry and worker_registry["worker-3"]["status"] == "IDLE":
                available_actions.append(2)
                
        # 최대 스케일 한도 내에서 SCALE_OUT(4) 기동 허용 (최대 Spot 각각 3대 제한)
        if current_worker_2_scale < 3 or current_worker_3_scale < 3:
            available_actions.append(4)

        # Q-Learning 에이전트 액션 결정
        action = agent.choose_action(state, available_actions)

        # --- 5. 의사결정 액션 실행 제어 ---
        if action in [0, 1, 2]:
            # 태스크 할당 처리
            target_worker = f"worker-{action + 1}"
            with queue_lock:
                target_task = task_queue.pop(0)  # FIFO 큐 선입선출
                
            worker_info = None
            with registry_lock:
                if target_worker in worker_registry:
                    worker_info = worker_registry[target_worker].copy()
            
            if worker_info:
                # 비동기 스레드를 실행하여 gRPC 작업 전달 및 갱신 수행
                threading.Thread(
                    target=run_task_on_worker,
                    args=(target_worker, worker_info, target_task, state, action),
                    daemon=True
                ).start()
            else:
                # 노드가 갑자기 끊긴 경우 작업을 다시 큐로 반환
                with queue_lock:
                    task_queue.insert(0, target_task)

        elif action == 3:
            # HOLD 액션: 대기
            print(f"[Scheduler Action] HOLD 상태 선택 (대기열 크기: {q_len} | 대기 페널티 발생 가능)")
            
        elif action == 4:
            # SCALE_OUT 액션: Docker compose를 통한 Spot 컨테이너 동적 증설 트리거
            # 큐 부하량이 많은 Spot 노드를 선정해 스케일아웃
            if current_worker_2_scale <= current_worker_3_scale and current_worker_2_scale < 3:
                current_worker_2_scale += 1
                print(f"[Scheduler Action] SCALE_OUT 트리거 -> worker-2 (Spot-A) 대수 증설 지시 ({current_worker_2_scale}대)")
                scale_workers("worker-2", current_worker_2_scale)
            elif current_worker_3_scale < 3:
                current_worker_3_scale += 1
                print(f"[Scheduler Action] SCALE_OUT 트리거 -> worker-3 (Spot-B) 대수 증설 지시 ({current_worker_3_scale}대)")
                scale_workers("worker-3", current_worker_3_scale)


# --- 6. Head Node 메인 구동 루프 ---

def serve():
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
