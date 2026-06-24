import grpc
from concurrent import futures
import time
import os

from proto import babyray_pb2
from proto import babyray_pb2_grpc

class BabyRayHeadServicer(babyray_pb2_grpc.BabyRayServiceServicer):
    def RegisterWorker(self, request, context):
        print(f"[Head] Registering worker: {request.worker_id} (Type: {request.node_type})")
        return babyray_pb2.RegisterResponse(success=True, message=f"Worker '{request.worker_id}' registered successfully.")

    def DeregisterWorker(self, request, context):
        print(f"[Head] Deregistering worker: {request.worker_id}")
        return babyray_pb2.DeregisterResponse(success=True, message=f"Worker '{request.worker_id}' deregistered successfully.")

    def SendHeartbeat(self, request, context):
        print(f"[Head] Heartbeat from {request.worker_id}: CPU={request.cpu_utilization}%, Mem={request.memory_utilization}%")
        return babyray_pb2.HeartbeatResponse(ack=True)

def serve():
    port = os.environ.get("HEAD_PORT", "50051")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    babyray_pb2_grpc.add_BabyRayServiceServicer_to_server(BabyRayHeadServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    print(f"=== [Head] Baby Ray Head Node gRPC Server started on port {port} ===")
    server.start()
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        print("[Head] Stopping gRPC Server...")
        server.stop(0)

if __name__ == '__main__':
    serve()
