/* 运行过程卡片:Agent 运行的实时状态与过程事件(命令/思考/工具/用量/权限交互),
   内联在触发消息之后,可折叠、实时刷新。频道聊天(sidebar.js)与项目配置聊天
   (project-configs.js)通过 syncRuns 共用这套渲染,须在两者之前加载。 */
const runCards = new Map();   // run_id -> {el, key, userToggled}
const RUN_EVENT_META = {
  command:     { label: "命令", cls: "re-command" },
  input:       { label: "输入", cls: "re-input" },
  thinking:    { label: "思考", cls: "re-thinking" },
  tool:        { label: "工具", cls: "re-tool" },
  tool_result: { label: "结果", cls: "re-tool" },
  plan:        { label: "计划", cls: "re-plan" },
  file_change: { label: "文件", cls: "re-file" },
  usage:       { label: "用量", cls: "re-status" },
  permission_request: { label: "权限", cls: "re-interaction" },
  user_input_request: { label: "提问", cls: "re-interaction" },
  backend_agent: { label: "后端 Agent", cls: "re-backend-agent" },
  text:        { label: "输出", cls: "re-text" },
  stdout:      { label: "输出", cls: "re-tool" },
  // stderr 是多数 Agent CLI 的进度/日志通道(codex 连思考都走这里),
  // 不是错误:中性展示,失败与否由卡片头部的状态与错误摘要表达
  stderr:      { label: "日志", cls: "re-log" },
  status:      { label: "状态", cls: "re-status" },
};
function runSummary(run) {
  const st = { queued: "排队中", running: "运行中", waiting_user: "等待用户",
               done: "已完成", failed: "失败", stopped: "已停止" }[run.status] || run.status;
  const live = ["queued", "running", "waiting_user"].includes(run.status);
  const secs = run.finished_at ? ` · ${Math.max(1, Math.round(run.finished_at - run.created_at))}s` : "";
  // 执行组合:runtime · model(/effort);旧记录没盖章时只显示 runtime
  const combo = [
    run.backend_id ? esc(run.backend_id) : "…",
    run.model ? esc(run.model) + (run.effort ? `/${esc(run.effort)}` : "") : "",
  ].filter(Boolean).join(" · ");
  return `<span class="rc-dot ${live ? "live" : run.status}">●</span>
    <b style="color:${roleColor[run.role_id] || "var(--muted)"}">@${esc(run.role_id)}</b>
    <span class="muted">${combo} · ${st}${secs}</span>
    ${run.error ? `<span class="rc-err">${esc(run.error).slice(0, 120)}</span>` : ""}
    ${live ? `<button class="rc-stop" data-run-id="${run.id}" title="停止本次运行并终止其 Runtime 进程(原生会话保留)"
      onclick="stopChatRun(event, this.dataset.runId)">停止</button>` : ""}`;
}

async function stopChatRun(event, runId) {
  // summary 上的点击默认会折叠/展开卡片,先拦下来
  event.preventDefault();
  event.stopPropagation();
  if (!await uiConfirm(
      "停止这次 Agent 运行,并终止它的 Runtime 进程?原生会话 ID 会保留,已完成的文件修改不会自动回滚。",
      "停止本次运行")) return;
  try {
    const result = await api("POST", `/api/chat/runs/${runId}/stop`);
    toast(result.stopped_runtimes
      ? `已停止 @${result.role_id} 的运行,并终止 ${result.stopped_runtimes} 个 Runtime 进程`
      : `已停止 @${result.role_id} 的运行`,
      result.runtime_errors ? "error" : "success");
  } catch (e) { /* api() 已提示错误 */ }
  await pollMessages();
}

function parseStructuredRunEvent(event) {
  try { return JSON.parse(event.content); } catch (_) { return null; }
}

function interactionDetails(payload) {
  const details = payload.details || {};
  const label = payload.tool || details.reason || details.command || payload.request_type || "Runtime 请求";
  const raw = Object.keys(details).length ? JSON.stringify(details, null, 2) : "";
  return `<div class="ri-title">${esc(label)}</div>` +
    (raw ? `<details class="ri-details"><summary>查看请求详情</summary><pre>${esc(raw)}</pre></details>` : "");
}

function renderBackendAgent(payload) {
  const status = payload.status || "running";
  const statusLabel = { running: "运行中", waiting: "等待后台 Agent",
    progress: "执行中", completed: "已完成", failed: "失败", stopped: "已停止" }[status] || status;
  const description = payload.description || payload.agent_type || "Claude backend Agent";
  const summary = payload.summary ? `<div class="rba-summary">${esc(payload.summary)}</div>` : "";
  const details = [payload.agent_type, payload.last_tool_name ? `工具：${payload.last_tool_name}` : "",
    payload.pending ? `剩余：${payload.pending}` : "",
    usageInlineSummary(payload.usage)].filter(Boolean).join(" · ");
  return `<div class="rba-head"><b>${esc(description)}</b><span class="rba-status ${esc(status)}">${esc(statusLabel)}</span></div>` +
    (details ? `<div class="muted">${esc(details)}</div>` : "") + summary;
}

function renderPermissionRequest(run, event, payload) {
  const status = payload.status || "pending";
  const pending = status === "pending";
  const statusLabel = { pending: "等待决定", auto_approved: "MissionCrew YOLO 已自动批准",
    denied: "已按策略拒绝", resolved: `已处理：${payload.decision || ""}`,
    timeout: "等待超时，已取消", stopped: "频道运行已停止" }[status] || status;
  const actions = !pending ? "" : `<div class="ri-actions">
    <button onclick="sendRuntimeInteraction(${run.id},'${esc(payload.request_id)}','approve')">批准一次</button>
    ${payload.can_approve_session ? `<button onclick="sendRuntimeInteraction(${run.id},'${esc(payload.request_id)}','approve_session')">本会话批准</button>` : ""}
    <button class="danger" onclick="sendRuntimeInteraction(${run.id},'${esc(payload.request_id)}','deny')">拒绝</button>
  </div>`;
  return `<span class="ri-status ${esc(status)}">${esc(statusLabel)}</span>
    ${interactionDetails(payload)}${actions}`;
}

function renderUserInputRequest(run, event, payload) {
  const pending = (payload.status || "pending") === "pending";
  const questions = (payload.questions || []).map((question, index) => {
    const qid = String(question.id ?? index);
    const inputType = question.isSecret ? "password" : "text";
    const optionType = question.multiSelect ? "checkbox" : "radio";
    const options = (question.options || []).map(option => `<label class="ri-option">
      <input type="${optionType}" name="ri-${esc(payload.request_id)}-${esc(qid)}"
        value="${esc(option.label || option)}"> <span>${esc(option.label || option)}</span>
      ${option.description ? `<small>${esc(option.description)}</small>` : ""}</label>`).join("");
    return `<div class="ri-question" data-question-id="${esc(qid)}">
      <b>${esc(question.header || `问题 ${index + 1}`)}</b>
      <div>${esc(question.question || "")}</div>${options}
      <input class="ri-other" type="${inputType}" placeholder="${options ? "其他回答（可选）" : "请输入回答"}">
    </div>`;
  }).join("");
  const status = payload.status || "pending";
  const actions = pending ? `<div class="ri-actions">
    <button class="action" onclick="submitRuntimeAnswers(${run.id},'${esc(payload.request_id)}',this)">提交回答</button>
    <button onclick="sendRuntimeInteraction(${run.id},'${esc(payload.request_id)}','cancel')">取消</button>
  </div>` : `<div class="ri-status ${esc(status)}">${{
    timeout: "等待超时，已取消", stopped: "频道运行已停止",
  }[status] || "回答已提交"}</div>`;
  return `${questions}${actions}`;
}

/* ---- 用量事件 ----
   Runtime 层已在源头把各工具的字段名归一成 usage/v1:turn(本轮)/total(累计)两个
   区段用固定字段 input/cache_read/cache_write/output/reasoning/total,另有
   context_window、cost_usd、tool_uses、duration_ms,raw 原样保留上报内容。这里只做
   排版;没有 schema 标记的旧事件或未知结构由调用方原样展示 JSON。 */
const USAGE_SCHEMA = "usage/v1";
const USAGE_FIELD_LABELS = { input: "输入", cache_read: "缓存命中", cache_write: "缓存写入",
  output: "输出", reasoning: "推理", total: "合计" };
const USAGE_SCOPE_LABELS = { turn: "本轮", total: "累计" };
// 这些字段为 0 是常态(没走缓存、模型不推理),为 0 时不占位
const USAGE_HIDE_ZERO = new Set(["cache_read", "cache_write", "reasoning"]);

function fmtTokenCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  const abs = Math.abs(n);
  const trim = text => text.replace(/\.?0+$/, "");
  // 999,950 起按 k 取一位小数会四舍五入成 1000k,直接进位到 M
  if (abs >= 1e6 || Math.round(abs / 100) >= 10000) return trim((n / 1e6).toFixed(2)) + "M";
  if (abs >= 1e4) return trim((n / 1e3).toFixed(1)) + "k";
  return n.toLocaleString();
}

function fmtDurationMs(value) {
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms < 0) return String(value);
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${(ms / 1000).toFixed(1).replace(/\.0$/, "")}s`;
  const mins = Math.floor(secs / 60);
  return mins < 60 ? `${mins}m${secs % 60 ? `${secs % 60}s` : ""}`
    : `${Math.floor(mins / 60)}h${mins % 60 ? `${mins % 60}m` : ""}`;
}

function fmtUsageCost(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  // 1 美元以上到分;小额保留 4 位再去掉尾零(至少到分);再小只标"< $0.0001"
  if (n > 0 && n < 0.0001) return "< $0.0001";
  if (n >= 1) return "$" + n.toFixed(2);
  return "$" + n.toFixed(4).replace(/0+$/, "").replace(/\.(\d)$/, ".$10").replace(/\.$/, ".00");
}

const isUsagePayload = payload =>
  Boolean(payload) && typeof payload === "object" && payload.schema === USAGE_SCHEMA;
const usageNumber = value =>
  (value === null || value === undefined || value === "" ? NaN : Number(value));

// 一个区段(turn/total)里可展示的字段,按固定顺序;每项 [标签, 展示文本, 原值]
function usageSectionEntries(section) {
  if (!section || typeof section !== "object") return [];
  return Object.keys(USAGE_FIELD_LABELS).filter(key => {
    const value = usageNumber(section[key]);
    return Number.isFinite(value) && !(USAGE_HIDE_ZERO.has(key) && value === 0);
  }).map(key => [USAGE_FIELD_LABELS[key], fmtTokenCount(section[key]), section[key]]);
}

// 工具调用 / 耗时 / 费用三个非 token 指标(费用只在有正值时展示)
function usageMetaEntries(payload) {
  const entries = [];
  const toolUses = usageNumber(payload.tool_uses);
  const duration = usageNumber(payload.duration_ms);
  const cost = usageNumber(payload.cost_usd);
  if (Number.isFinite(toolUses)) entries.push(["工具调用", `${toolUses.toLocaleString()} 次`, toolUses]);
  if (Number.isFinite(duration)) entries.push(["耗时", fmtDurationMs(duration), duration]);
  if (cost > 0) entries.push(["费用", fmtUsageCost(cost), cost]);
  return entries;
}

const usageEntriesText = entries =>
  entries.map(([label, text]) => label === "费用" ? text : `${label} ${text}`).join(" · ");
const usageEntriesHtml = entries => entries.map(([label, text, value]) =>
  `<span class="ru-metric" title="${esc(Number(value).toLocaleString())}"><em>${esc(label)}</em>${esc(text)}</span>`).join("");

/* 排版一份 usage/v1 payload。返回 null 表示不是该结构或没有可展示项(调用方原样
   展示 JSON);否则返回 {html, preview}:html 是分区段视图,preview 是折叠时的一句话。 */
function renderUsagePayload(payload) {
  if (!isUsagePayload(payload)) return null;
  const rows = Object.entries(USAGE_SCOPE_LABELS)
    .map(([scope, label]) => ({ scope, label, entries: usageSectionEntries(payload[scope]) }))
    .filter(row => row.entries.length);
  const meta = usageMetaEntries(payload);
  // 上下文占用:本轮合计(缺省用输入+输出)对比模型上下文窗口
  const turn = payload.turn || {};
  const contextWindow = usageNumber(payload.context_window);
  const used = usageNumber(turn.total)
    || (usageNumber(turn.input) || 0) + (usageNumber(turn.output) || 0);
  let contextHtml = "", contextPreview = "";
  if (contextWindow > 0 && used > 0) {
    const percent = Math.min(100, Math.round(used / contextWindow * 100));
    const tone = percent >= 90 ? "critical" : percent >= 70 ? "warning" : "healthy";
    contextPreview = `上下文 ${percent}%`;
    contextHtml = `<div class="ru-row ru-context"><span class="ru-scope">上下文</span>` +
      `<div class="ru-track ${tone}" role="progressbar" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100"><i style="width:${percent}%"></i></div>` +
      `<span class="ru-metric" title="${esc(used.toLocaleString())} / ${esc(contextWindow.toLocaleString())} tokens">` +
      `${esc(fmtTokenCount(used))} / ${esc(fmtTokenCount(contextWindow))} · ${percent}%</span></div>`;
  }
  if (!rows.length && !meta.length && !contextHtml) return null;
  const rowsHtml = rows.map(row =>
    `<div class="ru-row"><span class="ru-scope">${esc(row.label)}</span>${usageEntriesHtml(row.entries)}</div>`).join("");
  const metaHtml = meta.length
    ? `<div class="ru-row">${rows.length ? `<span class="ru-scope"></span>` : ""}${usageEntriesHtml(meta)}</div>` : "";
  const rawHtml = payload.raw === undefined ? ""
    : `<details class="ru-raw"><summary>原始上报</summary><pre>${esc(JSON.stringify(payload.raw, null, 2))}</pre></details>`;
  // 摘要:两个区段都有合计时各报一个数,否则列出唯一区段的字段
  const totals = rows.filter(row => Number.isFinite(usageNumber(payload[row.scope].total)));
  const preview = [
    ...(rows.length > 1 && totals.length > 1
      ? totals.map(row => `${row.label} ${fmtTokenCount(payload[row.scope].total)}`)
      : rows.length ? [usageEntriesText(rows[0].entries)] : []),
    contextPreview,
    usageEntriesText(meta),
  ].filter(Boolean).join(" · ");
  return { html: `<div class="ru">${rowsHtml}${contextHtml}${metaHtml}${rawHtml}</div>`,
           preview: preview || "Token 用量" };
}

// 后台 Agent 卡片详情行里的一句话用量;旧格式原样以 JSON 附上
function usageInlineSummary(usage) {
  if (!usage || typeof usage !== "object" || !Object.keys(usage).length) return "";
  if (!isUsagePayload(usage)) return JSON.stringify(usage);
  return usageEntriesText([...usageSectionEntries(usage.total || usage.turn),
                           ...usageMetaEntries(usage)]);
}

function runEventPreview(event) {
  const text = String(event.content || "").replace(/\s+/g, " ").trim();
  if (event.kind === "input") return `${event.content.length.toLocaleString()} 字符 · 完整原文`;
  if (!text) return "无内容";
  return text.length > 100 ? text.slice(0, 100) + "…" : text;
}

function renderRunEvent(run, event, openEventId) {
  const meta = RUN_EVENT_META[event.kind] || { label: event.kind, cls: "re-status" };
  let content = esc(event.content);
  let preview = runEventPreview(event);
  if (event.kind === "permission_request") {
    const payload = parseStructuredRunEvent(event);
    if (payload) {
      content = renderPermissionRequest(run, event, payload);
      preview = payload.status === "pending" ? "等待用户决定" : `已处理 · ${payload.status}`;
    }
  } else if (event.kind === "user_input_request") {
    const payload = parseStructuredRunEvent(event);
    if (payload) {
      content = renderUserInputRequest(run, event, payload);
      preview = payload.status === "pending" ? "等待用户回答" : `已处理 · ${payload.status}`;
    }
  } else if (event.kind === "usage") {
    const payload = parseStructuredRunEvent(event);
    if (payload) {
      // usage/v1 按区段/字段排版;旧格式或未知结构保持原始 JSON 不丢信息
      const friendly = renderUsagePayload(payload);
      content = friendly ? friendly.html : esc(JSON.stringify(payload, null, 2));
      preview = friendly ? friendly.preview : "Token 用量";
    }
  } else if (event.kind === "backend_agent") {
    const payload = parseStructuredRunEvent(event);
    if (payload) {
      const label = { running: "运行中", waiting: "等待汇总", progress: "执行中",
        completed: "已完成", failed: "失败", stopped: "已停止" }[payload.status] || payload.status;
      content = renderBackendAgent(payload);
      preview = `${payload.description || payload.agent_type || "Claude backend Agent"} · ${label || ""}`;
    }
  }
  const open = String(event.id) === openEventId ? " open" : "";
  return `<details class="re re-fold ${meta.cls}" data-event-id="${event.id}"${open}>` +
    `<summary><span class="re-k">${esc(meta.label)}</span>` +
    `<span class="re-fold-size">${esc(preview)}</span></summary>` +
    `<div class="re-content">${content}</div></details>`;
}

async function sendRuntimeInteraction(runId, requestId, decision, answers = {}) {
  const card = runCards.get(runId)
    || (typeof findConfigChatRunCard === "function" ? findConfigChatRunCard(runId) : null);
  card?.el.querySelectorAll(".ri-actions button").forEach(button => button.disabled = true);
  try {
    await api("POST", `/api/chat/runs/${runId}/interactions/${encodeURIComponent(requestId)}`,
      { decision, answers });
    if (card) card.key = null;
    await pollMessages();
    if (typeof pollConfigChat === "function") await pollConfigChat();
  } catch (error) {
    card?.el.querySelectorAll(".ri-actions button").forEach(button => button.disabled = false);
  }
}

function submitRuntimeAnswers(runId, requestId, button) {
  const root = button.closest(".re-interaction");
  const answers = {};
  root.querySelectorAll(".ri-question").forEach(question => {
    const values = [...question.querySelectorAll("input[type=radio]:checked,input[type=checkbox]:checked")]
      .map(input => input.value);
    const other = question.querySelector(".ri-other")?.value.trim();
    if (other) values.push(other);
    answers[question.dataset.questionId] = values;
  });
  sendRuntimeInteraction(runId, requestId, "submit", answers);
}

/* 运行期间的实时输出:把 text 过程事件聚合成一个跟随更新的输出框,
   一轮运行只有一个(消息之间是 Runtime 拼好的 Markdown 横线),不随
   每次输出新建气泡;运行结束后移除,由正式发布的 Agent 消息接替展示。 */
function syncRunLiveOutput(run, card, pane, events, liveOutput) {
  const existing = pane.querySelector(`.run-live-output[data-run-id="${run.id}"]`);
  // 以 card 上同步记录的最新状态为准:事件请求在途时运行可能已经结束,
  // 晚到的回调不能按调用时捕获的旧状态把刚移除的实时框重新建回来
  const live = ["queued", "running", "waiting_user"]
    .includes(card.runStatus || run.status);
  // 优先用接口全量拼接的 live_output:过程事件窗口只保留最近 N 行,
  // 长运行里最早的输出段会被挤出窗口,按事件拼接会丢开头
  const text = !live ? ""
    : liveOutput != null ? String(liveOutput)
    : events.filter(e => e.kind === "text").map(e => e.content).join("");
  if (!text.trim()) { existing?.remove(); return; }
  let bubble = existing;
  if (!bubble) {
    const color = roleColor[run.role_id] || "#888";
    bubble = document.createElement("div");
    bubble.className = "msg agent run-live-output";
    bubble.dataset.runId = run.id;
    bubble.innerHTML = `<span class="avatar" style="background:${esc(color)}">${esc((run.role_id[0] || "?").toUpperCase())}</span>
      <div class="msg-main">
        <div class="head"><span class="author" style="color:${esc(color)}">@${esc(run.role_id)}</span>
          <span class="via">运行中 · 过程输出实时更新</span></div>
        <div class="body markdown-body"></div>
      </div>`;
    // 多个 Agent 并行时各自的输出框以角色色左描边区分归属
    bubble.querySelector(".body").style.borderLeftColor = color;
    card.el.after(bubble);
  }
  // 不限高、整体随消息流展开;贴底跟随由外层消息面板统一处理
  bubble.querySelector(".body").innerHTML = fmtBody(text, true);
}

async function renderRunEvents(run, card, pane = document.getElementById("msgs")) {
  // 静默拉取(不弹 toast,服务重启间隙下轮重试);成功才返回 true,
  // 调用方据此提交 card.key,失败时下轮按 key 未变化重试
  try {
    const r = await fetch(`/api/chat/runs/${run.id}/events`);
    if (!r.ok) return false;
    const d = await r.json();
    const outerNear = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120;
    const body = card.el.querySelector(".rc-events");
    const innerNear = !body.childElementCount ||
      body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    const events = d.events;
    const latestEventId = events.length ? String(events[events.length - 1].id) : "";
    const newItem = latestEventId && latestEventId !== card.latestEventId;
    const openEventId = newItem ? latestEventId
      : card.openEventId === undefined ? latestEventId : card.openEventId;
    body.innerHTML = events.map(e => renderRunEvent(run, e, openEventId)).join("")
      || `<div class="re re-status">(暂无过程输出)</div>`;
    card.latestEventId = latestEventId;
    card.openEventId = openEventId;
    body.querySelectorAll(".re-fold").forEach(details => {
      details.addEventListener("toggle", () => {
        const eventId = details.dataset.eventId;
        if (details.open) {
          body.querySelectorAll(".re-fold[open]").forEach(other => {
            if (other !== details) other.open = false;
          });
          card.openEventId = eventId;
        } else if (card.openEventId === eventId) {
          card.openEventId = null;
        }
      });
    });
    syncRunLiveOutput(run, card, pane, events, d.live_output);
    // 内外滚动都只在原本贴底时跟随,不打断正在回看历史的读者
    if (innerNear) body.scrollTop = body.scrollHeight;
    if (outerNear) pane.scrollTop = pane.scrollHeight;
    return true;
  } catch (_) { return false; }
}

function syncRuns(runs, surface = null) {
  const pane = surface?.pane || document.getElementById("msgs");
  const cards = surface?.runCards || runCards;
  const nearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120;
  for (const run of runs) {
    let card = cards.get(run.id);
    if (!card) {
      // 内联定位:触发消息之后、同触发的更早卡片之后。触发消息还没加载
      // (增量未拉到,或更早历史尚未向上翻页)时先不建卡,消息就位后再挂
      let anchor = pane.querySelector(`[data-msg-id="${run.trigger_message_id}"]`);
      if (!anchor) continue;
      const el = document.createElement("details");
      el.className = "run-card";
      el.dataset.trigger = run.trigger_message_id;
      el.dataset.runId = run.id;
      el.innerHTML = `<summary></summary><div class="rc-events"></div>`;
      card = { el, key: null, userToggled: false, fetching: false,
               latestEventId: "", openEventId: undefined,
               runStatus: run.status };
      el.querySelector("summary").addEventListener("click", () => { card.userToggled = true; });
      el.addEventListener("toggle", () => {   // 展开时过程流贴底显示最新
        if (el.open) { const b = el.querySelector(".rc-events"); b.scrollTop = b.scrollHeight; }
      });
      // 更早运行的卡片连同其实时输出框一起越过,保持按 run id 排序
      while ((anchor.nextElementSibling?.classList?.contains("run-card")
              || anchor.nextElementSibling?.classList?.contains("run-live-output"))
             && Number(anchor.nextElementSibling.dataset.runId) < run.id)
        anchor = anchor.nextElementSibling;
      anchor.after(el);
      cards.set(run.id, card);
    }
    const live = ["queued", "running", "waiting_user"].includes(run.status);
    card.runStatus = run.status;
    // 运行结束在同步路径立即移除实时输出框:与本轮 appendMessages 挂上的
    // 正式消息同一渲染周期完成交接,不闪重复,也不依赖事件请求成功
    if (!live)
      pane.querySelector(`.run-live-output[data-run-id="${run.id}"]`)?.remove();
    const key = `${run.status}:${run.events_size}`;
    if (card.key !== key && !card.fetching) {
      card.el.querySelector("summary").innerHTML = runSummary(run);
      if (!card.userToggled) card.el.open = live;   // 运行中自动展开,结束自动收起
      if (run.events_size > 0 || !live) {
        card.fetching = true;
        renderRunEvents(run, card, pane).then(ok => {
          card.fetching = false;
          if (ok) card.key = key;
        });
      } else {
        card.key = key;
      }
    }
  }
  if (nearBottom) pane.scrollTop = pane.scrollHeight;
}
