import grpc
import time
import uuid
import sys
import os

# Import proto definitions
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from proto import baby_ray_pb2
from proto import baby_ray_pb2_grpc

def run_benchmark():
    head_address = "localhost:50051"
    print(f"Connecting to Head Node at {head_address} for benchmarking...")
    
    with grpc.insecure_channel(head_address) as channel:
        stub = baby_ray_pb2_grpc.BabyRayServiceStub(channel)

        # 1. Submit a series of LSTM tasks to benchmark scheduling
        task_id_1 = str(uuid.uuid4())
        task_id_2 = str(uuid.uuid4())
        
        print(f"\n[Step 1] Submitting Task 1: Normal execution (A100-like simulation, scale=1.0)...")
        request_1 = baby_ray_pb2.TaskRequest(
            task_id=task_id_1,
            task_name="LSTM_Sine_Prediction_Normal",
            sequence_length=50,
            hidden_dim=64,
            epochs=5,
            learning_rate=0.01,
            thread_limit=2,
            gpu_scale_factor=1.0,  # Full speed
            dependency_object_ids=[]
        )
        response_1 = stub.AssignTask(request_1)
        print(f"Task 1 Submission Response: success={response_1.success}, error='{response_1.error_message}'")

        print(f"\n[Step 2] Submitting Task 2: Throttled execution (T4-like simulation, scale=0.3)...")
        request_2 = baby_ray_pb2.TaskRequest(
            task_id=task_id_2,
            task_name="LSTM_Sine_Prediction_Throttled",
            sequence_length=50,
            hidden_dim=64,
            epochs=5,
            learning_rate=0.01,
            thread_limit=1,
            gpu_scale_factor=0.3,  # 70% delay added (Straggler simulation)
            dependency_object_ids=[]
        )
        response_2 = stub.AssignTask(request_2)
        print(f"Task 2 Submission Response: success={response_2.success}, error='{response_2.error_message}'")

        # 3. Poll task status until completion
        print("\n[Step 3] Polling tasks status...")
        completed_tasks = set()
        all_tasks = {task_id_1: "Task 1", task_id_2: "Task 2"}
        
        start_time = time.time()
        while len(completed_tasks) < len(all_tasks):
            time.sleep(2.0)
            elapsed = time.time() - start_time
            print(f"\nElapsed time: {elapsed:.1f}s")
            
            for tid, name in all_tasks.items():
                if tid in completed_tasks:
                    continue
                
                status_req = baby_ray_pb2.TaskStatusRequest(task_id=tid)
                status_resp = stub.GetTaskStatus(status_req)
                print(f"  {name} ({tid[:8]}): state={status_resp.status}, progress={status_resp.progress:.1f}, loss={status_resp.loss:.6f}")
                
                if status_resp.status in ["SUCCESS", "FAILED"]:
                    completed_tasks.add(tid)
                    print(f"  --> {name} finished with state {status_resp.status}!")

if __name__ == "__main__":
    run_benchmark()
