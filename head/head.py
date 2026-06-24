import grpc
from concurrent import futures
import time
import os
import sys
import threading

# 실행 시 프로젝트 루트 디렉토리를 sys.path에 추가하여 proto 패키지를 정상적으로 찾을 수 있도록 설정합니다.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from proto import babyray_pb2
from proto import babyray_pb2_grpc
from common.config import DEFAULT_HEAD_PORT

# 워커 관리용 인메모리 레지스트리
# worker_id -> { "node_type": str, "ip": str, "port": int, "last_heartbeat": float, "cpu": float, "mem": float, "status": str }
worker_registry = {}
registry_lock = threading.Lock()

# 할당할 작업 번호 생성기
task_counter = 0

class BabyRayHeadServicer(babyray_pb2_grpc.BabyRayServiceServicer):
    def RegisterWorker(self, request, context):
        peer = context.peer()
        # gRPC peer IP 주소 파싱 (IPv4 및 IPv6 호환)
        if peer.startswith("ipv4:"):
            ip = peer.split(":")[1]
        elif peer.startswith("ipv6:"):
            last_colon = peer.rfind(":")
            ip = peer[5:last_colon]
            # URL 인코딩되거나 표준 대괄호가 섞여있을 수 있어 제거합니다.
            ip = ip.replace("%5B", "").replace("%5D", "").replace("[", "").replace("]", "")
        else:
            ip = "127.0.0.1"
            
        # IPv6 로컬 루프백 처리
        if ip == "::1":
            ip = "127.0.0.1"
            
        with registry_lock:
            worker_registry[request.worker_id] = {
                "node_type": request.node_type,
                "ip": ip,
                "port": request.port,
                "last_heartbeat": time.time(),
                "cpu": 0.0,
                "mem": 0.0,
                "status": "IDLE"
            }
            print(f"[Head Registry] 워커 등록: ID='{request.worker_id}' | 주소: {ip}:{request.port}")
            
        return babyray_pb2.RegisterResponse(
            success=True, 
            message=f"Worker '{request.worker_id}' registered on Head."
        )

    def DeregisterWorker(self, request, context):
        with registry_lock:
            if request.worker_id in worker_registry:
                del worker_registry[request.worker_id]
                print(f"[Head Registry] 워커 퇴장: ID='{request.worker_id}'")
                return babyray_pb2.DeregisterResponse(success=True, message="Deregistered.")
            return babyray_pb2.DeregisterResponse(success=False, message="Worker not found.")

    def SendHeartbeat(self, request, context):
        with registry_lock:
            if request.worker_id in worker_registry:
                worker_registry[request.worker_id]["last_heartbeat"] = time.time()
                worker_registry[request.worker_id]["cpu"] = request.cpu_utilization
                worker_registry[request.worker_id]["mem"] = request.memory_utilization
            # 다른 동작 없이 하트비트 수집만 수행 (콘솔을 깨끗하게 유지하기 위해 로그 미출력)
        return babyray_pb2.HeartbeatResponse(ack=True)


# --- 4. 태스크 할당 및 실시간 모니터링 컨트롤러 ---

def run_task_on_worker(worker_id, worker_info, task_id, model_type, epochs):
    ip = worker_info['ip']
    if ":" in ip:
        worker_address = f"[{ip}]:{worker_info['port']}"
    else:
        worker_address = f"{ip}:{worker_info['port']}"
        
    print(f"\n[Scheduler] >>> 작업 할당 시도: {task_id} ({model_type}) -> 워커 '{worker_id}' ({worker_address})")
    
    with registry_lock:
        if worker_id in worker_registry:
            worker_registry[worker_id]["status"] = "BUSY"
            
    try:
        # Worker의 gRPC 서버 채널 오픈
        channel = grpc.insecure_channel(worker_address)
        stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
        
        # 1. 작업 할당 명령 전송
        result = stub.AssignTask(babyray_pb2.TaskAssignment(
            task_id=task_id,
            model_type=model_type,
            dataset_path=f"data/{model_type.lower()}_dataset.pt",
            epochs=epochs
        ))
        
        print(f"[Scheduler] 작업 할당 응답: 상태={result.status} | 메시지='{result.message}'")
        
        if result.status == "RUNNING":
            # 2. 진행 상태 실시간 폴링(Polling) 감시
            while True:
                time.sleep(2)  # 2초마다 감시
                status_res = stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=task_id))
                
                print(f"[Scheduler] 모니터링 - {task_id} 진행률: {status_res.progress:.1f}% | 상태: {status_res.status}")
                
                if status_res.status in ["SUCCESS", "FAILED", "COMPLETED"]:
                    print(f"\n=== [Scheduler] 작업 {task_id} 실행 완료 (최종 상태: {status_res.status}) ===")
                    print(f"[Scheduler] 워커 원격 출력 로그:\n---\n{status_res.logs}\n---")
                    break
        else:
            print(f"[Scheduler] 작업 실행 개시 실패: {result.message}")
            
    except grpc.RpcError as e:
        print(f"[Scheduler] 워커 '{worker_id}' 통신 오류: {e.details() if hasattr(e, 'details') else e}")
    finally:
        # 작업 완료 후 워커를 다시 IDLE로 변경
        with registry_lock:
            if worker_id in worker_registry:
                worker_registry[worker_id]["status"] = "IDLE"


def scheduler_loop():
    global task_counter
    print("[Scheduler] 백그라운드 스케줄러 기동 완료. 워커 등록을 기다립니다...")
    
    model_types = ["CNN", "RNN", "LSTM"]
    
    while True:
        time.sleep(10) # 10초마다 스케줄링 판단
        
        selected_worker = None
        selected_worker_info = None
        
        with registry_lock:
            # 1. DEAD 노드 헬스체크 (15초 초과 미수신 워커 자동 제거)
            current_time = time.time()
            dead_workers = []
            for wid, info in list(worker_registry.items()):
                if current_time - info["last_heartbeat"] > 15.0:
                    dead_workers.append(wid)
                    
            for wid in dead_workers:
                print(f"[Scheduler GCS] ☠️ 워커 오프라인 감지(15초 초과): 워커 '{wid}'가 삭제되었습니다.")
                del worker_registry[wid]
                
            # 2. 가용한 워커 탐색 (상태가 IDLE인 워커 중 CPU 사용량이 가장 낮은 워커 선정)
            idle_workers = {wid: info for wid, info in worker_registry.items() if info["status"] == "IDLE"}
            if idle_workers:
                selected_worker = min(idle_workers, key=lambda k: idle_workers[k]["cpu"])
                selected_worker_info = idle_workers[selected_worker].copy()
                
        if selected_worker:
            task_counter += 1
            task_id = f"task-{task_counter:04d}"
            model_type = model_types[task_counter % len(model_types)]
            
            # 작업을 스케줄러 메인 스레드를 블로킹하지 않도록 백그라운드 스레드로 비동기 할당
            threading.Thread(
                target=run_task_on_worker,
                args=(selected_worker, selected_worker_info, task_id, model_type, 5), # 5에포크(5초) 실행
                daemon=True
            ).start()


def serve():
    port = os.environ.get("HEAD_PORT", str(DEFAULT_HEAD_PORT))
    
    # 1. Head gRPC 서버 기동
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    babyray_pb2_grpc.add_BabyRayServiceServicer_to_server(BabyRayHeadServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"=== [Head] Baby Ray Head Node gRPC 서버 시작 완료 (포트: {port}) ===")
    
    # 2. 백그라운드 스케줄러 기동
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()
    
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        print("[Head] Head 서버를 중지합니다...")
        server.stop(0)

if __name__ == '__main__':
    serve()
