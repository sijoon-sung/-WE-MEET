# ==============================================================================
# WE-MEET: Q-Learning 기반 지능형 비용/SLA 최적화 스케줄링 모듈 (head/scheduler_qlearning.py)
# ==============================================================================

import time
import threading
import head.state as gcs_state
import head.cluster_manager as cluster_manager
import head.dashboard.server as dashboard

def run_qlearning_scheduler_step(MAX_SPOT_SCALE, empty_queue_duration, agent, run_task_on_worker, get_next_runnable_task, get_current_spot_scale):
    """
    Q-Learning 스케줄러의 1주기 의사결정 및 연산 할당 작업을 수행합니다.
    - 비용 및 SLA 저울질 행동 결정
    - 예산 부족 시 Action Masking 자동 가드
    """
    spot_scale = get_current_spot_scale()
    
    # 1. 룰 기반 Scale-In 작동 보완 (유휴 감지 회수)
    with gcs_state.queue_lock:
        q_len_for_scale_in = len(gcs_state.task_queue)
    if q_len_for_scale_in == 0:
        with gcs_state.registry_lock:
            active_workers = list(gcs_state.worker_registry.values())
            avg_cpu = sum(info.get("cpu", 0.0) for info in active_workers) / len(active_workers) if active_workers else 0.0
        if avg_cpu < 20.0:
            empty_queue_duration += 1.0
        else:
            empty_queue_duration = 0.0
    else:
        empty_queue_duration = 0.0
        
    if empty_queue_duration >= 10.0 and spot_scale > 0:
        dashboard.log_event("[Q-Learning Scale-In] 무부하 10초 유지로 인한 Spot 노드 안전 회수")
        if cluster_manager.scale_in_specific_worker("spot_a"):
            spot_scale -= 1
            empty_queue_duration = 0.0

    # 2. Q-Learning 의사결정 루프
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
            
        budget_level = 0 if gcs_state.virtual_budget < 20.0 else (1 if gcs_state.virtual_budget < 70.0 else 2)
        state = (q_len, active_bitmap, budget_level)

        # 가용 액션 설정
        available_actions = [2]  # HOLD
        
        peek_task = None
        with gcs_state.queue_lock:
            for task in gcs_state.task_queue:
                deps_met = True
                for dep in task.get("dependencies", []):
                    if not gcs_state.completed_tasks_cache.get(dep, False):
                        deps_met = False
                        break
                if deps_met:
                    peek_task = task
                    break
                    
        if peek_task:
            with gcs_state.registry_lock:
                if any(info["node_type"] == "on_demand" and info["status"] == "IDLE" and info.get("mem", 0.0) < 90.0 for info in gcs_state.worker_registry.values()):
                    available_actions.append(0)
                if any(info["node_type"] == "spot_a" and info["status"] == "IDLE" and info.get("mem", 0.0) < 90.0 for info in gcs_state.worker_registry.values()):
                    available_actions.append(1)
                    
        if spot_scale < MAX_SPOT_SCALE:
            available_actions.append(3)

        # Action Masking
        if gcs_state.virtual_budget <= 0.0:
            if 0 in available_actions and 1 in available_actions:
                available_actions.remove(0)
            if 3 in available_actions:
                available_actions.remove(3)

        if available_actions == [2]:
            break

        action = agent.choose_action(state, available_actions)

        if action in [0, 1]:
            target_type = ["on_demand", "spot_a"][action]
            target_task = get_next_runnable_task()
            
            if not target_task:
                break
                
            worker_id = None
            worker_info = None
            with gcs_state.registry_lock:
                for wid, info in gcs_state.worker_registry.items():
                    if info["node_type"] == target_type and info["status"] == "IDLE" and info.get("mem", 0.0) < 90.0:
                        worker_id = wid
                        worker_info = info.copy()
                        gcs_state.worker_registry[wid]["status"] = "BUSY"
                        break
            
            if worker_info:
                threading.Thread(
                    target=run_task_on_worker,
                    args=(worker_id, worker_info, target_task, state, action),
                    daemon=True
                ).start()
            else:
                with gcs_state.queue_lock:
                    gcs_state.task_queue.insert(0, target_task)
                break
            
        elif action == 2:
            dashboard.log_event(f"[Q-Learning Action] HOLD 상태 선택 (대기열 크기: {q_len})")
            break
            
        elif action == 3:
            dashboard.log_event(f"[Q-Learning Action] SCALE_OUT 트리거 -> Spot 노드 추가 증설")
            if cluster_manager.scale_out_worker("spot_a"):
                spot_scale += 1
            break
            
    return empty_queue_duration
