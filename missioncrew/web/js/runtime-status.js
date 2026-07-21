/* ---- 系统全局 Runtime 实时状态 ---- */
let runtimeStatusLoading = false;
let runtimeHistoryLoadedAt = 0;

const RUNTIME_STATE_LABELS = {
  running: "执行中", starting: "启动中", idle: "空闲驻留",
  disconnected: "连接已断开", stopped: "未运行", disabled: "已停用",
  succeeded: "成功", failed: "失败", interrupted: "已中断",
};
const RUNTIME_TRANSPORT_LABELS = {
  "claude-stream-json": "Claude stream-json",
  "codex-app-server": "Codex app-server",
  "acp-stdio": "ACP stdio",
  "cli-command": "命令行执行",
};

function runtimeStateBadge(state) {
  return `<span class="runtime-state ${esc(state)}"><i></i>` +
    `${esc(RUNTIME_STATE_LABELS[state] || state)}</span>`;
}

function runtimeAge(timestamp, now) {
  if (!timestamp) return "—";
  const seconds = Math.max(0, Math.floor(now - timestamp));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  const hours = Math.floor(seconds / 3600);
  return `${hours}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function runtimeDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function runtimeSessionCell(instance) {
  const key = instance.session_key
    ? `<div><span class="muted">key</span> <code>${esc(instance.session_key)}</code></div>` : "";
  const native = instance.native_session_id
    ? `<div title="${esc(instance.native_session_id)}"><span class="muted">native</span> ` +
      `<code>${esc(instance.native_session_id)}</code></div>` : "";
  return key + native || `<span class="muted">—</span>`;
}

function renderRuntimeStatusPayload(data) {
  const summary = data.summary || {};
  const cards = [
    ["已注册后端", summary.backends || 0, `${summary.connected_backends || 0} 个持有实例`],
    ["活动实例", summary.live_instances || 0, `${summary.tracked_instances || 0} 个受跟踪`],
    ["正在执行", summary.running || 0, "包含启动中的实例"],
    ["持久实例", summary.persistent || 0, `${summary.one_shot || 0} 个单次执行`],
  ];
  document.getElementById("runtime-status-cards").innerHTML = cards.map(card =>
    `<div class="runtime-stat-card"><span>${esc(card[0])}</span><strong>${card[1]}</strong>` +
    `<small>${esc(card[2])}</small></div>`).join("");

  const stateOrder = { running: 0, idle: 1, stopped: 2, disabled: 3 };
  const backends = [...(data.backends || [])].sort((a, b) =>
    (stateOrder[a.state] ?? 9) - (stateOrder[b.state] ?? 9) || a.id.localeCompare(b.id));
  document.getElementById("runtime-status-backends").innerHTML =
    `<tr><th>Runtime</th><th>状态</th><th>项目</th><th>角色</th><th>实例</th>` +
    `<th>持久</th><th>单次</th><th>正在执行</th></tr>` +
    backends.map(backend => `<tr>
      <td><b>${esc(backend.id)}</b><br><span class="muted">${esc(backend.adapter)}</span></td>
      <td>${runtimeStateBadge(backend.state)}</td>
      <td>${esc((backend.projects || []).join(", ") || "—")}</td>
      <td>${esc((backend.roles || []).map(role => "@" + role).join(", ") || "—")}</td>
      <td>${backend.instances}</td><td>${backend.persistent}</td>
      <td>${backend.one_shot}</td><td>${backend.running}</td></tr>`).join("");

  const now = data.generated_at || Date.now() / 1000;
  const instances = data.instances || [];
  document.getElementById("runtime-status-instances").innerHTML =
    `<tr><th>状态</th><th>Runtime / 连接</th><th>模式</th><th>PID</th>` +
    `<th>项目 / 角色</th><th>会话</th><th>任务</th><th>工作目录</th><th>存活 / 最近活动</th></tr>` +
    (instances.map(instance => `<tr>
      <td>${runtimeStateBadge(instance.state)}</td>
      <td><b>${esc(instance.backend_id)}</b><br><span class="muted">` +
        `${esc(RUNTIME_TRANSPORT_LABELS[instance.transport] || instance.transport)}</span></td>
      <td><span class="pill">${instance.mode === "persistent" ? "持久实例" : "单次执行"}</span>` +
        `<br><span class="muted">${esc(instance.executable || "—")}</span></td>
      <td><code>${instance.pid || "—"}</code></td>
      <td>${esc(instance.project_id || "—")}` +
        `${instance.role_id ? `<br><span class="muted">@${esc(instance.role_id)}</span>` : ""}</td>
      <td class="runtime-session-cell">${runtimeSessionCell(instance)}</td>
      <td>${esc(instance.task_id || "—")}` +
        `${instance.stage_name ? `<br><span class="muted">${esc(instance.stage_name)}</span>` : ""}` +
        `${instance.model ? `<br><span class="muted">${esc(instance.model)}</span>` : ""}</td>
      <td><code class="runtime-path" title="${esc(instance.workdir || "")}">` +
        `${esc(instance.workdir || "—")}</code></td>
      <td>${runtimeAge(instance.started_at, now)}` +
        `<br><span class="muted">${runtimeAge(instance.last_activity, now)} 前</span></td>
    </tr>`).join("") || `<tr><td colspan="9" class="empty runtime-empty">当前没有 Runtime 实例；发起任务后会实时出现。</td></tr>`);

  document.getElementById("runtime-status-updated").textContent =
    `更新于 ${new Date(now * 1000).toLocaleTimeString()}`;
}

function renderRuntimeHistoryPayload(data) {
  const history = data.history || [];
  document.getElementById("runtime-history-count").textContent = history.length;
  document.getElementById("runtime-status-history").innerHTML =
    `<tr><th>结果</th><th>开始时间</th><th>Runtime / 连接</th><th>模式</th>` +
    `<th>项目 / 角色</th><th>任务</th><th>模型</th><th>耗时</th></tr>` +
    (history.map(item => `<tr>
      <td title="${esc(item.summary || "")}">${runtimeStateBadge(item.status)}</td>
      <td>${new Date(item.started_at * 1000).toLocaleString()}</td>
      <td><b>${esc(item.backend_id)}</b><br><span class="muted">` +
        `${esc(RUNTIME_TRANSPORT_LABELS[item.transport] || item.transport || item.adapter)}</span></td>
      <td><span class="pill">${item.mode === "persistent" ? "持久实例" : "单次执行"}</span>` +
        `${item.session_key ? `<br><code title="${esc(item.session_key)}">${esc(item.session_key)}</code>` : ""}</td>
      <td>${esc(item.project_id || "—")}` +
        `${item.role_id ? `<br><span class="muted">@${esc(item.role_id)}</span>` : ""}</td>
      <td>${esc(item.task_id || "—")}` +
        `${item.stage_name ? `<br><span class="muted">${esc(item.stage_name)}</span>` : ""}</td>
      <td>${esc(item.model || "默认")}` +
        `${item.effort ? `<br><span class="muted">effort=${esc(item.effort)}</span>` : ""}</td>
      <td>${runtimeDuration(item.duration_seconds)}</td>
    </tr>`).join("") ||
      `<tr><td colspan="8" class="empty runtime-empty">尚无 Runtime 使用记录。</td></tr>`);
}

async function renderRuntimeHistory(force = false) {
  if (!force && Date.now() - runtimeHistoryLoadedAt < 3000) return;
  const response = await fetch("/api/runtime/history?limit=100", { cache: "no-store" });
  if (!response.ok) return;
  renderRuntimeHistoryPayload(await response.json());
  runtimeHistoryLoadedAt = Date.now();
}

async function renderRuntimeStatus(force = false) {
  if (currentTab !== "runtime-status" && !force) return;
  if (runtimeStatusLoading) return;
  runtimeStatusLoading = true;
  try {
    const response = await fetch("/api/runtime/status", { cache: "no-store" });
    if (!response.ok) return;
    renderRuntimeStatusPayload(await response.json());
    await renderRuntimeHistory(force);
  } catch (_) {
    const updated = document.getElementById("runtime-status-updated");
    if (updated) updated.textContent = "服务暂时不可用，等待重试…";
  } finally {
    runtimeStatusLoading = false;
  }
}

function pollRuntimeStatus() {
  if (currentTab === "runtime-status") renderRuntimeStatus();
}
