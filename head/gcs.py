import logging

logging.basicConfig(level=logging.INFO, format="[GCS] %(asctime)s - %(levelname)s - %(message)s")

class GlobalControlStore:
    def __init__(self):
        # Workers registry: {worker_id: {ip, port, total_cpus, total_memory, last_heartbeat, cpu_usage, memory_used}}
        self.workers = {}
        # Tasks registry: {task_id: {name, state, worker_id, progress, loss, lineage, dependencies}}
        self.tasks = {}
        # Objects store metadata: {object_id: {worker_id, size_bytes}}
        self.objects = {}

    def register_worker(self, worker_id, ip, port, total_cpus, total_memory):
        self.workers[worker_id] = {
            "ip": ip,
            "port": port,
            "total_cpus": total_cpus,
            "total_memory": total_memory,
            "last_heartbeat": 0,
            "cpu_usage": 0.0,
            "memory_used": 0.0,
            "status": "ACTIVE"
        }
        logging.info(f"Registered worker '{worker_id}' at {ip}:{port}")

    def update_heartbeat(self, worker_id, cpu_usage, memory_used, timestamp):
        if worker_id in self.workers:
            self.workers[worker_id]["cpu_usage"] = cpu_usage
            self.workers[worker_id]["memory_used"] = memory_used
            self.workers[worker_id]["last_heartbeat"] = timestamp
            self.workers[worker_id]["status"] = "ACTIVE"
        else:
            logging.warning(f"Heartbeat received from unregistered worker '{worker_id}'")

    def register_task(self, task_id, name, dependencies=None):
        self.tasks[task_id] = {
            "name": name,
            "state": "PENDING",
            "worker_id": None,
            "progress": 0.0,
            "loss": 0.0,
            "dependencies": dependencies or []
        }
        logging.info(f"Task '{task_id}' ({name}) registered as PENDING")

    def update_task_status(self, task_id, state, progress=0.0, loss=0.0, worker_id=None):
        if task_id in self.tasks:
            self.tasks[task_id]["state"] = state
            self.tasks[task_id]["progress"] = progress
            self.tasks[task_id]["loss"] = loss
            if worker_id:
                self.tasks[task_id]["worker_id"] = worker_id
        else:
            logging.warning(f"Task '{task_id}' not found for status update")

    def register_object(self, object_id, worker_id, size_bytes):
        self.objects[object_id] = {
            "worker_id": worker_id,
            "size_bytes": size_bytes
        }
        logging.info(f"Object '{object_id}' registered at worker '{worker_id}' ({size_bytes} bytes)")
