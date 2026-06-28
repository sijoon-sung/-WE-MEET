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
from q_learning import QLearningAgent

# state 모듈을 gcs_state라는 별칭으로 임포트하여 로컬 변수 state와 충돌하지 않게 함
import state as gcs_state
import cluster_manager
import dashboard

# Q-Learning 에이전트
# cost.yaml의 경로를 찾음 (head에서 ../common/cost_model.yaml -> 부모 디렉토리 common 폴더의 cost_model.yaml)
COST_MODEL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../common/cost_model.yaml'))
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
        state (tuple): 스케줄링 당시의 환경 상태 (q_len, active_bitmap, budget_level).
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
        gcs_state.virtual_budget -= task_cost
        
        # 큐 상태 갱신 후 다음 상태 추출
        with gcs_state.queue_lock:
            q_len_next = min(len(gcs_state.task_queue), 10)
            
        with gcs_state.registry_lock:
            w1_act = 1 if any(info["node_type"] == "on_demand" for info in gcs_state.worker_registry.values()) else 0
            w2_act = 1 if any(info["node_type"] == "spot_a" for info in gcs_state.worker_registry.values()) else 0
            active_bitmap_next = (w1_act * 1) + (w2_act * 2)
            
        budget_level_next = 0 if gcs_state.virtual_budget < 20.0 else (1 if gcs_state.virtual_budget < 70.0 else 2)
        next_state = (q_len_next, active_bitmap_next, budget_level_next)
        
        # Bellman Equation에 입각해 Q-Value 업데이트
        agent.update_q_value(state, action, reward, next_state)
        agent.save_q_table()
        
        dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} | Reward={reward:.4f} | NextState={next_state} | Epsilon={agent.epsilon:.4f}")
        dashboard.log_event(f"[Q-Learning Update] 잔여 가상 예산: ${gcs_state.virtual_budget:.4f}달러")
        
        # 만약 실패했다면 Task Lineage 자가 복구를 위해 큐 최전방에 작업을 재삽입
        if not success:
            dashboard.log_event(f"[장애 복구] 작업 {task_id} 장애 유실 감지 -> 복구를 위해 대기 큐 재할당.")
            with gcs_state.queue_lock:
                gcs_state.task_queue.insert(0, task)


# --- 5. 백그라운드 Q-Learning 스케줄러 핵심 루프 ---

def scheduler_loop():
    """
    [백그라운드 Q-Learning 의사결정 스케줄러 핵심 루프]
    1초 주기로 돌면서 DEAD 노드를 검출 및 회수하고, 주기적인 가상 태스크를 생성하며,
    Q-Learning 정책(Epsilon-Greedy 및 Action Masking)에 따라 작업을 가용 워커에 다중 분배(Multi-Dispatch)합니다.
    """
    dashboard.log_event("[Scheduler] Q-Learning 비용/SLA 인지형 의사결정 엔진 가동 성공.")
    
    model_types = ["CNN", "RNN", "LSTM"]
    
    # 최대 동적 워커 스케일 상한선 (Spot-A 단일 타입 최대 3대 제한 - 호스트 OOM 방지를 위해 30대에서 3대로 축소 조정)
    MAX_SPOT_SCALE = 3
    
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
        with gcs_state.registry_lock:
            # wid = worker id / info 정보 내역 중에서 last_heartbeat 값 참고
            for wid, info in list(gcs_state.worker_registry.items()):
                # 하트비트 수신이 15.0초 동안 끊어지면 사망 판정
                if current_time - info["last_heartbeat"] > 15.0:
                    dead_workers.append(wid)
            for wid in dead_workers:
                dashboard.log_event(f"[Scheduler GCS] [DEAD 노드 감지] {wid} 노드가 오프라인 처리되었습니다.")
                del gcs_state.worker_registry[wid] # dead 된 워커의 정보를 레지스트리에서 완전히 삭제 
 
        # Docker 컨테이너 실질적 회수 (락 바깥에서 진행하여 블로킹 방지)
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
                    dashboard.log_event(f"[Docker SDK 경고] DEAD 컨테이너 {container_ref} 회수 실패 (이미 종료되었거나 접근 불가능): {e}")
 
        # --- 2. 주기적 랜덤 가상 태스크 자동 생성 및 큐 투입 (시뮬레이터 구동용) ---
        # 매 루프마다 40% 확률로 1~2개의 적절한 태스크가 난수로 유입되어 부하를 조절합니다. (호스트 과부하 OOM 예방 목적)
        if random.random() < 0.4:
            num_new_tasks = random.randint(1, 2)
            with gcs_state.queue_lock:
                # 큐 최대 제한을 15개로 변경하여 안정성 도출
                if len(gcs_state.task_queue) < 15:
                    for _ in range(num_new_tasks):
                        gcs_state.task_counter += 1
                        task_id = f"task-{gcs_state.task_counter:04d}"
                        model = random.choice(model_types)
                        epochs = random.randint(5, 10)  # 에포크 수 난수화 (5~10 Epochs)
                        timeout = random.randint(25, 45)  # 25~45초의 마감기한
                        deadline = time.time() + timeout
                        gcs_state.task_queue.append({
                            "task_id": task_id,
                            "model_type": model,
                            "epochs": epochs,
                            "deadline": deadline,
                            "enqueue_time": time.time()
                        })
                        dashboard.log_event(f"[Task 유입] {task_id} ({model}, {epochs} Epochs) 큐 적재 완료. (마감기한: {timeout}초 후)")
 
        # --- 2.5. 룰 기반 Scale-In 감지 (Queue=0 & Avg CPU < 20% 지속) ---
        with gcs_state.queue_lock:
            q_len_for_scale_in = len(gcs_state.task_queue)
            
        if q_len_for_scale_in == 0:
            with gcs_state.registry_lock:
                active_workers = list(gcs_state.worker_registry.values())
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
                dashboard.log_event(f"[Scale-In 감지] 시스템 부하 극소 상태 10초 유지 -> Spot 노드 회수 절차 개시")
                if cluster_manager.scale_in_specific_worker("spot_a"):
                    current_worker_2_scale -= 1
                    empty_queue_duration = 0.0
 
        # --- 3. 다중 배정(Multi-Dispatch) 의사결정 서브 루프 ---
        while True:
            with gcs_state.queue_lock:
                q_len_real = len(gcs_state.task_queue)
            if q_len_real == 0:
                break
                
            q_len = min(q_len_real, 10)
            with gcs_state.registry_lock:
                w1_act = 1 if any(info["node_type"] == "on_demand" for info in gcs_state.worker_registry.values()) else 0
                w2_act = 1 if any(info["node_type"] == "spot_a" for info in gcs_state.worker_registry.values()) else 0
                active_bitmap = (w1_act * 1) + (w2_act * 2)
                
            # 예산 레벨 이산화
            budget_level = 0 if gcs_state.virtual_budget < 20.0 else (1 if gcs_state.virtual_budget < 70.0 else 2)
            state = (q_len, active_bitmap, budget_level)
 
            # --- 4. 행동 공간(Action Space) 가용 액션 필터링 ---
            # 0: OD, 1: Spot-A, 2: HOLD, 3: SCALE_OUT
            available_actions = [2]  # HOLD(2)는 상시 가용
            
            with gcs_state.registry_lock:
                # 각 노드 타입별로 IDLE 상태인 워커가 최소 1개 이상 존재하고 메모리가 정상인 경우에만 허용
                if any(info["node_type"] == "on_demand" and info["status"] == "IDLE" and info.get("mem", 0.0) < 90.0 for info in gcs_state.worker_registry.values()):
                    available_actions.append(0)
                if any(info["node_type"] == "spot_a" and info["status"] == "IDLE" and info.get("mem", 0.0) < 90.0 for info in gcs_state.worker_registry.values()):
                    available_actions.append(1)
                    
            # 최대 스케일 한도 내에서 SCALE_OUT(3) 기동 허용
            if current_worker_2_scale < MAX_SPOT_SCALE:
                available_actions.append(3)
 
            # 가상 예산 고갈 시 행동 제약 (Action Masking)
            if gcs_state.virtual_budget <= 0.0:
                # 예산이 고갈된 경우 On-demand 할당(Action 0)과 스케일 아웃(Action 3)을 억제
                if 0 in available_actions and 1 in available_actions:
                    available_actions.remove(0)  # Spot-A(Action 1)가 가능하면 On-demand 차단
                if 3 in available_actions:
                    available_actions.remove(3)  # 추가 스케일아웃 차단 (HOLD 또는 기존 워커 활용 유도)
 
            # 가용한 행동이 오직 HOLD(2) 뿐이라면 더 분배할 자원이 없으므로 루프를 즉시 탈출
            if available_actions == [2]:
                break
 
            # Q-Learning 에이전트 액션 결정
            action = agent.choose_action(state, available_actions)
 
            # --- 5. 의사결정 액션 실행 제어 ---
            if action in [0, 1]:
                # 태스크 할당 처리 (0: on_demand, 1: spot_a)
                target_type = ["on_demand", "spot_a"][action]
                with gcs_state.queue_lock:
                    target_task = gcs_state.task_queue.pop(0)  # FIFO 큐 선입선출
                    
                worker_id = None
                worker_info = None
                with gcs_state.registry_lock:
                    # 해당 타입에 해당하고 IDLE 상태이며 자원 한계 미만인 워커를 찾아 선점 (OOM 선제 회피)
                    for wid, info in gcs_state.worker_registry.items():
                        if info["node_type"] == target_type and info["status"] == "IDLE":
                            if info.get("mem", 0.0) >= 90.0:
                                dashboard.log_event(f"[OOM 선제 회피] 워커 '{wid}'의 메모리가 임계치를 초과({info['mem']}%)하여 할당에서 차단합니다.")
                                continue
                            worker_id = wid
                            worker_info = info.copy()
                            gcs_state.worker_registry[wid]["status"] = "BUSY"
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
                    with gcs_state.queue_lock:
                        gcs_state.task_queue.insert(0, target_task)
                    break
                
            elif action == 2:
                # HOLD 액션: 대기 페널티를 받으며 틱 마감
                dashboard.log_event(f"[Scheduler Action] HOLD 상태 선택 (대기열 크기: {q_len} | 대기 페널티 발생 가능)")
                break
                
            elif action == 3:
                # SCALE_OUT 액션: Docker SDK를 통한 Spot 컨테이너 동적 증설 트리거 (워커 구동을 기다리기 위해 루프 탈출)
                if current_worker_2_scale < MAX_SPOT_SCALE:
                    dashboard.log_event(f"[Scheduler Action] SCALE_OUT 트리거 -> worker-2 (Spot-A) 대수 증설 지시")
                    if cluster_manager.scale_out_worker("spot_a"):
                        current_worker_2_scale += 1
                break
