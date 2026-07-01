# WE-MEET 패치노트 (2026.07.01)

본 패치노트는 이기종 가상 클러스터 분산 학습 런타임인 **WE-MEET**의 스팟 인스턴스 이기종 확장 연동, 중복 워커 세션 충돌 방지, 호스트 시스템 자원 보호(Safety Guard) 상향 및 PyTorch 물리 GPU VRAM 할당 격리 기능 패치 내역을 기술합니다.

---

## 🛠️ 1. GCS 및 워커 관리 안정화

* **`RegisterWorker` 요청 시 중복 ID 검증 예외 처리 추가**:
  - **발생 현상 & 배경:** 기존에는 동일한 `worker_id`를 가진 워커가 실수로 중복 기동되는 경우, 검증 없이 GCS 레지스트리의 기존 정보를 덮어씌워 통신 세션 및 모니터링 메트릭이 꼬이는 장애가 있었습니다.
  - **해결 및 패치 내용:** [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 등록 API 내부에 중복 검증 로직을 신설하였습니다. 등록 시점에 `worker_id`가 이미 등록되어 있다면 경고 로그를 발생시키고 실패 응답(`success=False`)을 보내어 세션 침범을 완벽히 방어하였습니다.

---

## ⚡ 2. 이기종 Spot-B 워커 노드 동적 스케일링 완전 연동

* **스펙 정의 및 컨테이너 작명 분기**:
  - **해결 및 패치 내용:** [head/cluster_manager.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/cluster_manager.py)의 `scale_out_worker()` 함수 내에 **Spot-B (0.5 Core, 512MB RAM, 시작 포트 50070)** 설정을 추가하였습니다. 
  - 스케일아웃 시 `spot_a`는 `worker-2-*` (컨테이너명: `babyray-worker-2-*`), `spot_b`는 `worker-3-*` (컨테이너명: `babyray-worker-3-*`) 접두어를 각각 사용하도록 고유 ID 작명을 분기 처리하여, 부팅 시 좀비 컨테이너 일괄 소거 로직(`worker-2-`, `worker-3-` 감시)과 완벽하게 정합성을 맞췄습니다.
* **장애 시뮬레이터 및 Eviction Daemon 연동**:
  - 스팟 워커 증설 30% 확률 공급 부족(OutOfCapacity) 장애 모사 필터를 `spot_b`에도 동일하게 확장 적용했습니다.
  - 백그라운드 강제 회수 루프(`start_spot_eviction_loop`)의 대상 필터링 범위를 확장하여 `spot_b` 노드들도 정상적으로 무작위 강제 회수(Eviction)에 연동되도록 패치하였습니다.

---

## 🛡️ 3. 호스트 시스템 자원 보호 (Safety Guard) 및 확장 상한선 상향

* **호스트 물리 메모리 가드(Safety Guard) 기준 상향**:
  - **해결 및 패치 내용:** 스케일아웃 전 시스템 자원 상태를 검증하는 `is_host_resource_sufficient()`의 가용 메모리 임계 안전선을 기존 `2.0 GB`에서 **`3.0 GB`**로 상향하였습니다. 
  - 스팟 워커 1대 점유 한계(최대 1.0GB)와 호스트 OS/Head 노드/On-Demand 워커 등의 최소 작동 마진(2.0GB)을 합산하여, 메모리 과부하(Swap 지연 및 VM 다운)를 사전에 선제 차단하는 안전장치를 견고히 다졌습니다.
* **최대 스팟 스케일 상한선(`MAX_SPOT_SCALE`) 10대로 확장**:
  - [head/scheduler/core.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/scheduler/core.py)에 정의된 동적 스팟 워커의 최대 동시 가동 제한 수를 기존 3대에서 **10대**로 확장하여, 이기종 노드들이 풍폭넓게 병렬로 확장되는 시뮬레이션 동작을 풍부하게 관찰할 수 있도록 변경했습니다.

---

## 🧠 4. PyTorch 물리 GPU VRAM 할당 격리 제한 (VRAM Guard) 구현

* **물리 메모리 점유 가드 설정**:
  - **발생 현상 & 배경:** 기존에는 GPU 이기종 성능의 편차를 모사하기 위해 연산 후 인위적인 시간 지연(`sleep`)만 가하였을 뿐, 물리적으로 GPU 하드웨어 자원을 제어할 수 없는 한계가 있었습니다.
  - **해결 및 패치 내용:** [worker/gpu_simulator.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/gpu_simulator.py) 내부 `PyTorchTaskRunner.run()` 연산 개시부 직후에 `torch.cuda.set_per_process_memory_fraction` API를 사용해 각 워커가 물리적으로 접근할 수 있는 CUDA 메모리 한도를 강제 지정하였습니다.
  - **노드 등급별 VRAM 격리 제한:**
    - On-Demand: **4.0 GB (4096MB)**
    - Spot-A: **2.0 GB (2048MB)**
    - Spot-B: **1.0 GB (1024MB)**
  - 이로써 정해진 VRAM 한계를 넘어서는 텐서 적재 시 CUDA OOM 예외가 정상 발생하여 연산이 격리/실패하도록 하드웨어 리소스 가드를 구축하였습니다.
