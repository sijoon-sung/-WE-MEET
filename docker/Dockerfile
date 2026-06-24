# 1. Base Image: PyTorch 및 CUDA 지원 공식 런타임 이미지
FROM pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime

# 작업 디렉토리 설정
WORKDIR /app

# 2. 시스템 패키지 업데이트 및 빌드에 필요한 도구 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 3. Python 의존성 설치 (캐시 최적화)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 소스 코드 복사
COPY . .

# 5. Protobuf 파일은 사전 컴파일되어 복사되므로 별도의 빌드 타임 컴파일 단계가 필요하지 않습니다.

# 6. 기본 명령어 (compose 등에서 실제 노드 타입에 맞게 덮어쓰기됨)
CMD ["python"]
