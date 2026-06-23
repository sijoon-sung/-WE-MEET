import psutil
import time
import logging
import threading
import grpc
import sys
import os

# Import proto definitions
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from proto import baby_ray_pb2
from proto import baby_ray_pb2_grpc

logging.basicConfig(level=logging.INFO, format="[WORKER-MONITOR] %(asctime)s - %(levelname)s - %(message)s")

class ResourceMonitorDaemon:
    def __init__(self, worker_id, head_address="babyray-head:50051"):
        self.worker_id = worker_id
        self.head_address = head_address
        self.running = False

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logging.info(f"Started resource monitoring daemon for worker '{self.worker_id}'")

    def stop(self):
        self.running = False
        if hasattr(self, 'thread'):
            self.thread.join(timeout=1.0)

    def _run_loop(self):
        while self.running:
            try:
                # Gather CPU and Memory usage
                cpu_usage = psutil.cpu_percent(interval=None)
                mem_info = psutil.virtual_memory()
                memory_used = mem_info.used

                # Connect to head node and send heartbeat
                with grpc.insecure_channel(self.head_address) as channel:
                    stub = baby_ray_pb2_grpc.BabyRayServiceStub(channel)
                    request = baby_ray_pb2.HeartbeatRequest(
                        worker_id=self.worker_id,
                        cpu_usage_percent=cpu_usage,
                        memory_used_bytes=float(memory_used),
                        timestamp=int(time.time() * 1000)
                    )
                    # Set a short timeout for heartbeat requests
                    response = stub.SendHeartbeat(request, timeout=2.0)
                    if not response.acknowledged:
                        logging.warning(f"Heartbeat not acknowledged: command={response.command}")
            except grpc.RpcError as e:
                logging.warning(f"Failed to send heartbeat to head node: {e.code()}")
            except Exception as e:
                logging.error(f"Unexpected error in monitor loop: {e}")
            
            # Send heartbeat every 2 seconds
            time.sleep(2.0)
