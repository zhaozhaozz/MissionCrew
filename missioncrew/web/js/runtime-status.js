/* ---- 系统全局 Runtime 实时状态 ---- */
let runtimeStatusLoading = false;
let runtimeHistoryLoadedAt = 0;
let runtimeUsageLoadedAt = 0;
let runtimeStatusSnapshot = null;

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
const RUNTIME_USAGE_STATUS_LABELS = {
  ok: "已连接", unavailable: "暂不可用", auth_required: "需要登录",
  disabled: "已停用", unsupported: "不支持",
};
const RUNTIME_USAGE_SOURCE_LABELS = {
  codex_app_server: "Codex app-server",
  claude_usage_command: "Claude /usage",
  kimi_usage_api: "Kimi usage API",
  grok_billing_api: "Grok billing API",
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

function refreshRuntimeIndicators(data = runtimeStatusSnapshot) {
  if (!data) return;
  runtimeStatusSnapshot = data;
  const activeRoles = new Set((data.instances || [])
    .filter(instance => ["starting", "running"].includes(instance.state)
      && instance.project_id && instance.role_id)
    .map(instance => `${instance.project_id}\u0000${instance.role_id}`));
  document.querySelectorAll(".role-running-marker").forEach(marker => {
    marker.hidden = !activeRoles.has(
      `${marker.dataset.runtimeProject}\u0000${marker.dataset.runtimeRole}`);
  });
  const count = Number(data.summary?.running || 0);
  const badge = document.getElementById("runtime-running-count");
  if (badge) {
    badge.textContent = String(count);
    badge.hidden = count === 0;
    badge.title = `${count} 个运行中的实例`;
  }
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

function runtimeUsageTimeProgress(window, generatedAt) {
  const durationSeconds = Number(window.duration_minutes) * 60;
  const resetsAt = Number(window.resets_at);
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0
      || !Number.isFinite(resetsAt) || resetsAt <= 0) return null;
  const startsAt = resetsAt - durationSeconds;
  return Math.max(0, Math.min(100,
    (generatedAt - startsAt) / durationSeconds * 100));
}

function runtimeUsagePercent(value) {
  return value.toFixed(value % 1 ? 1 : 0);
}

function runtimeUsageWindowTitle(window, generatedAt, used, remaining, elapsed) {
  const details = [
    window.label || "当前周期",
    `额度已用 ${runtimeUsagePercent(used)}%`,
    `额度剩余 ${runtimeUsagePercent(remaining)}%`,
  ];
  if (elapsed !== null) {
    details.push(`周期已过去 ${runtimeUsagePercent(elapsed)}%`);
  }
  if (window.resets_at) {
    details.push(`重置时间 ${new Date(window.resets_at * 1000).toLocaleString()}`);
  }
  return details.join(" · ");
}

function runtimeUsageTone(percent) {
  if (percent >= 90) return "critical";
  if (percent >= 70) return "warning";
  return "healthy";
}

function runtimeUsagePlan(plan) {
  const cleaned = String(plan || "").replace(/^(LEVEL|TYPE)_/, "").replaceAll("_", " ");
  if (!cleaned) return "";
  if (cleaned.toLowerCase() === "prolite") return "Pro Lite";
  return cleaned === cleaned.toUpperCase()
    ? cleaned.toLowerCase().replace(/\b\w/g, letter => letter.toUpperCase())
    : cleaned;
}

function runtimeUsageCard(item, generatedAt) {
  const status = item.status || "unavailable";
  const windows = item.windows || [];
  const source = RUNTIME_USAGE_SOURCE_LABELS[item.source] || item.source || "Runtime";
  const planLabel = runtimeUsagePlan(item.plan);
  const adapterClass = String(item.adapter || "runtime").replace(/[^\w-]/g, "");
  const plan = planLabel
    ? `<span class="runtime-usage-plan">${esc(planLabel)}</span>` : "";
  const metrics = (item.metrics || []).length
    ? `<span class="runtime-usage-title-metrics">${item.metrics.map(metric =>
      `<span title="${esc(`${metric.label}：${metric.value}`)}">` +
        `<small>${esc(metric.label)}</small><b>${esc(metric.value)}</b></span>`).join("")}</span>`
    : "";
  const body = status === "ok" && windows.length
    ? `<div class="runtime-usage-windows">${windows.map(window => {
      const used = Math.max(0, Math.min(100, Number(window.used_percent) || 0));
      const remaining = Math.max(0, 100 - used);
      const elapsed = runtimeUsageTimeProgress(window, generatedAt);
      const tone = runtimeUsageTone(used);
      const title = runtimeUsageWindowTitle(
        window, generatedAt, used, remaining, elapsed);
      const marker = elapsed === null ? "" :
        `<span class="runtime-usage-time-marker" style="left:${elapsed}%"></span>`;
      const resetsAt = Number(window.resets_at) > 0 ? Number(window.resets_at) : "";
      return `<div class="runtime-usage-window ${tone}"
        data-tip-label="${esc(window.label || "当前周期")}" data-tip-used="${used}"
        data-tip-elapsed="${elapsed === null ? "" : elapsed}" data-tip-resets="${resetsAt}">
        <div class="runtime-usage-window-head">
          <span>${esc(window.label || "当前周期")}</span>
          <span class="runtime-usage-values">
            <strong>${runtimeUsagePercent(used)}%</strong>
            <small>已用 · ${runtimeUsagePercent(remaining)}% 可用</small>
          </span>
        </div>
        <div class="runtime-usage-composite">
          <div class="runtime-usage-track" role="progressbar" aria-label="${esc(title)}"
            aria-valuemin="0" aria-valuemax="100" aria-valuenow="${used}">
            <i style="width:${used}%"></i>
          </div>
          ${marker}
        </div>
      </div>`;
    }).join("")}</div>`
    : `<div class="runtime-usage-empty">
        <i></i><span>${esc(item.message || "暂时无法读取账户限额")}</span>
      </div>`;
  return `<article class="runtime-usage-card adapter-${adapterClass}">
    <header>
      <div class="runtime-usage-identity">
        <span class="runtime-usage-mark">${esc((item.backend_name || item.backend_id || "?").slice(0, 1))}</span>
        <div class="runtime-usage-identity-copy">
          <div class="runtime-usage-title-row">
            <h3>${esc(item.backend_name || item.backend_id)}</h3>${metrics}
          </div>
          <small title="数据源：${esc(source)}">${esc(item.adapter || source)}</small></div>
      </div>
      <div class="runtime-usage-meta">${plan}
        <span class="runtime-usage-status ${esc(status)}"><i></i>${esc(RUNTIME_USAGE_STATUS_LABELS[status] || status)}</span>
      </div>
    </header>
    ${body}
  </article>`;
}

/* ---- 用量悬浮弹层:替代原生 title,悬浮即出的 HTML 提示,含额度重置倒计时 ---- */
const RUNTIME_USAGE_TIP_DELAY = 80;
let runtimeUsageTipEl = null;
let runtimeUsageTipShowTimer = 0;
let runtimeUsageTipCountdownTimer = 0;
let runtimeUsageTipTarget = null;

function runtimeUsageCountdown(resetsAt) {
  const total = Math.floor(resetsAt - Date.now() / 1000);
  if (total <= 0) return "已到重置时间";
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (days) return `${days} 天 ${hours} 小时 ${minutes} 分`;
  if (hours) return `${hours} 小时 ${minutes} 分 ${seconds} 秒`;
  return `${minutes} 分 ${seconds} 秒`;
}

function ensureRuntimeUsageTip() {
  if (!runtimeUsageTipEl) {
    runtimeUsageTipEl = document.createElement("div");
    runtimeUsageTipEl.className = "runtime-usage-tip";
    runtimeUsageTipEl.setAttribute("role", "tooltip");
    runtimeUsageTipEl.hidden = true;
    document.body.appendChild(runtimeUsageTipEl);
  }
  return runtimeUsageTipEl;
}

function runtimeUsageTipHtml(dataset) {
  const used = Math.max(0, Math.min(100, Number(dataset.tipUsed) || 0));
  const rows = [["额度剩余", `${runtimeUsagePercent(Math.max(0, 100 - used))}%`]];
  if (dataset.tipElapsed !== "") {
    const elapsed = Math.max(0, Math.min(100, Number(dataset.tipElapsed) || 0));
    rows.push(["周期已过去", `${runtimeUsagePercent(elapsed)}%`]);
  }
  const resetsAt = Number(dataset.tipResets) || 0;
  if (resetsAt) {
    rows.push(["重置时间", new Date(resetsAt * 1000).toLocaleString()]);
  }
  const countdown = resetsAt
    ? `<div class="runtime-usage-tip-reset"><span>距额度重置</span>
        <strong data-countdown="${resetsAt}">${esc(runtimeUsageCountdown(resetsAt))}</strong></div>`
    : "";
  return `<header>
      <span>${esc(dataset.tipLabel || "当前周期")}</span>
      <strong>${runtimeUsagePercent(used)}% 已用</strong>
    </header>
    <div class="runtime-usage-tip-bar ${runtimeUsageTone(used)}"><i style="width:${used}%"></i></div>
    <dl>${rows.map(row =>
      `<div><dt>${esc(row[0])}</dt><dd>${esc(row[1])}</dd></div>`).join("")}</dl>
    ${countdown}`;
}

function positionRuntimeUsageTip(target) {
  const tip = ensureRuntimeUsageTip();
  const rect = target.getBoundingClientRect();
  const left = Math.max(8, Math.min(rect.left + rect.width / 2 - tip.offsetWidth / 2,
    window.innerWidth - tip.offsetWidth - 8));
  const top = rect.top - tip.offsetHeight - 8;
  tip.style.left = `${left}px`;
  tip.style.top = `${top < 8 ? rect.bottom + 8 : top}px`;
}

function showRuntimeUsageTip(target) {
  const tip = ensureRuntimeUsageTip();
  runtimeUsageTipTarget = target;
  tip.innerHTML = runtimeUsageTipHtml(target.dataset);
  tip.hidden = false;
  positionRuntimeUsageTip(target);
  clearInterval(runtimeUsageTipCountdownTimer);
  const countdownEl = tip.querySelector("[data-countdown]");
  if (countdownEl) {
    runtimeUsageTipCountdownTimer = setInterval(() => {
      countdownEl.textContent =
        runtimeUsageCountdown(Number(countdownEl.dataset.countdown));
    }, 1000);
  }
}

function hideRuntimeUsageTip() {
  if (!runtimeUsageTipTarget && !runtimeUsageTipShowTimer) return;
  clearTimeout(runtimeUsageTipShowTimer);
  clearInterval(runtimeUsageTipCountdownTimer);
  runtimeUsageTipShowTimer = 0;
  runtimeUsageTipCountdownTimer = 0;
  runtimeUsageTipTarget = null;
  if (runtimeUsageTipEl) runtimeUsageTipEl.hidden = true;
}

// 事件委托:用量卡片会整体重渲染,监听放在 document 上保持有效。
document.addEventListener("mouseover", event => {
  const target = event.target.closest?.(".runtime-usage-window[data-tip-label]");
  if (!target || target === runtimeUsageTipTarget) return;
  hideRuntimeUsageTip();
  runtimeUsageTipShowTimer = setTimeout(
    () => showRuntimeUsageTip(target), RUNTIME_USAGE_TIP_DELAY);
});
document.addEventListener("mouseout", event => {
  const target = event.target.closest?.(".runtime-usage-window[data-tip-label]");
  if (target && !target.contains(event.relatedTarget)) hideRuntimeUsageTip();
});
// 弹层为 fixed 定位,页面滚动时直接收起,避免位置漂移。
document.addEventListener("scroll", hideRuntimeUsageTip, true);

function renderRuntimeUsagePayload(data) {
  hideRuntimeUsageTip();
  const usage = data.usage || [];
  const available = usage.filter(item => item.status === "ok").length;
  document.getElementById("runtime-usage-count").textContent =
    `${available}/${usage.length} 可用`;
  document.getElementById("runtime-usage-cards").innerHTML =
    usage.map(item => runtimeUsageCard(item, data.generated_at || Date.now() / 1000)).join("") ||
    `<div class="runtime-usage-loading">当前没有支持账户限额读取的 Runtime。</div>`;
  document.getElementById("runtime-usage-updated").textContent =
    `读取于 ${new Date((data.generated_at || Date.now() / 1000) * 1000).toLocaleTimeString()}`;
}

async function renderRuntimeHistory(force = false) {
  if (!force && Date.now() - runtimeHistoryLoadedAt < 3000) return;
  const response = await fetch("/api/runtime/history?limit=100", { cache: "no-store" });
  if (!response.ok) return;
  renderRuntimeHistoryPayload(await response.json());
  runtimeHistoryLoadedAt = Date.now();
}

async function renderRuntimeUsage(force = false) {
  if (!force && Date.now() - runtimeUsageLoadedAt < 3000) return;
  const query = force ? "?refresh=true" : "";
  const response = await fetch(`/api/runtime/usage${query}`, { cache: "no-store" });
  if (!response.ok) return;
  renderRuntimeUsagePayload(await response.json());
  runtimeUsageLoadedAt = Date.now();
}

async function renderRuntimeStatus(force = false) {
  if (runtimeStatusLoading) return;
  runtimeStatusLoading = true;
  try {
    const response = await fetch("/api/runtime/status", { cache: "no-store" });
    if (!response.ok) return;
    const data = await response.json();
    refreshRuntimeIndicators(data);
    if (currentTab === "runtime-status" || force) {
      renderRuntimeStatusPayload(data);
      const refreshes = [renderRuntimeHistory(force)];
      // 账户限额只在进入页面或手动刷新时读取，不跟随 10 秒状态轮询。
      if (force) refreshes.push(renderRuntimeUsage(true));
      await Promise.all(refreshes);
    }
  } catch (_) {
    const updated = document.getElementById("runtime-status-updated");
    if (updated) updated.textContent = "服务暂时不可用，等待重试…";
  } finally {
    runtimeStatusLoading = false;
  }
}

function pollRuntimeStatus() {
  renderRuntimeStatus();
}
