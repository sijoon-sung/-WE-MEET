# 1. PyTorch CUDA 가속 공식 런타임 베이스 이미지 선택 (CUDA 12.1 호환)
FROM pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime

# 2. 작업 디렉토리 설정
WORKDIR /workspace

# 3. 호스트의 requirements.txt 파일을 복사하여 패키지 설치
COPY requirements.txt /workspace/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 4. 소스 코드 복사 (Docker Compose의 볼륨 마운트와 호환되나 이미지 자체에도 유지)
COPY . /workspace

# 5. gRPC 포트 대역 및 생존 리포팅 포트 노출
EXPOSE 50051 50052 50053
