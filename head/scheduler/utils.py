import os
import sys
import time
import grpc
import threading
import random

# GCS 전역 인메모리 스토어 상태 임포트
import head.state as gcs_state
import head.dashboard.server as dashboard
from proto import babyray_pb2
from proto import babyray_pb2_grpc
from head.q_learning.agent import QLearningAgent

# [Q-Learning Agent 싱글톤 인스턴스 모듈화]
# - scheduler.py와 scheduler/core.py가 각각 생성하던 에이전트를 공통 유틸로 통합하여 
#   비용 모델과 학습 Q-Table 인스턴스의 메모리 정합성 및 일관성을 확보합니다.
COST_MODEL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../common/cost_model.yaml'))
agent = QLearningAgent(cost_model_path=COST_MODEL_PATH)

def run_task_on_worker(worker_id, worker_info, task, state, action):
    """
    [Task 실행 및 강화학습 피드백 스레드 (공통 유틸리티)]
    특정 워커에 작업을 할당하여 gRPC로 실행 지시를 내리고 완료 모니터링 후 보상(Reward)을 계산하여 Q-Table을 갱신합니다.
    epochs 가 8 이상이고 가용 IDLE 노드가 2대 이상일 경우 병렬 Map-Reduce 연산으로 확장 분할 구동합니다.
    """
    ip = worker_info['ip']
    worker_address = f"[{ip}]:{worker_info['port']}" if ":" in ip else f"{ip}:{worker_info['port']}"
    task_id = task["task_id"]
    success = False
    execution_time = 0.0
    start_time = time.time()
    
    try:
        model_type = task["model_type"]
        epochs = task["epochs"]
        
        # 현재 GCS에 등록된 가용 IDLE 워커 리스트 스캔
        with gcs_state.registry_lock:
            available_idle_workers = [
                (wid, info) for wid, info in gcs_state.worker_registry.items()
                if info["status"] == "IDLE" and wid != worker_id
            ]
            if worker_id in gcs_state.worker_registry:
                available_idle_workers.append((worker_id, gcs_state.worker_registry[worker_id]))
                
        is_map_reduce = (
            model_type.upper() in ["CNN", "RNN", "LSTM"]
            and epochs >= 8
            and len(available_idle_workers) >= 2
        )
        
        if is_map_reduce:
            # 맵-리듀스로 분배 연산이 진행되므로, 원래 이 스레드의 주체 워커 락을 풀어주어 맵/리듀스 풀에 참가시킵니다 (데드락 방지).
            with gcs_state.registry_lock:
                if worker_id in gcs_state.worker_registry:
                    gcs_state.worker_registry[worker_id]["status"] = "IDLE"

            dashboard.log_event(f"[Map-Reduce] {task_id} 병렬 학습 분할 개시. 가용 IDLE 워커 수: {len(available_idle_workers)}")
            
            # 1. 쪼갤 대수 결정 (최대 3분할)
            num_splits = min(len(available_idle_workers), 3)
            sub_epochs = epochs // num_splits
            remainder = epochs % num_splits
            
            map_threads = []
            map_results = {}
            
            selected_workers = available_idle_workers[:num_splits]
            with gcs_state.registry_lock:
                for wid, _ in selected_workers:
                    if wid in gcs_state.worker_registry:
                        gcs_state.worker_registry[wid]["status"] = "BUSY"
                        
            def execute_map_subtask(sub_idx, w_id, w_info):
                sub_task_id = f"{task_id}-map-{sub_idx}"
                sub_ep = sub_epochs + (remainder if sub_idx == 0 else 0)
                sub_ip = w_info['ip']
                sub_address = f"[{sub_ip}]:{w_info['port']}" if ":" in sub_ip else f"{sub_ip}:{w_info['port']}"
                
                dashboard.log_event(f"[Map Task] 서브맵 할당: {sub_task_id} ({model_type}, {sub_ep} Ep) -> 워커 {w_id}")
                
                sub_success = False
                sub_start = time.time()
                try:
                    sub_channel = grpc.insecure_channel(sub_address)
                    sub_stub = babyray_pb2_grpc.BabyRayServiceStub(sub_channel)
                    
                    sub_dataset_path = task.get("dataset_path", f"data/{model_type.lower()}_dataset.pt")
                    if task.get("dataset_path", "").endswith(".pt"):
                        sub_dataset_path = task["dataset_path"]
                    
                    res = sub_stub.AssignTask(babyray_pb2.TaskAssignment(
                        task_id=sub_task_id,
                        model_type=model_type,
                        dataset_path=sub_dataset_path,
                        epochs=sub_ep
                    ))
                    
                    if res.status == "RUNNING":
                        while True:
                            time.sleep(1.0)
                            with gcs_state.registry_lock:
                                if w_id not in gcs_state.worker_registry:
                                    raise grpc.RpcError("Worker went offline during subtask")
                                    
                            stat = sub_stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=sub_task_id))
                            if stat.status in ["SUCCESS", "COMPLETED"]:
                                sub_success = True
                                break
                            elif stat.status == "FAILED":
                                sub_success = False
                                break
                except Exception as ex:
                    dashboard.log_event(f"[Map Task 에러] 서브맵 {sub_task_id} (워커 {w_id}) 실패: {ex}")
                    sub_success = False
                finally:
                    with gcs_state.registry_lock:
                        if w_id in gcs_state.worker_registry:
                            gcs_state.worker_registry[w_id]["status"] = "IDLE"
                    
                    map_results[sub_idx] = {
                        "success": sub_success,
                        "worker_id": w_id,
                        "worker_type": w_info["node_type"],
                        "execution_time": time.time() - sub_start,
                        "output_file": f"data/final_{sub_task_id}.pt"
                    }
                    
            for idx, (wid, winfo) in enumerate(selected_workers):
                t = threading.Thread(target=execute_map_subtask, args=(idx, wid, winfo))
                t.start()
                map_threads.append(t)
                
            for t in map_threads:
                t.join()
                
            all_maps_success = len(map_results) == num_splits and all(r["success"] for r in map_results.values())
            
            if not all_maps_success:
                dashboard.log_event(f"[Map-Reduce] 경고: 일부 맵 태스크가 실패했습니다. 복구 복구 루프 재진입.")
                success = False
                execution_time = time.time() - start_time
            else:
                merge_files = [r["output_file"] for r in map_results.values()]
                merge_dataset_path = f"merge:" + ",".join(merge_files)
                reduce_task_id = f"{task_id}-reduce"
                
                with gcs_state.registry_lock:
                    reduce_candidates = [
                        (wid, info) for wid, info in gcs_state.worker_registry.items()
                        if info["status"] == "IDLE"
                    ]
                
                # Reduce는 회수(Eviction) 위험이 없는 안정적인 on_demand 노드로 전용 고정(Pinning)합니다.
                reduce_worker_id = None
                reduce_worker_info = None
                
                while True:
                    with gcs_state.registry_lock:
                        on_demand_candidates = [
                            (wid, info) for wid, info in gcs_state.worker_registry.items()
                            if info["node_type"] == "on_demand" and info["status"] == "IDLE"
                        ]
                    if on_demand_candidates:
                        reduce_worker_id, reduce_worker_info = on_demand_candidates[0]
                        break
                    else:
                        dashboard.log_event(f"[Reduce Task] 온디맨드 가용 IDLE 워커(worker-1) 대기 중...")
                        time.sleep(1.0)
                
                if reduce_worker_id:
                    
                    with gcs_state.registry_lock:
                        if reduce_worker_id in gcs_state.worker_registry:
                            gcs_state.worker_registry[reduce_worker_id]["status"] = "BUSY"
                            
                    reduce_success = False
                    reduce_ip = reduce_worker_info['ip']
                    reduce_address = f"[{reduce_ip}]:{reduce_worker_info['port']}" if ":" in reduce_ip else f"{reduce_ip}:{reduce_worker_info['port']}"
                    
                    dashboard.log_event(f"[Reduce Task] 병합 가중치 생성 트리거: {reduce_task_id} -> 워커 {reduce_worker_id}")
                    try:
                        r_channel = grpc.insecure_channel(reduce_address)
                        r_stub = babyray_pb2_grpc.BabyRayServiceStub(r_channel)
                        
                        r_res = r_stub.AssignTask(babyray_pb2.TaskAssignment(
                            task_id=task_id,
                            model_type="REDUCE",
                            dataset_path=merge_dataset_path,
                            epochs=1
                        ))
                        
                        if r_res.status == "RUNNING":
                            while True:
                                time.sleep(1.0)
                                with gcs_state.registry_lock:
                                    if reduce_worker_id not in gcs_state.worker_registry:
                                        raise grpc.RpcError("Reduce worker went offline")
                                        
                                r_stat = r_stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=task_id))
                                if r_stat.status in ["SUCCESS", "COMPLETED"]:
                                    reduce_success = True
                                    break
                                elif r_stat.status == "FAILED":
                                    reduce_success = False
                                    break
                    except Exception as rex:
                        dashboard.log_event(f"[Reduce Task 에러] FedAvg 병합 실패: {rex}")
                        reduce_success = False
                    finally:
                        with gcs_state.registry_lock:
                            if reduce_worker_id in gcs_state.worker_registry:
                                gcs_state.worker_registry[reduce_worker_id]["status"] = "IDLE"
                                
                    success = reduce_success
                    execution_time = time.time() - start_time
                    if success:
                        dashboard.log_event(f"[Map-Reduce] {task_id} 최종 Map-Reduce FedAvg 병합 성공! (총 시간: {execution_time:.2f}초)")
                    else:
                        dashboard.log_event(f"[Map-Reduce] {task_id} Reduce 병합 단계 실패.")
                else:
                    dashboard.log_event(f"[Reduce Task 에러] 병합을 맡길 가용 워커가 존재하지 않습니다. 실패 처리.")
                    success = False
                    execution_time = time.time() - start_time
        
        else:
            # [일반 단일 워커 할당 분기]
            channel = grpc.insecure_channel(worker_address)
            stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
            
            # [아키텍처 선택: 파일 기반 통신 및 복잡도 절충]
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
                while True:
                    time.sleep(1.5)
                    
                    with gcs_state.registry_lock:
                        if worker_id not in gcs_state.worker_registry:
                            raise grpc.RpcError("Worker node went offline during task execution.")
                    
                    status_res = stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=task_id))
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
                
        # --- Q-Learning 보상 산출 및 Q-Table 업데이트 피드백 단계 ---
        end_time = time.time()
        delay_time = max(0.0, end_time - task["deadline"])
        deadline_exceeded = end_time > task["deadline"]
        
        # 가상 예산 차감
        worker_type = worker_info["node_type"]
        cost_profile = agent.nodes_config.get(worker_type, {"cost_per_hour": 0.0})
        cost_per_hour = cost_profile.get("cost_per_hour", 0.0)
        task_cost = cost_per_hour * (execution_time / 3600.0)
        gcs_state.virtual_budget -= task_cost
        
        if gcs_state.SCHEDULER_MODE == "q_learning" and state is not None and action is not None:
            reward = agent.calculate_reward(
                success=success,
                execution_time=execution_time,
                worker_type=worker_type,
                delay_time=delay_time,
                deadline_exceeded=deadline_exceeded
            )
            
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
            
            agent.update_q_value(state, action, reward, next_state)
            agent.save_q_table()
            
            dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} | Reward={reward:.4f} | NextState={next_state} | Epsilon={agent.epsilon:.4f}")
            dashboard.log_event(f"[Q-Learning Update] 잔여 가상 예산: ${gcs_state.virtual_budget:.4f}달러")
        else:
            dashboard.log_event(f"[Resource Spend] [Mode: {gcs_state.SCHEDULER_MODE}] 비용 차감: ${task_cost:.4f} | 잔여 예산: ${gcs_state.virtual_budget:.4f}")
        
        # 실패 시 복구 재삽입
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
