import grpc
import time
import argparse
import os
import sys
import psutil

from proto import babyray_pb2
from proto import babyray_pb2_grpc

def run(worker_id, node_type, head_host, head_port):
    print(f"=== [Worker] Starting Worker: {worker_id} (Type: {node_type}) ===")
    print(f"[Worker] Connecting to Head at {head_host}:{head_port}...")
    
    channel = grpc.insecure_channel(f"{head_host}:{head_port}")
    stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
    
    # 1. Register with Head
    try:
        response = stub.RegisterWorker(babyray_pb2.RegisterRequest(
            worker_id=worker_id,
            node_type=node_type,
            port=50052  # Local port (placeholder)
        ))
        if response.success:
            print(f"[Worker] Registration successful: {response.message}")
        else:
            print(f"[Worker] Registration failed by Head: {response.message}")
            sys.exit(1)
    except grpc.RpcError as e:
        print(f"[Worker] RpcError during registration: {e}")
        print("[Worker] Continuing execution and will retry heartbeats...")
        
    # 2. Periodic heartbeat and status reporting
    try:
        while True:
            try:
                # Collect real system metrics using psutil
                cpu_util = psutil.cpu_percent(interval=None)
                mem_util = psutil.virtual_memory().percent
                
                hb_response = stub.SendHeartbeat(babyray_pb2.HeartbeatRequest(
                    worker_id=worker_id,
                    cpu_utilization=cpu_util,
                    memory_utilization=mem_util
                ))
                print(f"[Worker] Heartbeat sent: CPU={cpu_util}%, Mem={mem_util}%. ACK={hb_response.ack}")
            except grpc.RpcError as e:
                print(f"[Worker] Failed to send heartbeat: {e.details() if hasattr(e, 'details') else e}")
            
            time.sleep(5)
            
    except KeyboardInterrupt:
        print("\n[Worker] KeyboardInterrupt received. Deregistering and shutting down...")
        try:
            dereg_response = stub.DeregisterWorker(babyray_pb2.DeregisterRequest(worker_id=worker_id))
            print(f"[Worker] Deregistration response: {dereg_response.message}")
        except grpc.RpcError as e:
            print(f"[Worker] Failed to deregister from Head: {e}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="BabyRay Worker Node")
    parser.add_argument("--id", type=str, required=True, help="Worker ID")
    parser.add_argument("--type", type=str, required=True, help="Worker node type (e.g., on_demand, spot_a)")
    parser.add_argument("--port", type=int, default=50052, help="Worker port")
    parser.add_argument("--head-host", type=str, default="localhost", help="Head node hostname or IP")
    parser.add_argument("--head-port", type=int, default=50051, help="Head node port")
    
    # Allow overriding via environment variables (Docker friendly)
    env_id = os.environ.get("WORKER_ID")
    env_type = os.environ.get("NODE_TYPE")
    env_head_host = os.environ.get("HEAD_HOST")
    env_head_port = os.environ.get("HEAD_PORT")
    
    args = parser.parse_args()
    
    worker_id = env_id if env_id else args.id
    node_type = env_type if env_type else args.type
    head_host = env_head_host if env_head_host else args.head_host
    head_port = int(env_head_port) if env_head_port else args.head_port
    
    run(worker_id, node_type, head_host, head_port)
