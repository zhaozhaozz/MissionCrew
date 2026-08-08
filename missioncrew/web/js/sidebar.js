/* ---- 侧栏分区的快捷操作 ---- */
function quickCreateChannel() {
  openChannelDialog();
}

function requestBoardFocus() {   // 面板由主控创建:跳到需求输入框
  switchTab("custom");
  setTimeout(() => document.getElementById("board-request")?.focus(), 80);
}

function quickNewDocument() {
  if (currentTab !== "docs") switchTab("docs");
  newDocument();
}

function quickNewRole() {
  switchTab("proj");
  setTimeout(() => editRole(null), 300);   // 等 traits 元数据与表单渲染就绪
}

function scrollToSec(id) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function gotoProjSection(elemId) {
  switchTab("proj");
  setTimeout(() => document.getElementById(elemId)
    ?.scrollIntoView({ behavior: "smooth", block: "start" }), 300);
}

function quickAddResource() {
  openResourceDialog();
}

function openResourceDialog() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  openFormDialog("添加资源", `
    <label>本地路径或 git 远程地址(本地路径若是 git 仓,自动绑定其远程仓库)</label>
    <div class="row">
      <div><input type="text" id="res-target" placeholder="~/code/myrepo 或 https://github.com/acme/x.git"></div>
      <div style="flex:0 0 90px"><button class="ghost" style="width:100%"
        onclick="openDirPicker(document.getElementById('res-target').value)">浏览…</button></div>
    </div>
    <label>名称(可选)</label><input type="text" id="res-name">`,
    `<button class="action" onclick="addResourceFromForm()">添加资源</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
  setTimeout(() => document.getElementById("res-target")?.focus(), 60);
}

// 本地目录选择弹窗:逐级浏览,支持显示隐藏目录;确认后回填资源输入框
let _pickHidden = false;
let _pickTargetInputId = "res-target";

function openDirPicker(startPath, targetInputId = "res-target") {
  _pickHidden = false;
  _pickTargetInputId = targetInputId;
  document.getElementById("ddlg-hidden").classList.remove("on");
  ddlg.showModal();
  pickBrowse(startPath || "");
}

async function pickBrowse(path) {
  let d;
  try {
    d = await api("GET", `/api/fs/dirs?path=${encodeURIComponent((path || "").trim())}` +
                          `&hidden=${_pickHidden}`);
  } catch (e) { return; }
  window._pickPath = d.path;
  // 面包屑:每一段都可点击跳转到对应上级目录
  const parts = d.path.split("/").filter(Boolean);
  let acc = "";
  const crumbs = [`<span class="crumb" data-p="/" onclick="pickBrowse(this.dataset.p)">/</span>`]
    .concat(parts.map(p => {
      acc += "/" + p;
      return `<span class="crumb" data-p="${esc(acc)}" onclick="pickBrowse(this.dataset.p)">${esc(p)}</span>`;
    })).join(`<span style="opacity:.45">/</span>`).replace(
      `</span><span style="opacity:.45">/</span>`, `</span>`);  // 根后不重复斜杠
  const gitInfo = !d.is_git ? "" :
    ` <span class="pill" style="color:var(--ok);border-color:var(--ok)">git 仓</span>` +
    ((d.remotes || []).map(r =>
      `<div class="muted" style="font-size:12px;margin-top:2px">🔗 ${esc(r.name)}: ${esc(r.url)}</div>`).join("")
     || `<div class="muted" style="font-size:12px;margin-top:2px">(未配置远程)</div>`);
  document.getElementById("ddlg-path").innerHTML = crumbs + gitInfo;
  document.getElementById("ddlg-list").innerHTML =
    (d.parent ? `<div class="side-item" data-p="${esc(d.parent)}" onclick="pickBrowse(this.dataset.p)">⬆ 上一级</div>` : "") +
    (d.dirs.map(n => `<div class="side-item" data-p="${esc(d.path)}/${esc(n)}"
        onclick="pickBrowse(this.dataset.p)">📁 ${esc(n)}</div>`).join("")
      || `<div class="empty" style="padding:8px">无子目录</div>`);
}

function togglePickHidden(el) {
  el.classList.toggle("on");
  _pickHidden = el.classList.contains("on");
  pickBrowse(window._pickPath);
}

function pickDirConfirm() {
  const input = document.getElementById(_pickTargetInputId);
  if (input && window._pickPath) input.value = window._pickPath;
  ddlg.close();
}

function openGuidelineFromSidebar(name) {
  editGuideline(name);
}

function openSkillFromSidebar(id) {
  editSkill(id);
}

function quickNewGuideline() {
  editGuideline(null);
}

function quickNewSkill() {
  editSkill(null);
}

function openBoardFromSidebar(id) {
  currentCustomBoard = id;
  customBoardEditing = false;
  boardEditorVisible = false;
  if (currentTab === "custom") {
    renderCustomBoards(true);
    renderSidebar();
    syncUrl();
  } else switchTab("custom");
}

function openPanelFromSidebar(kind, id) {
  if (kind === "tasks") {
    switchTab("board");
    return;
  }
  openBoardFromSidebar(id);
}

async function openDocFromSidebar(path) {
  if (currentTab === "docs" && path !== docSelected && !await confirmDocDiscard()) return;
  docSelected = path;
  docMode = "view";
  docViewer.activate();
  if (currentTab === "docs") { renderDocPane(); renderSidebar(); syncUrl(); }
  else switchTab("docs");
}

let savedComposerRange = null;
let mentionPickerRoles = [];
let mentionPickerIndex = 0;

function roleInfo(id) {
  return projRoles().find(role => role.id === id) || null;
}

function legalMentionTitle(id) {
  const role = roleInfo(id);
  if (role?.enabled === false)
    return `${roleDisabledReason(role)}：@${id}${role.name ? `（${role.name}）` : ""}，不会触发新执行`;
  return `已确认提及：单选会触发 @${id}${role?.name ? `（${role.name}）` : ""}，多选由主控协调`;
}

function createComposerMention(id) {
  const role = roleInfo(id);
  if (!role || role.enabled === false) return null;
  const span = document.createElement("span");
  span.className = "mention legal-mention mention-compose";
  span.contentEditable = "false";
  span.dataset.roleId = id;
  span.title = legalMentionTitle(id);
  span.style.color = role.color || roleColor[id] || "var(--accent)";
  span.textContent = `@${id}`;
  return span;
}

function composerContainsNode(box, node) {
  const element = node?.nodeType === Node.TEXT_NODE ? node.parentNode : node;
  return element === box || box.contains(element);
}

function rememberComposerSelection() {
  const box = document.getElementById("input");
  const selection = window.getSelection();
  if (!selection?.rangeCount) return;
  const range = selection.getRangeAt(0);
  if (composerContainsNode(box, range.commonAncestorContainer))
    savedComposerRange = range.cloneRange();
}

function composerInsertionRange() {
  const box = document.getElementById("input");
  const selection = window.getSelection();
  if (savedComposerRange && composerContainsNode(box, savedComposerRange.commonAncestorContainer)) {
    selection.removeAllRanges();
    selection.addRange(savedComposerRange);
    return savedComposerRange.cloneRange();
  }
  const range = document.createRange();
  range.selectNodeContents(box);
  range.collapse(false);
  return range;
}

function activeMentionQuery() {
  const box = document.getElementById("input");
  const selection = window.getSelection();
  if (!selection?.rangeCount) return null;
  const range = selection.getRangeAt(0);
  if (!range.collapsed || !composerContainsNode(box, range.startContainer)
      || range.startContainer.nodeType !== Node.TEXT_NODE) return null;
  const prefix = range.startContainer.nodeValue.slice(0, range.startOffset);
  const match = prefix.match(/(^|[^\w@])@([\w-]*)$/u);
  if (!match) return null;
  const start = match.index + match[1].length;
  return { node: range.startContainer, start, end: range.startOffset,
           query: match[2].toLowerCase() };
}

function hideMentionPicker() {
  const picker = document.getElementById("mention-picker");
  picker.hidden = true;
  picker.innerHTML = "";
  mentionPickerRoles = [];
  mentionPickerIndex = 0;
}

function renderMentionPicker(query) {
  const picker = document.getElementById("mention-picker");
  mentionPickerRoles = activeProjRoles().filter(role => !query
    || role.id.toLowerCase().includes(query)
    || String(role.name || "").toLowerCase().includes(query));
  if (!mentionPickerRoles.length) { hideMentionPicker(); return; }
  mentionPickerIndex = Math.min(mentionPickerIndex, mentionPickerRoles.length - 1);
  picker.innerHTML = mentionPickerRoles.map((role, index) =>
    `<button type="button" role="option" class="${index === mentionPickerIndex ? "active" : ""}"
       data-role-id="${esc(role.id)}" aria-selected="${index === mentionPickerIndex}"
       onmousedown="event.preventDefault();chooseComposerMention(this.dataset.roleId)">
       <span class="role-dot" style="background:${esc(role.color || "#888")}"></span>
       <b>@${esc(role.id)}</b><span>${esc(role.name || "")}</span>
       <small>${esc(role.description || "")}</small></button>`).join("");
  picker.hidden = false;
}

function updateMentionPicker() {
  const query = activeMentionQuery();
  if (!query) { hideMentionPicker(); return; }
  mentionPickerIndex = 0;
  renderMentionPicker(query.query);
}

function moveMentionPicker(delta) {
  if (!mentionPickerRoles.length) return;
  mentionPickerIndex = (mentionPickerIndex + delta + mentionPickerRoles.length)
    % mentionPickerRoles.length;
  renderMentionPicker(activeMentionQuery()?.query || "");
  document.querySelector("#mention-picker button.active")?.scrollIntoView({ block: "nearest" });
}

function insertComposerMention(id) {
  const box = document.getElementById("input");
  const mention = createComposerMention(id);
  if (!mention) return;
  box.focus();
  let range = composerInsertionRange();
  const query = activeMentionQuery();
  if (query) {
    range = document.createRange();
    range.setStart(query.node, query.start);
    range.setEnd(query.node, query.end);
  }
  range.deleteContents();

  const before = range.cloneRange();
  before.selectNodeContents(box);
  before.setEnd(range.startContainer, range.startOffset);
  if (before.toString() && !/\s$/u.test(before.toString())) {
    const space = document.createTextNode(" ");
    range.insertNode(space);
    range.setStartAfter(space);
    range.collapse(true);
  }
  range.insertNode(mention);
  const trailing = document.createTextNode(" ");
  range.setStartAfter(mention);
  range.collapse(true);
  range.insertNode(trailing);
  range.setStartAfter(trailing);
  range.collapse(true);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  savedComposerRange = range.cloneRange();
  hideMentionPicker();
}

function chooseComposerMention(id) {
  insertComposerMention(id);
}

function insertMention(id) {
  if (roleInfo(id)?.enabled === false) {
    toast(`@${id} 已停用，请先在项目设置中启用`, "error");
    return;
  }
  if (projChannels().find(channel => channel.id === currentChan)?.archived) {
    toast("频道已归档，请先恢复后再发送消息", "error");
    return;
  }
  if (currentTab !== "chat") switchTab("chat");
  insertComposerMention(id);
}

function selectChannel(id, jump = true) {
  currentChan = id; lastMsgId = 0; lastMsgDate = "";
  firstMsgId = 0; chanHasEarlier = false;
  runCards.clear();
  updateChatRunControls([]);
  document.getElementById("msgs").innerHTML = "";
  if (jump && currentTab !== "chat") switchTab("chat");
  renderSidebar(); pollMessages();
  syncUrl();
}

function fmtBody(text, markdown = false, mentionSpans = []) {
  // 只有后端验证过的精确范围会变成提及徽标；正文里的其他 @xxx 保持普通文字。
  const source = String(text ?? "");
  const chars = Array.from(source);
  const mentions = [];
  let held = "";
  let cursor = 0;
  const spans = [...(Array.isArray(mentionSpans) ? mentionSpans : [])]
    .sort((a, b) => Number(a.start) - Number(b.start));
  for (const span of spans) {
    const id = String(span.role_id || "");
    const start = Number(span.start), end = Number(span.end);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < cursor || end > chars.length
        || chars.slice(start, end).join("") !== `@${id}`) continue;
    held += chars.slice(cursor, start).join("");
    const color = roleInfo(id)?.color || roleColor[id] || "var(--accent)";
    mentions.push(`<span class="mention legal-mention" style="color:${esc(color)}" ` +
      `title="${esc(legalMentionTitle(id))}">@${esc(id)}</span>`);
    held += `\uE100${mentions.length - 1}\uE101`;
    cursor = end;
  }
  held += chars.slice(cursor).join("");
  const html = markdown ? miniMarkdown(held) : esc(held);
  return html.replace(/\uE100(\d+)\uE101/g, (_, index) => mentions[Number(index)]);
}

let lastMsgDate = "";   // 聊天流的日期分隔线:与上一条消息不同天时插入
const MESSAGE_FOLD_AT = 4000;

function agentExecutionLabel(message) {
  return `runtime=${message.runtime_id || "未记录"} · ` +
    `model=${message.model || "CLI 默认"} · ` +
    `effort=${message.effort || "CLI 默认"}`;
}

function toggleMessageBody(button) {
  const body = button.previousElementSibling;
  const expanded = body.classList.toggle("expanded");
  button.setAttribute("aria-expanded", String(expanded));
  button.textContent = expanded ? "收起长回复" : `展开完整回复（${button.dataset.size} 字符）`;
}

function appendMessagesToSurface(list, surface) {
  const pane = surface.pane;
  if (list.length) pane.querySelector(".chat-empty")?.remove();
  const nearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120;
  for (const m of list) {
    const d = new Date(m.created_at * 1000);
    const day = d.toLocaleDateString();
    if (day !== surface.lastMsgDate) {
      surface.lastMsgDate = day;
      const sep = document.createElement("div");
      sep.className = "date-sep";
      sep.innerHTML = `<span>${esc(day)}</span>`;
      pane.appendChild(sep);
    }
    if (m.kind === "context_boundary") {
      const boundary = document.createElement("div");
      boundary.className = "context-boundary";
      boundary.dataset.msgId = m.id;
      boundary.innerHTML = `<span>${esc(m.content || "上下文已清除")}</span>`;
      pane.appendChild(boundary);
      surface.lastMsgId = Math.max(surface.lastMsgId, m.id);
      continue;
    }
    const isAgent = m.author_type === "agent";
    const isHuman = m.author_type === "human";
    const isToolReceipt = m.author_type === "platform" && m.kind === "agent_tool";
    const color = isAgent ? (roleColor[m.author] || "#888")
                : isHuman ? "var(--accent)" : "var(--muted)";
    const name = isAgent ? "@" + m.author
      : isToolReceipt ? "MissionCrew Tool"
      : m.author_type === "platform" ? "系统" : m.author;
    const initial = isAgent || isHuman ? (m.author[0] || "?").toUpperCase() : "⚙";
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const longReply = isAgent && m.content.length > MESSAGE_FOLD_AT;
    const renderMarkdown = isAgent || isToolReceipt;
    const div = document.createElement("div");
    div.className = `msg ${m.author_type}`;
    div.dataset.msgId = m.id;   // 运行过程卡片按触发消息内联定位
    div.innerHTML = `<span class="avatar" style="background:${color}">${esc(initial)}</span>
      <div class="msg-main">
        <div class="head"><span class="author" style="color:${isAgent ? color : "var(--text)"}">${esc(name)}</span>
          ${isAgent ? `<span class="via">${esc(agentExecutionLabel(m))}</span>` : ""}<span class="time">${time}</span></div>
        <div class="body${renderMarkdown ? " markdown-body" : ""}${longReply ? " folded" : ""}">${fmtBody(m.content, renderMarkdown, m.mention_spans)}</div>
        ${longReply ? `<button type="button" class="message-fold-toggle" data-size="${m.content.length}"
          aria-expanded="false" onclick="toggleMessageBody(this)">展开完整回复（${m.content.length} 字符）</button>` : ""}
      </div>`;
    pane.appendChild(div);
    surface.lastMsgId = Math.max(surface.lastMsgId, m.id);
  }
  if (list.length && surface.channelId) {
    const channel = overview.channels.find(item => item.id === surface.channelId);
    if (channel)
      channel.last_message_at = Math.max(channelActivity(channel),
        ...list.map(message => Number(message.created_at || 0)));
    renderSidebar();
  }
  if (list.length && nearBottom) pane.scrollTop = pane.scrollHeight;
}

function appendMessages(list) {
  // 并发轮询兜底:切频道瞬间可能有两轮请求同时在途,已渲染的 id 直接丢弃
  list = list.filter(message => Number(message.id) > lastMsgId);
  const surface = {
    pane: document.getElementById("msgs"),
    channelId: currentChan,
    lastMsgDate,
    lastMsgId,
  };
  appendMessagesToSurface(list, surface);
  lastMsgDate = surface.lastMsgDate;
  lastMsgId = surface.lastMsgId;
}

/* ---- 向上翻页:滚动到顶部时按 before_id 取更早历史,插入到消息流顶部 ---- */
function prependMessages(list) {
  if (!list.length) return;
  const pane = document.getElementById("msgs");
  const holder = document.createElement("div");
  // 复用追加渲染:在离屏容器里从空日期状态渲染这一批,再整体挂到顶部
  const surface = { pane: holder, channelId: null, lastMsgDate: "", lastMsgId: 0 };
  appendMessagesToSurface(list, surface);
  // 拼接处同一天时去掉原顶部的日期分隔线,避免重复
  const first = pane.firstElementChild;
  if (first?.classList.contains("date-sep")
      && first.textContent.trim() === surface.lastMsgDate) first.remove();
  const prevHeight = pane.scrollHeight, prevTop = pane.scrollTop;
  pane.prepend(...holder.childNodes);
  pane.scrollTop = prevTop + (pane.scrollHeight - prevHeight);
  firstMsgId = list[0].id;
}

async function loadEarlierMessages() {
  if (!currentChan || !chanHasEarlier || chanLoadingEarlier || !firstMsgId) return;
  const chan = currentChan;
  chanLoadingEarlier = true;
  const pane = document.getElementById("msgs");
  const notice = document.createElement("div");
  notice.className = "chat-earlier-loading";
  notice.textContent = "正在加载更早的消息…";
  pane.prepend(notice);
  try {
    const r = await fetch(`/api/chat/${chan}/messages?before_id=${firstMsgId}`);
    if (!r.ok || chan !== currentChan) return;
    const d = await r.json();
    if (chan !== currentChan) return;
    chanHasEarlier = Boolean(d.has_earlier);
    notice.remove();   // 先移除提示再插入,保证滚动位置补偿量准确
    prependMessages(d.messages);
  } catch (_) { /* 服务重启间隙,继续滚动可重试 */ }
  finally {
    notice.remove();
    chanLoadingEarlier = false;
  }
}

document.getElementById("msgs").addEventListener("scroll", event => {
  if (event.target.scrollTop < 80) loadEarlierMessages();
});

/* ---- 运行过程卡片:内联在触发消息之后,可折叠,实时刷新 ---- */
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
  return `<span class="rc-dot ${live ? "live" : run.status}">●</span>
    <b style="color:${roleColor[run.role_id] || "var(--muted)"}">@${esc(run.role_id)}</b>
    <span class="muted">${run.backend_id ? esc(run.backend_id) : "…"} · ${st}${secs}</span>
    ${run.error ? `<span class="rc-err">${esc(run.error).slice(0, 120)}</span>` : ""}`;
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
    payload.pending ? `剩余：${payload.pending}` : ""].filter(Boolean).join(" · ");
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
      content = esc(JSON.stringify(payload, null, 2));
      preview = "Token 用量";
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
               latestEventId: "", openEventId: undefined };
      el.querySelector("summary").addEventListener("click", () => { card.userToggled = true; });
      el.addEventListener("toggle", () => {   // 展开时过程流贴底显示最新
        if (el.open) { const b = el.querySelector(".rc-events"); b.scrollTop = b.scrollHeight; }
      });
      while (anchor.nextElementSibling?.classList?.contains("run-card")
             && Number(anchor.nextElementSibling.dataset.runId) < run.id)
        anchor = anchor.nextElementSibling;
      anchor.after(el);
      cards.set(run.id, card);
    }
    const live = ["queued", "running", "waiting_user"].includes(run.status);
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

function updateChatRunControls(activeRuns = []) {
  const button = document.getElementById("stop-chat-btn");
  if (!button) return;
  const count = activeRuns.filter(run =>
    ["queued", "running", "waiting_user"].includes(run.status)).length;
  button.hidden = count === 0;
  button.dataset.runCount = String(count);
  button.textContent = count > 1 ? `停止全部 (${count})` : "停止 Agent";
  const archived = Boolean(projChannels().find(
    channel => channel.id === currentChan)?.archived);
  button.disabled = archived;
}

async function pollMessages() {
  if (!currentChan) return;
  const chan = currentChan;   // 响应落地时可能已切频道:丢弃过期响应
  try {
    // 首屏用 tail 直接定位频道最新一页(长历史不再从头分批追平),
    // 之后按 after_id 增量;更早历史由 loadEarlierMessages 向上翻页
    const query = lastMsgId ? `after_id=${lastMsgId}` : "tail=true";
    const r = await fetch(`/api/chat/${chan}/messages?${query}`);
    if (!r.ok || chan !== currentChan) return;
    const d = await r.json();
    if (chan !== currentChan) return;
    const channel = overview.channels.find(item => item.id === chan);
    if (channel && d.channel) {
      const lastMessageAt = channel.last_message_at;
      Object.assign(channel, d.channel);
      channel.last_message_at = Math.max(Number(lastMessageAt || 0),
                                         Number(d.channel.last_message_at || 0));
    }
    if (!firstMsgId && d.messages.length) firstMsgId = d.messages[0].id;
    if ("has_earlier" in d) chanHasEarlier = Boolean(d.has_earlier);
    appendMessages(d.messages);
    syncRuns(d.runs || []);
    updateChatRunControls(d.active_runs || []);
    if (setChannelRunningCount(chan, (d.active_runs || []).length))
      refreshChannelRunningMarkers();
    const pane = document.getElementById("msgs");
    if (!pane.children.length)
      pane.innerHTML = `<div class="chat-empty empty">还没有消息：从角色列表选择提及对象；不选择时默认交给项目主控。</div>`;
  } catch (e) { /* 服务重启间隙,忽略 */ }
}

function composerPayload() {
  const box = document.getElementById("input");
  let content = "";
  let codePoints = 0;
  const mentions = [];
  const append = value => {
    content += value;
    codePoints += Array.from(value).length;
  };
  const visit = node => {
    if (node.nodeType === Node.TEXT_NODE) { append(node.nodeValue || ""); return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.classList.contains("mention-compose") && node.dataset.roleId) {
      const token = `@${node.dataset.roleId}`;
      const start = codePoints;
      append(token);
      mentions.push({ role_id: node.dataset.roleId, start, end: codePoints });
      return;
    }
    if (node.tagName === "BR") { append("\n"); return; }
    const block = node !== box && (node.tagName === "DIV" || node.tagName === "P");
    if (block && content && !content.endsWith("\n")) append("\n");
    node.childNodes.forEach(visit);
    if (block && content && !content.endsWith("\n")) append("\n");
  };
  box.childNodes.forEach(visit);
  if (!content.trim()) return { content: "", mentions: [] };

  const leading = content.match(/^\s*/u)?.[0] || "";
  const trailing = content.match(/\s*$/u)?.[0] || "";
  const shift = Array.from(leading).length;
  const keptEnd = codePoints - Array.from(trailing).length;
  return {
    content: content.trim(),
    mentions: mentions
      .filter(item => item.start >= shift && item.end <= keptEnd)
      .map(item => ({ ...item, start: item.start - shift, end: item.end - shift })),
  };
}

function restoreComposerPayload(content, mentionSpans = []) {
  const box = document.getElementById("input");
  const chars = Array.from(String(content || ""));
  const fragment = document.createDocumentFragment();
  let cursor = 0;
  for (const span of [...mentionSpans].sort((a, b) => a.start - b.start)) {
    const token = chars.slice(span.start, span.end).join("");
    if (span.start < cursor || token !== `@${span.role_id}`) continue;
    if (span.start > cursor)
      fragment.appendChild(document.createTextNode(chars.slice(cursor, span.start).join("")));
    const mention = createComposerMention(span.role_id);
    if (mention) fragment.appendChild(mention);
    else fragment.appendChild(document.createTextNode(token));
    cursor = span.end;
  }
  if (cursor < chars.length)
    fragment.appendChild(document.createTextNode(chars.slice(cursor).join("")));
  box.replaceChildren(fragment);
  const range = document.createRange();
  range.selectNodeContents(box);
  range.collapse(false);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  savedComposerRange = range.cloneRange();
  box.focus();
}

async function send() {
  const box = document.getElementById("input");
  const payload = composerPayload();
  const { content, mentions } = payload;
  if (!content || !currentChan) return;
  if (projChannels().find(channel => channel.id === currentChan)?.archived) {
    toast("频道已归档，请先恢复后再发送消息", "error");
    return;
  }
  box.replaceChildren();
  savedComposerRange = null;
  hideMentionPicker();
  try {
    await api("POST", `/api/chat/${currentChan}/messages`,
              { author: "human", content, mentions });
  } catch (e) {
    restoreComposerPayload(content, mentions);  // 发送失败时还原结构化提及，不降级成文本
    return;
  }
  await pollMessages();
}

async function clearChatContext() {
  if (!currentChan) return;
  if (!await uiConfirm(
      "清除后，历史消息仍会保留在聊天记录中，但后续 Agent 不再复用之前的 Runtime 会话，也不会自动带入分隔线之前的最近对话。",
      "清除频道上下文")) return;
  const result = await api("POST", `/api/chat/${currentChan}/clear-context`);
  await pollMessages();
  toast(`上下文已清除${result.stopped_runtimes ? `，已停止 ${result.stopped_runtimes} 个持久实例` : ""}`,
        "success");
}

async function stopChannelAgents() {
  if (!currentChan) return;
  const button = document.getElementById("stop-chat-btn");
  const count = Number(button?.dataset.runCount || 0);
  if (!count) return;
  if (!await uiConfirm(
      `停止当前频道中正在排队、运行或等待交互的 ${count} 个 Agent，并终止对应 Runtime 进程？原生会话 ID 会保留，已完成的文件修改不会自动回滚。`,
      "停止频道 Agent")) return;
  button.disabled = true;
  try {
    const result = await api("POST", `/api/chat/${currentChan}/stop`);
    await pollMessages();
    if (!result.stopped_runs && !result.stopped_runtimes) {
      toast("当前频道已经没有运行中的 Agent", "success");
      return;
    }
    const agentText = result.stopped_runs
      ? `已停止 ${result.stopped_runs} 个 Agent 运行` : "没有活动 Agent 运行";
    const runtimeText = result.stopped_runtimes
      ? `；已终止 ${result.stopped_runtimes} 个 Runtime 进程` : "";
    toast(`${agentText}${runtimeText}`,
      result.runtime_errors ? "error" : "success", 6000);
  } finally {
    if (!button.hidden) button.disabled = false;
  }
}

const inputBox = document.getElementById("input");
inputBox.addEventListener("keydown", e => {
  if (imeComposing(e)) return;
  const pickerOpen = !document.getElementById("mention-picker").hidden;
  if (pickerOpen && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
    e.preventDefault(); moveMentionPicker(e.key === "ArrowDown" ? 1 : -1); return;
  }
  if (pickerOpen && e.key === "Escape") { e.preventDefault(); hideMentionPicker(); return; }
  if (pickerOpen && e.key === "Enter" && !e.shiftKey && mentionPickerRoles.length) {
    e.preventDefault(); chooseComposerMention(mentionPickerRoles[mentionPickerIndex].id); return;
  }
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); return; }
  if (e.key === "Enter" && e.shiftKey) {
    e.preventDefault(); document.execCommand("insertText", false, "\n");
  }
});
inputBox.addEventListener("input", () => {
  if (!inputBox.textContent && !inputBox.querySelector(".mention-compose"))
    inputBox.replaceChildren();
  rememberComposerSelection();
  updateMentionPicker();
});
inputBox.addEventListener("keyup", rememberComposerSelection);
inputBox.addEventListener("mouseup", rememberComposerSelection);
inputBox.addEventListener("paste", event => {
  event.preventDefault();
  document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
});
document.addEventListener("selectionchange", rememberComposerSelection);
document.addEventListener("mousedown", event => {
  if (!event.target.closest("#input-wrap") && !event.target.closest("#role-bar")
      && !event.target.closest("#role-list")) hideMentionPicker();
});
document.getElementById("board-request").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !imeComposing(e)) { e.preventDefault(); requestBoard(); }
});
