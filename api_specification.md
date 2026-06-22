# 🛠️ Baby Ray 분산 시스템 API 명세서

본 문서는 Docker 기반 가상 분산 런타임(Baby Ray) 인프라 구축에 적용할 **API 규격 명세서**입니다.
관리 노드(Head)와 연산 노드(Worker) 간의 모든 데이터 전송 메시지와 gRPC 서비스 명세, 그리고 자가 복구 및 cGroup 자원 튜격(Resize) 규격을 정의합니다.

---

## 1️⃣ gRPC 프로토콜 및 Protobuf 메시지 명세 (`baby_ray.proto`)

분산 런타임의 핵심 양방향 통신 서비스를 정의하는 Protobuf 스키마 파일 사양입니다.

```protobuf
syntax = "proto3";

package babyray;

// Baby Ray 핵심 통신 서비스
service BabyRayController {
  // 1. Heartbeat: Worker가 Head Node에 생존 신호 및 실시간 CPU/Memory 자원 점유 메트릭을 보고
  rpc SendHeartbeat (HeartbeatRequest) returns (HeartbeatResponse);
  
  // 2. Task Assignment: Head Node가 스케줄링 판단에 따라 Worker Node에 AI 연산 태스크 할당
  rpc AssignTask (TaskAssignRequest) returns (TaskAssignResponse);
  
  // 3. Task Status Query: Head Node가 분산 실행 중인 특정 태스크의 진행 현황을 지속적 모니터링
  rpc GetTaskStatus (TaskStatusRequest) returns (TaskStatusResponse);

  // [연구 확장] 4. Dynamic Resource Control: Head Node가 Worker Node의 cGroup 설정을 실시간 동적 튜닝
  rpc ResizeResources (ResourceResizeRequest) returns (ResourceResizeResponse);
}

// -------------------------------------------------------------
// 메시지 스키마 정의

// 1. Heartbeat 메시지
message HeartbeatRequest {
  string worker_id = 1;         // Worker 컨테이너 식별자 (예: "worker-node-1")
  double cpu_usage_percent = 2; // 실시간 CPU 사용량 (%)
  double memory_used_bytes = 3;  // 실시간 메모리 사용량 (Bytes)
  double memory_total_bytes = 4; // cGroup으로 할당 제한된 총 메모리 크기 (Bytes)
  int64 timestamp = 5;          // 전송 시간 (Epoch Unix 밀리초)
}

message HeartbeatResponse {
  bool status_acknowledged = 1; // 수신 완료 플래그
  string command = 2;           // 비상 정지 및 자원 해제 명령 지시 (예: "SHUTDOWN", "GC")
}

// 2. Task Assignment 메시지
message TaskAssignRequest {
  string task_id = 1;            // 고유 태스크 UUID
  string task_name = 2;          // 실행 태스크 명칭
  string model_type = 3;         // 학습 모델 유형 (예: "AI_Model")
  string dataset_path = 4;       // 학습 데이터셋 로컬 경로
  
  // 하이퍼파라미터 파라미터 조합 (JSON 또는 key-value 맵)
  map<string, string> hyperparameters = 5; // 예: {"learning_rate": "0.01", "batch_size": "64"}
  
  int32 thread_limit = 6;        // cGroup CPU 제한에 매핑할 PyTorch 연산 스레드 상한
  
  // [연구 확장] 가상 GPU 연산 비중 프로파일 팩터
  double virtual_gpu_scaling_factor = 7; // 예: 1.0 (A100 기준), 0.35 (T4 환산 지연 유도)
}

message TaskAssignResponse {
  string task_id = 1;
  bool success = 2;              // 태스크 배정 수락 여부
  string error_message = 3;      // 거절 사유 (예: "OOM_RISK", "LIMIT_EXCEEDED")
}

// 3. Task Status 메시지
message TaskStatusRequest {
  string task_id = 1;
}

message TaskStatusResponse {
  string task_id = 2;
  enum State {
    PENDING = 0;
    RUNNING = 1;
    SUCCESS = 2;
    FAILED = 3;
  }
  State state = 3;               // 현재 실행 상태
  double progress = 4;           // 진행률 (0.0 ~ 1.0)
  string result_summary = 5;     // 결과 요약 (예: "Loss: 0.05, Epoch: 100")
  string error_log = 6;          // 에러 발생 시 Stack Trace 로그
}

// [연구 확장] 4. Resource Dynamic Resize 메시지
message ResourceResizeRequest {
  double target_cpu_cores = 1;   // 동적으로 확장/제한할 타겟 CPU 코어 수
  int64 target_memory_bytes = 2; // 동적으로 확장/제한할 타겟 메모리 한도 (Bytes)
}

message ResourceResizeResponse {
  bool resize_success = 1;       // 리사이징 제어 반영 성공 플래그
  double current_cpu_cores = 2;  // 제어 후 실시간 CPU 코어 한도
  int64 current_memory_bytes = 3; // 제어 후 실시간 메모리 한도
}
```
