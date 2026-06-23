import logging

logging.basicConfig(level=logging.INFO, format="[LINEAGE] %(asctime)s - %(levelname)s - %(message)s")

class LineageTracker:
    def __init__(self, gcs):
        self.gcs = gcs
        # Lineage DAG representation: {child_task_id: [parent_task_ids]}
        self.dag = {}

    def add_lineage(self, task_id, parent_task_ids):
        self.dag[task_id] = parent_task_ids
        logging.info(f"Recorded lineage for task '{task_id}': parents -> {parent_task_ids}")

    def trace_recovery_plan(self, failed_worker_id):
        """
        장애 노드 발생 시 유실된 태스크를 식별하고 복구(재실행) 우선순위 계획 수립
        """
        logging.info(f"Tracing recovery plan for failed worker '{failed_worker_id}'")
        
        lost_tasks = []
        for tid, tinfo in self.gcs.tasks.items():
            if tinfo["worker_id"] == failed_worker_id and tinfo["state"] != "SUCCESS":
                lost_tasks.append(tid)

        # 의존 관계를 고려한 복구 순서 결정 (위상 정렬의 간소화)
        # 부모 태스크가 먼저 복구되고, 이를 사용하는 자식 태스크가 복구되도록 순서 정렬
        recovery_order = []
        visited = set()

        def dfs(task_id):
            if task_id in visited:
                return
            visited.add(task_id)
            # 부모 태스크들을 먼저 재귀 탐색
            parents = self.dag.get(task_id, [])
            for parent in parents:
                if parent in lost_tasks:
                    dfs(parent)
            recovery_order.append(task_id)

        for task_id in lost_tasks:
            dfs(task_id)

        logging.info(f"Recovery plan formulated. Rescheduling tasks in order: {recovery_order}")
        return recovery_order
