import logging

logging.basicConfig(level=logging.INFO, format="[SCHEDULER] %(asctime)s - %(levelname)s - %(message)s")

class GlobalScheduler:
    def __init__(self, gcs):
        self.gcs = gcs

    def schedule_task(self, task_id, dependency_object_ids):
        """
        Locality-aware 및 Resource-aware 스케줄링 의사결정
        """
        active_workers = {wid: winfo for wid, winfo in self.gcs.workers.items() if winfo["status"] == "ACTIVE"}
        
        if not active_workers:
            logging.error("No active workers available for scheduling")
            return None

        # 1. Locality-aware Score 계산
        # 의존성이 있는 데이터 객체들이 주로 위치한 노드를 선별
        locality_scores = {wid: 0 for wid in active_workers.keys()}
        for obj_id in dependency_object_ids:
            if obj_id in self.gcs.objects:
                loc_worker = self.gcs.objects[obj_id]["worker_id"]
                if loc_worker in locality_scores:
                    locality_scores[loc_worker] += 1
        
        # 2. Resource-aware 및 Dynamic Bursting 검사
        # CPU/메모리 부하가 임계치(예: 80%)를 초과한 노드는 후순위로 미룸 (버스팅 유도)
        selected_worker = None
        best_score = -1
        
        # 정렬하여 데이터 지역성이 높고 자원 상태가 좋은 노드 선발
        for worker_id in sorted(active_workers.keys(), key=lambda w: (locality_scores[w], -active_workers[w]["cpu_usage"]), reverse=True):
            winfo = active_workers[worker_id]
            cpu_usage = winfo["cpu_usage"]
            
            # 오프로딩/버스팅 임계값 체크: 너무 바쁘면 다른 노드로 보냄
            if cpu_usage > 85.0:
                logging.info(f"Worker '{worker_id}' is overloaded ({cpu_usage}%). Checking alternative workers...")
                continue
            
            selected_worker = worker_id
            break

        # 만약 모든 노드가 부하 임계값을 초과한 경우, 가장 부하가 적은 노드를 차선책으로 선택
        if not selected_worker:
            selected_worker = min(active_workers.keys(), key=lambda w: active_workers[w]["cpu_usage"])
            logging.warning(f"All workers overloaded. Selected least loaded worker '{selected_worker}'")

        logging.info(f"Scheduled task '{task_id}' to worker '{selected_worker}' (Locality Score: {locality_scores.get(selected_worker, 0)})")
        return selected_worker
