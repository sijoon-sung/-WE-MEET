# 1. Base Image: PyTorch 및 CUDA 지원 공식 런타임 이미지
FROM pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime

# 작업 디렉토리 설정
WORKDIR /app

# 2. 시스템 패키지 업데이트 및 빌드에 필요한 도구/인프라 제어 클라이언트 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI 설치 (Head 노드에서 호스트 도커 제어용)
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-24.0.7.tgz | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/ \
    && rm -rf /tmp/docker

# Docker Compose CLI 플러그인 설치 (Head 노드의 스케일링 명령용)
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -SL https://github.com/docker/compose/releases/download/v2.24.5/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# 3. Python 의존성 설치 (캐시 최적화)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 소스 코드 복사
COPY . .

# 5. Protobuf 파일은 사전 컴파일되어 복사되므로 별도의 빌드 타임 컴파일 단계가 필요하지 않습니다.

# 6. 기본 명령어 (compose 등에서 실제 노드 타입에 맞게 덮어쓰기됨)
CMD ["python"]
