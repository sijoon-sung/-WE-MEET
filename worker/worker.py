import grpc
import sys
import os

# 실행 시 프로젝트 루트 디렉토리를 sys.path에 추가하여 proto 패키지를 정상적으로 찾을 수 있도록 설정합니다.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from proto import babyray_pb2
from proto import babyray_pb2_grpc

def test_connection():
    head_address = "localhost:50051"
    print(f"[Worker Client] Head 노드 연결 중: {head_address}...")
    
    try:
        # gRPC 채널 생성
        channel = grpc.insecure_channel(head_address)
        stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
        
        # 1. RegisterWorker RPC 테스트 (1회성 동기식 호출)
        print("[Worker Client] 1. RegisterWorker 요청 송신...")
        reg_response = stub.RegisterWorker(babyray_pb2.RegisterRequest(
            worker_id="test-worker-01",
            node_type="on_demand",
            port=50052
        ))
        print(f"[Worker Client] 1. 응답 성공! 결과: success={reg_response.success}, message='{reg_response.message}'")
        
        # 2. SendHeartbeat RPC 테스트 (1회성 동기식 호출)
        print("[Worker Client] 2. SendHeartbeat 요청 송신...")
        hb_response = stub.SendHeartbeat(babyray_pb2.HeartbeatRequest(
            worker_id="test-worker-01",
            cpu_utilization=15.4,
            memory_utilization=48.2
        ))
        print(f"[Worker Client] 2. 응답 성공! 결과: ack={hb_response.ack}")
        
        print("\n=== [Worker Client] gRPC 연결 체크 실험 성공! ===")
        
    except grpc.RpcError as e:
        print(f"\n[Worker Client] gRPC 에러 발생: {e.code()} - {e.details()}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[Worker Client] 예외 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_connection()
