import grpc
from concurrent import futures
import time
import os
import sys

# 실행 시 프로젝트 루트 디렉토리를 sys.path에 추가하여 proto 패키지를 정상적으로 찾을 수 있도록 설정합니다.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from proto import babyray_pb2
from proto import babyray_pb2_grpc

class SimpleHeadServicer(babyray_pb2_grpc.BabyRayServiceServicer):
    def RegisterWorker(self, request, context):
        print(f"[Head Server] RegisterWorker 요청 수신 | ID: '{request.worker_id}', Type: '{request.node_type}'")
        return babyray_pb2.RegisterResponse(
            success=True, 
            message="[Head] 연결 성공! Worker 등록이 접수되었습니다."
        )

    def SendHeartbeat(self, request, context):
        print(f"[Head Server] SendHeartbeat 수신 | ID: '{request.worker_id}', CPU: {request.cpu_utilization}%, Mem: {request.memory_utilization}%")
        return babyray_pb2.HeartbeatResponse(ack=True)

def serve():
    # 2개의 동시 작업 스레드를 지원하는 gRPC 서버 생성
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    babyray_pb2_grpc.add_BabyRayServiceServicer_to_server(SimpleHeadServicer(), server)
    
    # 50051 포트 개방
    server.add_insecure_port("[::]:50051")
    print("=== [Head Server] 50051 포트에서 gRPC 서버 시작 완료 ===")
    server.start()
    
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("[Head Server] 서버를 종료합니다...")
        server.stop(0)

if __name__ == "__main__":
    serve()
