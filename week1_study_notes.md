# 📖 1주차 정리 노트: gRPC 핵심 기술 명세 및 개발 가이드

본 문서는 프로젝트 1주차 인프라 세팅 및 통신망 구축 단계를 위한 **gRPC 선행 학습 정리 노트이자 기술 실무 가이드**입니다.
gRPC의 기본 개념과 강점, 개발 환경 구축법, 그리고 본 프로젝트(Baby Ray)에서 어떻게 통신망을 개설하여 활용할 것인지 상세히 기술합니다.

---

## 1️⃣ gRPC란 무엇인가? (개념과 핵심 특징)

gRPC(Remote Procedure Call by Google)는 구글이 개발한 오픈소스 고성능 **원격 프로시저 호출(RPC) 프레임워크**입니다. 
네트워크 프로토콜 단을 직접 다루지 않고도, 로컬에 구현된 함수를 호출하듯 원격 서버의 함수를 직접 실행할 수 있게 해줍니다.

### 🌟 gRPC의 3대 핵심 차별화 요소

#### 1. HTTP/2 프로토콜 기반의 양방향 고속 통신
*   기존의 일반적인 Web API(REST API)는 HTTP/1.1 프로토콜을 사용하여 매 요청마다 3-Way Handshake 연결을 맺고 끊어 오버헤드가 크고 응답 속도가 느립니다.
*   gRPC는 **HTTP/2**를 사용하여 단일 연결 채널을 통해 여러 개의 메시지를 실시간으로 동시에 전송하는 **멀티플렉싱(Multiplexing)**을 지원하고, 헤더를 압축(HPACK)하여 대역폭 낭비를 획득 지점 이하로 줄입니다.

#### 2. Protobuf (Protocol Buffers) 직렬화를 통한 성능 극대화
*   기존 REST API는 사람이 읽을 수 있는 텍스트 기반의 **JSON** 형식을 주고받기 때문에 데이터 용량이 크고, 파싱하는 CPU 연산 비용이 비쌉니다.
*   gRPC는 **이진(Binary) 압축 직렬화 방식인 Protobuf**를 사용하므로 패킷의 용량이 JSON 대비 평균 3배~10배 이상 작고 직렬화 속도가 극도로 빠릅니다.

#### 3. 4가지 통신 스트리밍 형태 지원
*   **Simple RPC (Unary)**: 클라이언트가 1번 요청하고 서버가 1번 응답 (일반적인 API 호출)
*   **Server Streaming**: 클라이언트가 1번 요청하면, 서버가 지속적으로 데이터를 끊임없이 전송
*   **Client Streaming**: 클라이언트가 지속적으로 데이터를 전송하고, 서버가 1번 응답
*   **Bidirectional Streaming (양방향 스트리밍)**: 클라이언트와 서버가 서로 자유롭게 끊임없이 데이터를 송수신 (실시간 하트비트 및 제어망에 최적)

---

## 2️⃣ gRPC Python 기본 사용법 및 빌드 프로세스

분산 런타임(Baby Ray)의 Python 환경에서 gRPC 서비스를 코딩하고 빌드하는 구체적인 실무 개발 절차입니다.

### 1. 패키지 설치
Python 환경에서 gRPC 프로토콜 버퍼 빌더와 런타임 라이브러리를 설치합니다.
```bash
pip install grpcio grpcio-tools
```

### 2. 프로토콜 파일 (`baby_ray.proto`) 정의
통신 메시지 규격과 서비스를 선언합니다 (예시):
```protobuf
syntax = "proto3";

package babyray;

service BabyRayService {
  rpc PingPong (PingRequest) returns (PingResponse);
}

message PingRequest {
  string message = 1;
}

message PingResponse {
  string reply = 1;
}
```

### 3. Python gRPC 통신 코드 생성 (컴파일)
정의한 `.proto` 파일을 기반으로 Python에서 호출할 수 있는 Stub 및 클라이언트/서버 스켈레톤 코드를 자동 컴파일하여 생성합니다.
```bash
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. baby_ray.proto
```
*   **산출되는 파일**:
    *   `baby_ray_pb2.py`: 메시지 직렬화/역직렬화가 구현된 Python 클래스
    *   `baby_ray_pb2_grpc.py`: 서버/클라이언트 통신을 연동해주는 gRPC Stub 모듈

---

## 3️⃣ Baby Ray 프로젝트에서의 gRPC 구체적 활용 설계

본 프로젝트에서 Head Node와 Worker Node가 gRPC를 활용해 동작하는 3대 핵심 시나리오 설계입니다.

### 1. 양방향 실시간 생존 신고 및 자원 로깅 (Heartbeat API)
*   **목적**: Worker 노드들이 주기적으로 살아있는지 확인하고, 스케줄러가 부하 분산을 결정하기 위한 CPU/Mem 수치를 수집합니다.
*   **동작 흐름**:
    1. Worker 컨테이너가 기동되면 Head Node의 gRPC 서버 주소(`babyray-head:50050`)로 채널을 연결합니다.
    2. Worker의 모니터링 데몬이 `1초` 주기로 `SendHeartbeat` gRPC 요청을 호출하여 본인의 CPU/Memory 메트릭을 전달합니다.
    3. Head Node는 이를 수신하여 `Worker_Registry` 테이블의 `last_seen` 타임스탬프와 가용 자원 수치를 즉시 갱신합니다.

### 2. 자원 상태 인지형 AI 작업 할당 (AssignTask API)
*   **목적**: 대장 서버가 스케줄링 판단에 따라 연산 노드에 작업을 원격 지시합니다.
*   **동작 흐름**:
    1. 사용자가 Head Node에 AI 연산 요청을 보냅니다.
    2. 스케줄러가 `Worker_Registry`를 스캔하여 자원이 넉넉한 노드(예: Worker 1)를 선택합니다.
    3. Head Node는 Worker 1의 gRPC 주소로 `AssignTask` 요청을 보내어 학습할 AI 연산 명세와 하이퍼파라미터를 전송합니다.
    4. Worker 1의 Task Executor는 gRPC 요청을 수신하는 즉시 내부 PyTorch 모듈을 트리거하여 백그라운드 학습 스레드를 가동하고, 성공 여부를 응답합니다.

### 3. 노드 다운 시나리오 자가 복구 (Task Recovery API 연동)
*   **목적**: 노드 정지 시 작업을 살아있는 다른 노드에 재배치하여 고가용성(HA)을 보장합니다.
*   **동작 흐름**:
    1. Worker 1의 컨테이너가 정지(`docker stop`)되면 Head Node에 오던 Heartbeat API 요청이 두절됩니다.
    2. Head Node의 생존 모니터링 루프가 `last_seen` 타임이 3초 경과됨을 감지하고 Worker 1을 `DEAD` 상태로 전환합니다.
    3. GCS의 `Task_Lineage_DAG`를 조회해 Worker 1에서 돌고 있던 태스크를 색출합니다.
    4. 살아남은 Worker 2 노드에 해당 태스크 정보와 최신 체크포인트 경로를 담은 gRPC `AssignTask` 요청을 새롭게 전송하여 끊김 없는 증분 복구를 수행합니다.
