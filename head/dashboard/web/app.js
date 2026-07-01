/* ==============================================================================
 * WE-MEET: BabyRay Premium Live Dashboard Controller (app.js)
 * ============================================================================== */

const API_URL = "/api/status";
let lastLogCount = 0;
const activeWorkersMap = new Map(); // workerId -> DOM Element mapping

// 실시간 로그 하이라이팅 파서
function colorizeLog(msg) {
    if (msg.includes("[DEAD") || msg.includes("경고") || msg.includes("실패") || msg.includes("Error") || msg.includes("에러") || msg.includes("장애")) {
        return `<span style="color: #f43f5e; font-weight: 600;">${msg}</span>`;
    }
    if (msg.includes("완료 성공") || msg.includes("성공") || msg.includes("SUCCESS") || msg.includes("가동") || msg.includes("연결 성공") || msg.includes("복구")) {
        return `<span style="color: #10b981; font-weight: 500;">${msg}</span>`;
    }
    if (msg.includes("[Scheduler Action]") || msg.includes("SCALE_OUT") || msg.includes("SCALE_IN") || msg.includes("트리거") || msg.includes("스케일아웃") || msg.includes("증설") || msg.includes("회수")) {
        return `<span style="color: #c084fc; font-weight: 600;">${msg}</span>`;
    }
    if (msg.includes("Heartbeat 수신") || msg.includes("하트비트")) {
        return `<span style="color: #475569; font-size: 0.8rem;">${msg}</span>`;
    }
    return `<span style="color: #cbd5e1;">${msg}</span>`;
}

// 워커 카드 DOM 생성
function createWorkerCard(wid, info) {
    const card = document.createElement("div");
    card.className = "worker-card scale-up-enter";
    card.id = `worker-card-${wid}`;

    const now = Date.now() / 1000;
    const hbAge = Math.max(0, now - info.last_heartbeat).toFixed(1);
    const isWarning = hbAge > 5.0;

    const statusClass = info.status === "IDLE" ? "status-idle" : "status-busy";
    const typeClass = `type-${info.node_type}`;
    const typeName = info.node_type === "on_demand" ? "On-Demand" : (info.node_type === "spot_a" ? "Spot-A" : "Spot-B");

    card.innerHTML = `
        <div class="worker-title-row">
            <span class="worker-id">${wid}</span>
            <span class="worker-badge-type ${typeClass}">${typeName}</span>
        </div>
        
        <div class="worker-status ${statusClass}" id="w-status-${wid}">
            ${info.status} ${isWarning ? '(지연)' : ''}
        </div>

        <div class="worker-stats">
            <div class="worker-stat-bar">
                <span>CPU 사용률</span>
                <div style="display:flex; align-items:center;">
                    <span id="w-cpu-val-${wid}">${(info.cpu || 0).toFixed(1)}%</span>
                    <div class="worker-mini-bar">
                        <div class="worker-mini-fill" id="w-cpu-bar-${wid}" style="width: ${info.cpu || 0}%; background-color: var(--accent-blue);"></div>
                    </div>
                </div>
            </div>
            <div class="worker-stat-bar">
                <span>Memory 사용률</span>
                <div style="display:flex; align-items:center;">
                    <span id="w-mem-val-${wid}">${(info.mem || 0).toFixed(1)}%</span>
                    <div class="worker-mini-bar">
                        <div class="worker-mini-fill" id="w-mem-bar-${wid}" style="width: ${info.mem || 0}%; background-color: var(--accent-purple);"></div>
                    </div>
                </div>
            </div>
        </div>

        <div class="worker-heartbeat" id="w-hb-${wid}">
            <span>Last Heartbeat</span>
            <span class="hb-seconds">${hbAge}초 전</span>
        </div>
    `;

    // 0.1초 뒤 트랜지션 엔터 효과
    requestAnimationFrame(() => {
        setTimeout(() => {
            card.classList.remove("scale-up-enter");
            card.classList.add("scale-up-enter-active");
        }, 50);
    });

    return card;
}

// 워커 카드 DOM 업데이트
function updateWorkerCard(wid, card, info) {
    const now = Date.now() / 1000;
    const hbAge = Math.max(0, now - info.last_heartbeat).toFixed(1);
    const isWarning = hbAge > 5.0;

    // Status 배지
    const statusBadge = card.querySelector(`#w-status-${wid}`);
    if (statusBadge) {
        statusBadge.className = `worker-status ${info.status === "IDLE" ? "status-idle" : "status-busy"}`;
        statusBadge.innerText = `${info.status} ${isWarning ? '(지연)' : ''}`;
    }

    // CPU / Mem 리드 수치 갱신
    const cpuVal = card.querySelector(`#w-cpu-val-${wid}`);
    const cpuBar = card.querySelector(`#w-cpu-bar-${wid}`);
    if (cpuVal) cpuVal.innerText = `${(info.cpu || 0).toFixed(1)}%`;
    if (cpuBar) cpuBar.style.width = `${info.cpu || 0}%`;

    const memVal = card.querySelector(`#w-mem-val-${wid}`);
    const memBar = card.querySelector(`#w-mem-bar-${wid}`);
    if (memVal) memVal.innerText = `${(info.mem || 0).toFixed(1)}%`;
    if (memBar) memBar.style.width = `${info.mem || 0}%`;

    // 하트비트 세컨드
    const hbBlock = card.querySelector(`#w-hb-${wid}`);
    if (hbBlock) {
        if (isWarning) {
            hbBlock.style.color = "var(--accent-red)";
            hbBlock.style.fontWeight = "700";
        } else {
            hbBlock.style.color = "var(--text-muted)";
            hbBlock.style.fontWeight = "400";
        }
        const secondsSpan = hbBlock.querySelector(".hb-seconds");
        if (secondsSpan) secondsSpan.innerText = `${hbAge}초 전`;
    }
}

// 워커 노드 실시간 갱신 오케스트레이션 (Scale-in/out 트랜지션 탑재)
function reconcileWorkers(workersData) {
    const container = document.getElementById("workers-container");
    const incomingWorkerIds = new Set(Object.keys(workersData));

    // 1. Placeholder 제거 (첫 기동 시)
    const placeholder = container.querySelector(".empty-placeholder");
    if (placeholder && incomingWorkerIds.size > 0) {
        container.removeChild(placeholder);
    }

    // 2. Scale-Out & Update
    incomingWorkerIds.forEach(wid => {
        const info = workersData[wid];
        if (!activeWorkersMap.has(wid)) {
            // New worker detected -> Scale-Out animation!
            const newCard = createWorkerCard(wid, info);
            container.appendChild(newCard);
            activeWorkersMap.set(wid, newCard);
        } else {
            // Existing worker -> update metrics only
            const card = activeWorkersMap.get(wid);
            updateWorkerCard(wid, card, info);
        }
    });

    // 3. Scale-In (제거 및 축소 페이드아웃 애니메이션)
    activeWorkersMap.forEach((card, wid) => {
        if (!incomingWorkerIds.has(wid)) {
            // Worker gone -> Scale-In animation!
            card.classList.remove("scale-up-enter-active");
            card.classList.add("scale-down-exit-active");
            
            // 0.5초 트랜지션 완료 후 DOM에서 삭제
            setTimeout(() => {
                if (card.parentNode === container) {
                    container.removeChild(card);
                }
                activeWorkersMap.delete(wid);

                // 만약 모든 워커가 다 제거되었다면 placeholder 다시 복구
                if (activeWorkersMap.size === 0) {
                    container.innerHTML = `
                        <div class="empty-placeholder">
                            <div class="empty-icon">🔌</div>
                            <p>활성화된 워커 노드가 존재하지 않습니다.<br>하트비트 신호를 대기하고 있습니다.</p>
                        </div>
                    `;
                }
            }, 500);
        }
    });
}

// 실시간 대시보드 데이터 취합 및 업데이트 루프
async function updateDashboard() {
    try {
        const response = await fetch(API_URL);
        if (!response.ok) throw new Error("GCS HTTP status error");
        
        const data = await response.json();
        
        // 1. GCS Status 상태 바인딩
        const statusContainer = document.getElementById("status-container");
        const statusText = document.getElementById("status-text");
        statusContainer.className = "system-status online";
        statusText.innerText = "GCS ONLINE";

        // 2. Budget 예산 갱신
        if (data.virtual_budget !== undefined) {
            document.getElementById("budget-val").innerHTML = `$${data.virtual_budget.toFixed(4)}<span class="currency">USD</span>`;
        }

        // 3. Host Resource
        if (data.host_cpu !== undefined) {
            document.getElementById("host-cpu-val").innerText = `${data.host_cpu.toFixed(1)}%`;
            document.getElementById("host-cpu-bar").style.width = `${data.host_cpu}%`;
        }
        if (data.host_mem !== undefined) {
            document.getElementById("host-mem-val").innerText = `${data.host_mem.toFixed(1)}%`;
            document.getElementById("host-mem-bar").style.width = `${data.host_mem}%`;
        }
        if (data.gpu_free_vram !== undefined) {
            const freeVram = data.gpu_free_vram;
            if (freeVram === -1) {
                document.getElementById("gpu-vram-val").innerText = "N/A (No GPU)";
                document.getElementById("gpu-vram-bar").style.width = "0%";
            } else {
                document.getElementById("gpu-vram-val").innerText = `${freeVram} MiB`;
                // 8GB를 100% 기준치로 상정
                const pct = Math.min(100, (freeVram / 8192) * 100);
                document.getElementById("gpu-vram-bar").style.width = `${pct}%`;
            }
        }

        // 4. Workers Reconciler
        reconcileWorkers(data.workers || {});

        // 5. Task Queue 갱신
        const queueContainer = document.getElementById("queue-container");
        const queueList = data.queue || [];
        document.getElementById("queue-count").innerText = `대기 ${queueList.length}개`;

        if (queueList.length === 0) {
            queueContainer.innerHTML = `
                <div class="empty-placeholder">
                    <div class="empty-icon">📥</div>
                    <p>대기열이 비어 있습니다.<br>클라이언트 부하 유입을 모니터링 중입니다.</p>
                </div>
            `;
        } else {
            let html = "";
            const now = Date.now() / 1000;
            
            queueList.forEach(task => {
                const secondsLeft = Math.max(0, task.deadline - now);
                const isOverdue = secondsLeft <= 0;
                const timeText = isOverdue ? "Deadline 초과!" : `${secondsLeft.toFixed(1)}초 남음`;
                
                // 가상 스케일 30초
                const fillPct = isOverdue ? 0 : Math.min(100, (secondsLeft / 30) * 100);
                
                let barColor = "var(--accent-green)";
                if (secondsLeft < 12) barColor = "orange";
                if (secondsLeft < 5 || isOverdue) barColor = "var(--accent-red)";

                html += `
                    <div class="task-card" style="${isOverdue ? 'border-color: rgba(244, 63, 94, 0.35);' : ''}">
                        <div class="task-header">
                            <span class="task-id">${task.task_id}</span>
                            <span class="task-model-badge">${task.model_type}</span>
                        </div>
                        <div class="task-details">
                            <span>Epochs: ${task.epochs}회</span>
                            <span style="font-weight: 700; color: ${barColor}">${timeText}</span>
                        </div>
                        <div class="task-deadline-bar">
                            <div class="task-deadline-fill" style="width: ${fillPct}%; background-color: ${barColor};"></div>
                        </div>
                    </div>
                `;
            });
            queueContainer.innerHTML = html;
        }

        // 6. Console Event Stream 갱신
        const logs = data.logs || [];
        if (logs.length !== lastLogCount) {
            const consoleLogs = document.getElementById("console-logs");
            let logsHtml = "";
            logs.forEach(log => {
                logsHtml += `<div class="console-line">${colorizeLog(log)}</div>`;
            });
            consoleLogs.innerHTML = logsHtml;
            consoleLogs.scrollTop = consoleLogs.scrollHeight;
            lastLogCount = logs.length;
        }

    } catch (err) {
        const statusContainer = document.getElementById("status-container");
        const statusText = document.getElementById("status-text");
        statusContainer.className = "system-status offline";
        statusText.innerText = "GCS DISCONNECTED";
        console.error("Dashboard refresh disconnected:", err);
    }
}

// 1.2초 주기로 타이트하게 동적 모니터링 갱신 기동
setInterval(updateDashboard, 1200);
updateDashboard();
