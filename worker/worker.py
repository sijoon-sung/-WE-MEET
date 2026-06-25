import grpc
import sys
import os
import time
import argparse
import threading
from concurrent import futures

# 실행 시 프로젝트 루트 디렉토리를 sys.path에 추가하여 proto 패키지를 정상적으로 찾을 수 있도록 설정합니다.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from proto import babyray_pb2
from proto import babyray_pb2_grpc
from common.config import DEFAULT_HEARTBEAT_INTERVAL

# 분리된 GPU 시뮬레이터 모듈에서 실행기를 가져옵니다.
from gpu_simulator import PyTorchTaskRunner


# --- 1. Worker gRPC 서비스 서버 구현 ---

class BabyRayWorkerServicer(babyray_pb2_grpc.BabyRayServiceServicer):
    def __init__(self, worker_type):
        self.worker_type = worker_type
        self.current_task_id = None
        self.runner = None
        self.lock = threading.Lock()

    def AssignTask(self, request, context):
        with self.lock:
            # 1. 이미 작업이 구동 중인지 중복 검사
            if self.current_task_id is not None and self.runner.status == "RUNNING":
                print(f"[Worker gRPC] 작업 거절: {request.task_id} (이유: 다른 작업 실행 중)")
                return babyray_pb2.TaskResult(
                    task_id=request.task_id,
                    status="FAILED",
                    execution_time=0.0,
                    message="Another task is already running on this worker."
                )
            
            # 2. 신규 실제 작업(PyTorch 모델) 생성
            self.current_task_id = request.task_id
            self.runner = PyTorchTaskRunner(
                task_id=request.task_id,
                model_type=request.model_type,
                epochs=request.epochs,
                worker_type=self.worker_type
            )
            
            # 3. 백그라운드 스레드에서 연산 실행
            threading.Thread(target=self.runner.run, daemon=True).start()
            
            print(f"[Worker gRPC] 작업 접수 승인: {request.task_id}")
            return babyray_pb2.TaskResult(
                task_id=request.task_id,
                status="RUNNING",
                execution_time=0.0,
                message="Task assigned successfully, executing in background."
            )

    def GetTaskStatus(self, request, context):
        with self.lock:
            if self.runner is None or self.runner.task_id != request.task_id:
                return babyray_pb2.TaskStatusResponse(
                    status="NOT_FOUND",
                    progress=0.0,
                    logs="No such task found on this worker."
                )
            
            return babyray_pb2.TaskStatusResponse(
                status=self.runner.status,
                progress=self.runner.progress,
                logs="\n".join(self.runner.logs)
            )

    def ResizeResources(self, request, context):
        print(f"[Worker gRPC] 자원 크기 조절 요청 수신: CPU={request.cpu_cores} Cores, Mem={request.memory_bytes} Bytes")
        return babyray_pb2.ResizeResponse(
            success=True,
            message=f"Configured worker cGroups: CPU={request.cpu_cores}, Mem={request.memory_bytes}"
        )


# --- 2. 하트비트 송신 클라이언트 루프 (Head로 전송) ---

def heartbeat_sender_loop(worker_id, node_type, port, head_host, head_port):
    time.sleep(1.0) # Worker 자체 gRPC 서버가 부팅될 때까지 1초 대기
    
    head_address = f"{head_host}:{head_port}"
    print(f"[Heartbeat] Head 서버 연결 시도: {head_address}...")
    
    channel = grpc.insecure_channel(head_address)
    stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
    
    # 1. Head 서버에 워커 등록 요청
    registered = False
    while not registered:
        try:
            response = stub.RegisterWorker(babyray_pb2.RegisterRequest(
                worker_id=worker_id,
                node_type=node_type,
                port=port
            ))
            if response.success:
                print(f"[Heartbeat] Head 서버 등록 완료: {response.message}")
                registered = True
            else:
                print(f"[Heartbeat] 등록 거절됨. 3초 후 재시도...")
                time.sleep(3)
        except grpc.RpcError:
            print(f"[Heartbeat] Head 서버 연결 지연. 3초 후 재시도...")
            time.sleep(3)
            
    # 2. 주기적 생존 신고 및 상태 리포트
    while True:
        try:
            # 더미 자원 수치 송신
            stub.SendHeartbeat(babyray_pb2.HeartbeatRequest(
                worker_id=worker_id,
                cpu_utilization=12.5,
                memory_utilization=40.0
            ))
        except grpc.RpcError:
            print(f"[Heartbeat] 경고: 생존 신고 전송 실패 (Head 연결이 끊겼습니다)")
            
        time.sleep(DEFAULT_HEARTBEAT_INTERVAL)


# --- 3. Worker 메인 구동 루프 ---

def serve(worker_id, node_type, port, head_host, head_port):
    # 1. Head의 명령을 수신받을 Worker 자체 gRPC 서버 실행
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=3))
    servicer = BabyRayWorkerServicer(worker_type=node_type)
    babyray_pb2_grpc.add_BabyRayServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"=== [Worker] '{worker_id}' ({node_type}) gRPC 서버 활성화 (포트: {port}) ===")
    
    # 2. Head에 하트비트를 보내는 클라이언트 스레드 가동
    hb_thread = threading.Thread(
        target=heartbeat_sender_loop,
        args=(worker_id, node_type, port, head_host, head_port),
        daemon=True
    )
    hb_thread.start()
    
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        print(f"\n[Worker] '{worker_id}' 종료 중...")
        try:
            # 종료 시 GCS 해제 요청
            channel = grpc.insecure_channel(f"{head_host}:{head_port}")
            stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
            stub.DeregisterWorker(babyray_pb2.DeregisterRequest(worker_id=worker_id))
        except Exception:
            pass
        server.stop(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="BabyRay Worker Node")
    parser.add_argument("--id", type=str, default="worker-01", help="Worker ID")
    parser.add_argument("--type", type=str, default="on_demand", help="Worker Type")
    parser.add_argument("--port", type=int, default=50052, help="Worker listening port")
    parser.add_argument("--head-host", type=str, default="localhost", help="Head node IP/Host")
    parser.add_argument("--head-port", type=int, default=50051, help="Head node port")
    
    args = parser.parse_args()
    serve(args.id, args.type, args.port, args.head_host, args.head_port)
