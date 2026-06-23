import grpc
import time
import uuid
import sys
import os

# Import proto definitions
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from proto import baby_ray_pb2
from proto import baby_ray_pb2_grpc

def run_fault_recovery_test():
    head_address = "localhost:50051"
    print(f"Connecting to Head Node at {head_address} for Fault Tolerance integration test...")
    
    with grpc.insecure_channel(head_address) as channel:
        stub = baby_ray_pb2_grpc.BabyRayServiceStub(channel)

        # Submit a long training task (epochs = 30) so we have time to kill the container
        task_id = str(uuid.uuid4())
        print(f"\n[Step 1] Submitting a long-running Task ({task_id[:8]}) with 30 epochs...")
        request = baby_ray_pb2.TaskRequest(
            task_id=task_id,
            task_name="Fault_Tolerance_Demo_Task",
            sequence_length=50,
            hidden_dim=64,
            epochs=30,
            learning_rate=0.01,
            thread_limit=1,
            gpu_scale_factor=1.0,
            dependency_object_ids=[]
        )
        response = stub.AssignTask(request)
        print(f"Task Submission Response: success={response.success}")

        # Monitor progress, wait until it starts running and reaches some progress
        print("\n[Step 2] Monitoring task execution progress...")
        has_started = False
        
        for _ in range(5):
            time.sleep(2.0)
            status_resp = stub.GetTaskStatus(baby_ray_pb2.TaskStatusRequest(task_id=task_id))
            print(f"  Task status: state={status_resp.status}, progress={status_resp.progress:.2f}, loss={status_resp.loss:.6f}")
            if status_resp.status == "RUNNING" and status_resp.progress > 0.05:
                has_started = True
                break
        
        if not has_started:
            print("Task did not progress enough or start. Please ensure the containers are running.")
            return

        # Instruct user to kill the node
        print("\n" + "="*80)
        print(" [ACTION REQUIRED] Please stop the worker container where the task is running!")
        print(" Run the following command in a separate terminal:")
        print("     docker stop babyray-worker-1")
        print("="*80 + "\n")

        print("Waiting for worker timeout detection and auto-recovery (approx 10-15 seconds)...")
        recovered = False
        for elapsed in range(1, 20):
            time.sleep(2.0)
            status_resp = stub.GetTaskStatus(baby_ray_pb2.TaskStatusRequest(task_id=task_id))
            print(f"  t+{elapsed*2}s: state={status_resp.status}, progress={status_resp.progress:.2f}, loss={status_resp.loss:.6f}")
            
            if status_resp.status == "PENDING":
                print("  --> Head Node detected worker failure and rescheduled the task back to PENDING!")
                recovered = True
            
            if status_resp.status == "RUNNING" and recovered:
                print("  --> SUCCESS! Task has resumed running on the alternative worker!")
                break
                
            if status_resp.status == "SUCCESS":
                print("  --> Task completed successfully after recovery!")
                break

if __name__ == "__main__":
    run_fault_recovery_test()
