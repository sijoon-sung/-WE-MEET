import grpc
from concurrent import futures
import time
import logging
import threading
import sys
import os

# Import proto definitions
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from proto import baby_ray_pb2
from proto import baby_ray_pb2_grpc

from head.gcs import GlobalControlStore
from head.scheduler import GlobalScheduler
from head.lineage import LineageTracker

logging.basicConfig(level=logging.INFO, format="[HEAD-MAIN] %(asctime)s - %(levelname)s - %(message)s")

class BabyRayServiceServicer(baby_ray_pb2_grpc.BabyRayServiceServicer):
    def __init__(self, gcs, scheduler, lineage):
        self.gcs = gcs
        self.scheduler = scheduler
        self.lineage = lineage

    def RegisterWorker(self, request, context):
        self.gcs.register_worker(
            worker_id=request.worker_id,
            ip=request.worker_ip,
            port=request.worker_port,
            total_cpus=request.total_cpu_cores,
            total_memory=request.total_memory_bytes
        )
        return baby_ray_pb2.RegisterResponse(success=True, message=f"Worker '{request.worker_id}' successfully registered.")

    def SendHeartbeat(self, request, context):
        current_time = time.time()
        self.gcs.update_heartbeat(
            worker_id=request.worker_id,
            cpu_usage=request.cpu_usage_percent,
            memory_used=request.memory_used_bytes,
            timestamp=current_time
        )
        return baby_ray_pb2.HeartbeatResponse(acknowledged=True, command="NONE")

    def AssignTask(self, request, context):
        # Register task in GCS
        self.gcs.register_task(request.task_id, request.task_name, request.dependency_object_ids)
        
        # Schedule the task
        selected_worker = self.scheduler.schedule_task(request.task_id, request.dependency_object_ids)
        
        if not selected_worker:
            return baby_ray_pb2.TaskResponse(success=False, error_message="No active worker available for assignment.")

        # Relay the TaskRequest to the selected worker's gRPC Server
        worker_info = self.gcs.workers[selected_worker]
        worker_address = f"{worker_info['ip']}:{worker_info['port']}"
        
        try:
            logging.info(f"Relaying task '{request.task_id}' to worker '{selected_worker}' at {worker_address}")
            with grpc.insecure_channel(worker_address) as channel:
                stub = baby_ray_pb2_grpc.BabyRayServiceServicerStub(channel) if hasattr(baby_ray_pb2_grpc, 'BabyRayServiceServicerStub') else None
                # Note: Worker runs a gRPC service as well to receive tasks, or we can invoke it via a separate interface.
                # Let's ensure the worker stub matches our schema.
                # Since the worker implements the same service schema, we can use the same stub.
                worker_stub = baby_ray_pb2_grpc.BabyRayServiceStub(channel)
                response = worker_stub.AssignTask(request)
                
                if response.success:
                    self.gcs.update_task_status(request.task_id, "RUNNING", worker_id=selected_worker)
                return response
        except Exception as e:
            logging.error(f"Failed to relay task to worker '{selected_worker}': {e}")
            return baby_ray_pb2.TaskResponse(success=False, error_message=str(e))

    def GetTaskStatus(self, request, context):
        if request.task_id in self.gcs.tasks:
            tinfo = self.gcs.tasks[request.task_id]
            return baby_ray_pb2.TaskStatusResponse(
                task_id=request.task_id,
                status=tinfo["state"],
                progress=tinfo["progress"],
                loss=tinfo["loss"]
            )
        else:
            return baby_ray_pb2.TaskStatusResponse(task_id=request.task_id, status="UNKNOWN", progress=0.0, loss=0.0)


def monitor_workers_timeout(gcs, lineage_tracker):
    """
    6초 이상 하트비트가 수집되지 않은 노드를 사망(DEAD) 처리하고
    Task Lineage 기반 복구 로직을 수행하는 모니터 스레드 함수
    """
    while True:
        time.sleep(2)
        current_time = time.time()
        for worker_id, winfo in list(gcs.workers.items()):
            if winfo["status"] == "ACTIVE" and (current_time - winfo["last_heartbeat"]) > 6.0:
                logging.warning(f"Worker '{worker_id}' heartbeat timeout! Marking as DEAD.")
                winfo["status"] = "DEAD"
                
                # Tracing recovery plan
                recovery_tasks = lineage_tracker.trace_recovery_plan(worker_id)
                for tid in recovery_tasks:
                    gcs.update_task_status(tid, "PENDING", progress=0.0, loss=0.0)
                    logging.info(f"Task '{tid}' rescheduled due to node failure.")


def serve():
    gcs = GlobalControlStore()
    scheduler = GlobalScheduler(gcs)
    lineage = LineageTracker(gcs)

    # Start timeout monitoring thread
    monitor_thread = threading.Thread(target=monitor_workers_timeout, args=(gcs, lineage), daemon=True)
    monitor_thread.start()

    # Start gRPC Server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    baby_ray_pb2_grpc.add_BabyRayServiceServicer_to_server(BabyRayServiceServicer(gcs, scheduler, lineage), server)
    
    server.add_insecure_port("[::]:50051")
    logging.info("Starting Head Node gRPC server on port 50051...")
    server.start()
    
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        logging.info("Shutting down Head Node...")
        server.stop(0)

if __name__ == "__main__":
    serve()
