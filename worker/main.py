import grpc
from concurrent import futures
import time
import logging
import argparse
import threading
import sys
import os

# Import proto definitions
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from proto import baby_ray_pb2
from proto import baby_ray_pb2_grpc

from worker.object_store import LocalObjectStore
from worker.monitor import ResourceMonitorDaemon
from worker.executor import run_lstm_training

logging.basicConfig(level=logging.INFO, format="[WORKER-MAIN] %(asctime)s - %(levelname)s - %(message)s")

class WorkerServiceServicer(baby_ray_pb2_grpc.BabyRayServiceServicer):
    def __init__(self, worker_id, object_store, gpu_scale, threads):
        self.worker_id = worker_id
        self.object_store = object_store
        self.gpu_scale = gpu_scale
        self.threads = threads
        # Local task registry: {task_id: {status, progress, loss}}
        self.tasks = {}

    def AssignTask(self, request, context):
        task_id = request.task_id
        logging.info(f"Received task assignment request: '{task_id}' ({request.task_name})")
        
        # Initialize local task status
        self.tasks[task_id] = {
            "status": "RUNNING",
            "progress": 0.0,
            "loss": 0.0
        }

        # Spawn a background thread to execute PyTorch training
        thread = threading.Thread(
            target=self._execute_task_async,
            args=(request,),
            daemon=True
        )
        thread.start()

        return baby_ray_pb2.TaskResponse(success=True, error_message="")

    def _execute_task_async(self, request):
        task_id = request.task_id
        
        def status_callback(progress, loss):
            self.tasks[task_id]["progress"] = progress
            self.tasks[task_id]["loss"] = loss

        try:
            # Overwrite default parameters if request specifies them
            thread_limit = request.thread_limit if request.thread_limit > 0 else self.threads
            gpu_scale = request.gpu_scale_factor if request.gpu_scale_factor > 0 else self.gpu_scale

            # Run training
            model = run_lstm_training(
                task_id=task_id,
                sequence_length=request.sequence_length,
                hidden_dim=request.hidden_dim,
                epochs=request.epochs,
                learning_rate=request.learning_rate,
                thread_limit=thread_limit,
                gpu_scale_factor=gpu_scale,
                status_callback=status_callback
            )
            
            # Save trained model to Local Object Store (e.g. key as task_id_model)
            object_id = f"{task_id}_model"
            self.object_store.put(object_id, model)
            
            self.tasks[task_id]["status"] = "SUCCESS"
            logging.info(f"Task '{task_id}' completed successfully and model saved to object store.")
        except Exception as e:
            self.tasks[task_id]["status"] = "FAILED"
            logging.error(f"Task '{task_id}' execution failed: {e}")

    def GetTaskStatus(self, request, context):
        task_id = request.task_id
        if task_id in self.tasks:
            tinfo = self.tasks[task_id]
            return baby_ray_pb2.TaskStatusResponse(
                task_id=task_id,
                status=tinfo["status"],
                progress=tinfo["progress"],
                loss=tinfo["loss"]
            )
        else:
            return baby_ray_pb2.TaskStatusResponse(task_id=task_id, status="UNKNOWN", progress=0.0, loss=0.0)

    # Empty implementations of Head-only endpoints
    def RegisterWorker(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("RegisterWorker is only implemented on the Head Node")
        return baby_ray_pb2.RegisterResponse()

    def SendHeartbeat(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("SendHeartbeat is only implemented on the Head Node")
        return baby_ray_pb2.HeartbeatResponse()


def register_at_head(worker_id, worker_ip, worker_port, threads, head_address):
    """
    Worker 기동 시 Head Node gRPC 서버에 본인을 등록하는 함수
    """
    logging.info(f"Connecting to Head Node at {head_address} for registration...")
    # Retry loop in case Head node is booting up
    for i in range(10):
        try:
            with grpc.insecure_channel(head_address) as channel:
                stub = baby_ray_pb2_grpc.BabyRayServiceStub(channel)
                request = baby_ray_pb2.RegisterRequest(
                    worker_id=worker_id,
                    worker_ip=worker_ip,
                    worker_port=worker_port,
                    total_cpu_cores=float(threads),
                    total_memory_bytes=1024 * 1024 * 1024  # Simulated total memory
                )
                response = stub.RegisterWorker(request, timeout=3.0)
                if response.success:
                    logging.info("Successfully registered worker at Head Node.")
                    return True
        except Exception as e:
            logging.warning(f"Registration attempt {i+1} failed: {e}. Retrying in 2s...")
            time.sleep(2)
    logging.error("Failed to register worker at Head Node after 10 attempts.")
    return False


def serve():
    parser = argparse.ArgumentParser(description="Baby Ray Worker Node")
    parser.add_argument("--id", type=str, required=True, help="Worker ID (e.g. worker-1)")
    parser.add_argument("--port", type=int, required=True, help="Port to listen for tasks")
    parser.add_argument("--gpu-scale", type=float, default=1.0, help="Virtual GPU performance scaling factor")
    parser.add_argument("--threads", type=int, default=2, help="CPU thread limit")
    parser.add_argument("--head", type=str, default="babyray-head:50051", help="Head node address")
    args = parser.parse_args()

    # Determine worker container IP (container name acts as host, so we use container ID as IP or container hostname)
    worker_ip = args.id # In docker network, container name/id acts as IP/domain
    
    object_store = LocalObjectStore()

    # Register at Head Node
    if not register_at_head(args.id, worker_ip, args.port, args.threads, args.head):
        logging.error("Shutting down due to registration failure.")
        sys.exit(1)

    # Start Heartbeat Daemon
    monitor_daemon = ResourceMonitorDaemon(args.id, args.head)
    monitor_daemon.start()

    # Start gRPC Server to listen for Task Assignments from Head
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=5))
    baby_ray_pb2_grpc.add_BabyRayServiceServicer_to_server(
        WorkerServiceServicer(args.id, object_store, args.gpu_scale, args.threads),
        server
    )
    server.add_insecure_port(f"[::]:{args.port}")
    logging.info(f"Starting Worker Node '{args.id}' gRPC server on port {args.port}...")
    server.start()

    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        logging.info("Shutting down Worker Node...")
        monitor_daemon.stop()
        server.stop(0)

if __name__ == "__main__":
    serve()
