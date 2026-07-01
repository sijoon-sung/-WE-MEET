import time
import os
import sys
import random
import threading
import grpc

# 실행 시 프로젝트 루트 디렉토리를 sys.path에 추가 
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from proto import babyray_pb2
from proto import babyray_pb2_grpc
from head.q_learning.agent import QLearningAgent

# state 모듈을 gcs_state라는 별칭으로 임포트하여 로컬 변수 state와 충돌하지 않게 함
import head.state as gcs_state # gcs state (변수, 인메모리 캐시 모음), q-learning state와의 차별을 두기 위해서
import head.cluster_manager as cluster_manager
import head.dashboard.server as dashboard

from head.scheduler.static import run_static_scheduler_step
from head.scheduler.dynamic import run_dynamic_scheduler_step
from head.q_learning.scheduler import run_qlearning_scheduler_step

# Q-Learning 에이전트
# cost.yaml의 경로를 찾음 (head에서 ../../common/cost_model.yaml -> 부모 디렉토리 common 폴더의 cost_model.yaml)
COST_MODEL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../common/cost_model.yaml'))
agent = QLearningAgent(cost_model_path=COST_MODEL_PATH)
# 에이전트는 cost_model.yaml에 정의된 비용 모델을 참고해서 학습하거나 행동을 결정하게 됩니다.

def run_task_on_worker(worker_id, worker_info, task, state, action):
    """
    [Task 실행 및 강화학습 피드백 스레드]
    특정 워커에 작업을 할당하여 gRPC로 실행 지시를 내리고 완료 모니터링 후 보상(Reward)을 계산하여 Q-Table을 갱신합니다.

    Args:
        worker_id (str): 작업을 배정할 대상 워커 식별자 ID.
        worker_info (dict): 대상 워커의 GCS IP/포트/타입 등의 정보 딕셔너리.
        task (dict): 실행할 태스크 정보 (task_id, model_type, epochs, deadline 등).
        state (tuple): 스케줄링 당시의 환경 상태 (t_profile, w_active, p_spot, budget_level).
        action (int): 스케줄러가 결정했던 행동 ID.
    """
    # worker_registry -> worker_info를 뽑아서 줌
    ip = worker_info['ip']
    worker_address = f"[{ip}]:{worker_info['port']}" if ":" in ip else f"{ip}:{worker_info['port']}"
    task_id = task["task_id"]
    model_type = task["model_type"]
    epochs = task["epochs"]
    worker_type = worker_info["node_type"]
    
    dashboard.log_event(f"[Scheduler Action] >>> 작업 할당 실행: {task_id} ({model_type}) -> 워커 '{worker_id}' ({worker_type})")
    
    # 워커 상태를 BUSY로 마킹하여 중복 할당 방지
    with gcs_state.registry_lock:
        if worker_id in gcs_state.worker_registry:
            gcs_state.worker_registry[worker_id]["status"] = "BUSY"
            
    success = False
    execution_time = 0.0
    
    try:
        # 워커 gRPC 채널 오픈 / stub 파일 생성

        # 워커의 주소(IP:Port)로 gRPC 통신을 위한 '파이프(채널)'를 연결
        channel = grpc.insecure_channel(worker_address)
        # 내 PC에 있는 함수처럼 쉽게 호출할 수 있게 해주는 '리모컨(Stub)' 객체를 생성
        stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
        
        # 1. 작업 개시 전송 (체크포인트/FedAvg 결합 매핑 확장)
        start_time = time.time()
        dataset_path = task.get("dataset_path", f"data/{model_type.lower()}_dataset.pt")
        if model_type.upper() == "REDUCE":
            job_id = task.get("job_id", "")
            dataset_path = f"merge:data/final_{job_id}-map-1.pt,data/final_{job_id}-map-2.pt,data/final_{job_id}-map-3.pt"
            
        result = stub.AssignTask(babyray_pb2.TaskAssignment(
            task_id=task_id,
            model_type=model_type,
            dataset_path=dataset_path,
            epochs=epochs
        ))
        
        if result.status == "RUNNING":
            # 2. 완료 여부 실시간 폴링 감시
            while True:
                time.sleep(1.5)
                
                # 워커가 죽어 오프라인 처리된 경우 통신 예외 발생 유도
                with gcs_state.registry_lock:
                    if worker_id not in gcs_state.worker_registry:
                        raise grpc.RpcError("Worker node went offline during task execution.")
                
                # 작업을 진행률을 받기
                status_res = stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=task_id))
                # worker가 응답한 상태를 체크
                if status_res.status in ["SUCCESS", "COMPLETED"]:
                    success = True
                    execution_time = time.time() - start_time
                    dashboard.log_event(f"[Scheduler Feedback] 작업 {task_id} 완료 성공! (실제 수행 시간: {execution_time:.2f}초)")
                    break
            
                elif status_res.status == "FAILED":
                    success = False
                    execution_time = time.time() - start_time
                    dashboard.log_event(f"[Scheduler Feedback] 경고: 작업 {task_id} 연산 실패 리포트 수신.")
                    break
        else:
            dashboard.log_event(f"[Scheduler Feedback] 작업 개시 거부당함: {result.message}")
            
    except grpc.RpcError as e:
        dashboard.log_event(f"[Scheduler Feedback 에러] 워커 '{worker_id}' 실행 중 통신 크래시 감지: {e}")
        success = False
        execution_time = time.time() - task["enqueue_time"]
    finally:
        # GCS 워커 노드 상태 복구
        with gcs_state.registry_lock:
            if worker_id in gcs_state.worker_registry:
                gcs_state.worker_registry[worker_id]["status"] = "IDLE"
                
        # --- 3. Q-Learning 보상 산출 및 Q-Table 업데이트 피드백 단계 ---
        end_time = time.time()
        delay_time = max(0.0, end_time - task["deadline"])
        deadline_exceeded = end_time > task["deadline"]
        
        # 가상 예산 차감
        cost_profile = agent.nodes_config.get(worker_type, {"cost_per_hour": 0.0})
        cost_per_hour = cost_profile.get("cost_per_hour", 0.0)
        task_cost = cost_per_hour * (execution_time / 3600.0)
        gcs_state.virtual_budget -= task_cost
        
        if gcs_state.SCHEDULER_MODE == "q_learning" and state is not None and action is not None:
            # 보상 수식 적용
            reward = agent.calculate_reward(
                success=success,
                execution_time=execution_time,
                worker_type=worker_type,
                delay_time=delay_time,
                deadline_exceeded=deadline_exceeded
            )
            
            # 큐 상태 갱신 후 다음 상태 추출
            with gcs_state.queue_lock:
                cnn_count = sum(1 for t in gcs_state.task_queue if t.get("model_type") == "CNN")
                lstm_rnn_count = sum(1 for t in gcs_state.task_queue if t.get("model_type") in ["LSTM", "RNN"])
            t_profile_next = 0 if cnn_count > lstm_rnn_count else 1
                
            with gcs_state.registry_lock:
                w1_idle = 1 if any(info["node_type"] == "on_demand" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()) else 0
                w2_idle = 1 if any(info["node_type"] == "spot_a" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()) else 0
            w_active_next = (w1_idle * 1) + (w2_idle * 2)
                
            p_spot_next = 1 if (time.time() % 30.0) < 10.0 else 0
            budget_level_next = 0 if gcs_state.virtual_budget < 0.2 else (1 if gcs_state.virtual_budget < 0.7 else 2)
            next_state = (t_profile_next, w_active_next, p_spot_next, budget_level_next)
            
            # Bellman Equation에 입각해 Q-Value 업데이트
            agent.update_q_value(state, action, reward, next_state)
            agent.save_q_table()
            
            dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} | Reward={reward:.4f} | NextState={next_state} | Epsilon={agent.epsilon:.4f}")
            dashboard.log_event(f"[Q-Learning Update] 잔여 가상 예산: ${gcs_state.virtual_budget:.4f}달러")
        else:
            dashboard.log_event(f"[Resource Spend] [Mode: {gcs_state.SCHEDULER_MODE}] 비용 차감: ${task_cost:.4f} | 잔여 예산: ${gcs_state.virtual_budget:.4f}")
        
        # 만약 실패했다면 Task Lineage 자가 복구를 위해 복구 태스크(가중치 상속) 큐 재삽입
        if not success:
            last_epoch = 0
            checkpoint_file = None
            for ep in range(epochs, 0, -1):
                chk_path = f"data/checkpoint_{task_id}_epoch_{ep}.pt"
                if os.path.exists(chk_path):
                    last_epoch = ep
                    checkpoint_file = chk_path
                    break
            
            if last_epoch > 0 and last_epoch < epochs:
                remaining_epochs = epochs - last_epoch
                dashboard.log_event(f"[장애 복구] 작업 {task_id} 중단 감지 -> {last_epoch} Epoch 가중치를 기반으로 이어서 학습 복구(남은 {remaining_epochs} Epochs) 대기 큐 재할당.")
                task["dataset_path"] = checkpoint_file
                task["epochs"] = remaining_epochs
            else:
                dashboard.log_event(f"[장애 복구] 작업 {task_id} 장애 유실 감지 -> 복구를 위해 대기 큐 재할당 (처음부터 재학습).")
                
            with gcs_state.queue_lock:
                gcs_state.task_queue.insert(0, task)


def get_next_runnable_task():
    """
    태스크 큐에서 실행 가능한(DAG 의존성이 충족된) 첫 번째 태스크를 꺼내어 반환합니다.
    """
    with gcs_state.queue_lock:
        for i, task in enumerate(gcs_state.task_queue):
            deps_met = True
            for dep in task.get("dependencies", []):
                if not gcs_state.completed_tasks_cache.get(dep, False):
                    deps_met = False
                    break
            if deps_met:
                return gcs_state.task_queue.pop(i)
    return None

def get_current_spot_scale():
    """GCS 워커 레지스트리 내 spot_a 노드의 현재 대수를 카운트합니다."""
    with gcs_state.registry_lock:
        return sum(1 for info in gcs_state.worker_registry.values() if info["node_type"] == "spot_a")

# --- 5. 백그라운드 스케줄러 핵심 루프 ---

def scheduler_loop():
    """
    [백그라운드 Q-Learning 의사결정 스케줄러 핵심 루프]
    1초 주기로 돌면서 DEAD 노드를 검출 및 회수하고, 주기적인 가상 태스크를 생성하며,
    Q-Learning 정책(Epsilon-Greedy 및 Action Masking)에 따라 작업을 가용 워커에 다중 분배(Multi-Dispatch)합니다.
    """
    dashboard.log_event("[Scheduler] Q-Learning 비용/SLA 인지형 의사결정 엔진 가동 성공.")
    
    model_types = ["CNN", "RNN", "LSTM"]
    
    # 최대 동적 워커 스케일 상한선 (Spot-A/B 혼합 최대 5대 제한)
    MAX_SPOT_SCALE = 5
    
    # 초기 컨테이너 대수 세팅 (Compose 기본 스펙 기준)
    # docker-compose.yml에서 spot 워커(worker-2, 3)는 주석 처리되어 있으므로 초기 기동 대수는 0대입니다.
    current_worker_2_scale = 0
    
    # 타이머 초기화
    empty_queue_duration = 0.0
    scale_in_timer = 0.0
    
    while True:
        time.sleep(1.0)  # 1초 주기 의사결정 루프
        
        # --- 1. DEAD 노드 헬스체크 및 격리 제거 ---
        current_time = time.time()
        dead_workers = []
        with gcs_state.registry_lock:
            for wid, info in list(gcs_state.worker_registry.items()):
                if current_time - info["last_heartbeat"] > 15.0:
                    dead_workers.append(wid)
            for wid in dead_workers:
                dashboard.log_event(f"[Scheduler GCS] [DEAD 노드 감지] {wid} 노드가 오프라인 처리되었습니다.")
                del gcs_state.worker_registry[wid]
 
        # Docker 컨테이너 실질적 회수
        for wid in dead_workers:
            if wid.startswith("worker-2-") or wid.startswith("worker-3-"):
                container_ref = f"babyray-{wid}"
                try:
                    if gcs_state.DOCKER_CLIENT is not None:
                        container = gcs_state.DOCKER_CLIENT.containers.get(container_ref)
                        dashboard.log_event(f"[Docker SDK] DEAD 컨테이너 회수 시작: {container_ref}")
                        container.stop(timeout=2)
                        container.remove()
                        dashboard.log_event(f"[Docker SDK] DEAD 컨테이너 회수 성공: {container_ref}")
                except Exception as e:
                    dashboard.log_event(f"[Docker SDK 경고] DEAD 컨테이너 {container_ref} 회수 실패: {e}")
 
        # --- 2. 주기적 랜덤 가상 태스크 자동 생성 및 큐 투입 (시뮬레이터 구동용) ---
        if random.random() < 0.4:
            num_new_tasks = random.randint(1, 2)
            with gcs_state.queue_lock:
                if len(gcs_state.task_queue) < 15:
                    for _ in range(num_new_tasks):
                        gcs_state.task_counter += 1
                        task_id = f"task-{gcs_state.task_counter:04d}"
                        model = random.choice(model_types)
                        epochs = random.randint(5, 10)
                        timeout = random.randint(25, 45)
                        deadline = time.time() + timeout
                        gcs_state.task_queue.append({
                            "task_id": task_id,
                            "model_type": model,
                            "epochs": epochs,
                            "deadline": deadline,
                            "enqueue_time": time.time()
                        })
                        dashboard.log_event(f"[Task 유입] {task_id} ({model}, {epochs} Epochs) 큐 적재 완료. (마감기한: {timeout}초 후)")
 
        # --- 3. 각 모드별 의사결정 서브 모듈 위임 ---
        if gcs_state.SCHEDULER_MODE == "static":
            scale_in_timer = run_static_scheduler_step(
                MAX_SPOT_SCALE,
                scale_in_timer,
                run_task_on_worker,
                get_next_runnable_task,
                get_current_spot_scale
            )
        elif gcs_state.SCHEDULER_MODE == "dynamic":
            scale_in_timer = run_dynamic_scheduler_step(
                MAX_SPOT_SCALE,
                scale_in_timer,
                run_task_on_worker,
                get_next_runnable_task,
                get_current_spot_scale
            )
        elif gcs_state.SCHEDULER_MODE == "q_learning":
            empty_queue_duration = run_qlearning_scheduler_step(
                MAX_SPOT_SCALE,
                empty_queue_duration,
                agent,
                run_task_on_worker,
                get_next_runnable_task,
                get_current_spot_scale
            )
