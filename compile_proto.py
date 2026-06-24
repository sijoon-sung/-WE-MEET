import os
import sys
from grpc_tools import protoc

def compile_protobuf():
    print("=== Baby Ray Protobuf 컴파일 시작 ===")
    
    # 1. 경로 설정
    base_dir = os.path.abspath(os.path.dirname(__file__))
    proto_dir = os.path.join(base_dir, "proto")
    proto_file = os.path.join(proto_dir, "babyray.proto")
    
    if not os.path.exists(proto_file):
        print(f"[오류] Protobuf 정의 파일이 없습니다: {proto_file}")
        sys.exit(1)
        
    print(f"프로토 파일 경로: {proto_file}")
    
    # 2. grpc_tools.protoc 실행 인자 정의
    # 상대 경로로 통일하여 protoc include path 불일치 및 한글 경로 깨짐 오류를 방지합니다.
    arguments = [
        "grpc_tools.protoc",
        "-I.",
        "--python_out=.",
        "--grpc_python_out=.",
        "proto/babyray.proto"
    ]
    
    # 3. 컴파일러 호출
    print(f"컴파일 실행 명령 인자: {arguments}")
    exit_code = protoc.main(arguments)
    
    if exit_code == 0:
        print("[성공] Protobuf 컴파일이 완료되었습니다.")
        
        # 4. gRPC 파일 내 import 경로 보정 (Python absolute/relative import 이슈 해결)
        # grpc_tools.protoc가 생성한 babyray_pb2_grpc.py 내부에는 'import babyray_pb2 as babyray__pb2'로 
        # 코드 생성이 되어 이를 외부에서 'import proto.babyray_pb2_grpc' 할 때 모듈을 찾지 못하는 문제가 발생합니다.
        # 이를 'from proto import babyray_pb2 as babyray__pb2' 형태로 패치해 줍니다.
        grpc_file = os.path.join(proto_dir, "babyray_pb2_grpc.py")
        if os.path.exists(grpc_file):
            print(f"gRPC 임포트 경로 패치 대상: {grpc_file}")
            with open(grpc_file, "r", encoding="utf-8") as f:
                content = f.read()
            
            target_import = "import proto.babyray_pb2 as proto_dot_babyray__pb2"
            # 혹은 환경에 따라 'import babyray_pb2 as babyray__pb2' 형태로 나옴
            if "import babyray_pb2 as babyray__pb2" in content:
                content = content.replace(
                    "import babyray_pb2 as babyray__pb2",
                    "from proto import babyray_pb2 as babyray__pb2"
                )
                print("임포트 패치 완료: 'import babyray_pb2 as babyray__pb2' -> 'from proto import babyray_pb2'")
            elif "import proto.babyray_pb2 as proto_dot_babyray__pb2" in content:
                # 이미 올바르게 패키지명 포함된 경우
                print("임포트 경로가 이미 패키지를 포함하고 있습니다.")
            else:
                # 혹시 모를 다른 매칭 케이스 처리
                content = content.replace(
                    "import babyray_pb2 as babyray__pb2",
                    "from proto import babyray_pb2 as babyray__pb2"
                )
            
            with open(grpc_file, "w", encoding="utf-8") as f:
                f.write(content)
                
            print("=== Baby Ray Protobuf 컴파일 및 패치 종료 ===")
    else:
        print(f"[오류] Protobuf 컴파일에 실패했습니다. 에러 코드: {exit_code}")
        sys.exit(exit_code)

if __name__ == "__main__":
    compile_protobuf()
