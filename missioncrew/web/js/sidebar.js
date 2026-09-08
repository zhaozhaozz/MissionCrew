/* ---- 侧栏分区的快捷操作 ---- */
function quickCreateChannel() {
  openChannelDialog();
}

function closePanelActions() {
  document.querySelectorAll(".channel-actions-menu").forEach(item => { item.hidden = true; });
}

function togglePanelActions(event, button) {
  event.stopPropagation();
  const menu = button.nextElementSibling;
  const opening = menu.hidden;
  closePanelActions();
  document.getElementById("channel-filter-menu").hidden = true;
  menu.hidden = !opening;
}

function editPanelFromSidebar(id) {
  closePanelActions();
  const board = projBoards().find(item => item.id === id);
  if (!board) return;
  if (board.kind === "taskboard") { openTaskboardDialog(board.id); return; }
  currentCustomBoard = id;
  customBoardEditing = false;
  boardEditorVisible = false;
  boardEditMode = true;
  if (currentTab === "custom") {
    renderCustomBoards(true);
    renderSidebar();
    syncUrl();
  } else switchTab("custom");
}

function deletePanelFromSidebar(id) {
  closePanelActions();
  deleteCustomBoard(id);
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
  openFormDialog("添加本地代码仓", `
    <label>本地路径或 git 远程地址(本地路径若由 git 管理,自动绑定其远程仓库)</label>
    <div class="row">
      <div><input type="text" id="res-target" placeholder="~/code/myrepo 或 https://github.com/acme/x.git"></div>
      <div style="flex:0 0 90px"><button class="ghost" style="width:100%"
        onclick="openDirPicker(document.getElementById('res-target').value)">浏览…</button></div>
    </div>
    <label>名称(可选)</label><input type="text" id="res-name">`,
    `<button class="action" onclick="addResourceFromForm()">添加代码仓</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
  setTimeout(() => document.getElementById("res-target")?.focus(), 60);
}

// 本地目录选择弹窗:逐级浏览,支持显示隐藏目录;确认后回填代码仓输入框
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
    ` <span class="pill" style="color:var(--ok);border-color:var(--ok)">${GIT_ICON_SVG} git 仓</span>` +
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
  boardEditMode = false;
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
// 当前活跃的输入框实例:聊天主输入框与 Task 派发弹窗共用同一套提及选择器逻辑
let activeComposer = { boxId: "input", pickerId: "mention-picker" };

function composerBox() { return document.getElementById(activeComposer.boxId); }
function composerPicker() { return document.getElementById(activeComposer.pickerId); }

function activateComposer(boxId, pickerId) {
  if (activeComposer.boxId === boxId) return;
  hideMentionPicker();
  savedComposerRange = null;
  activeComposer = { boxId, pickerId };
}

function roleInfo(id) {
  return projRoles().find(role => role.id === id) || null;
}

function chatEmptyHint() {
  return peerModeProject()
    ? "还没有消息：本项目没有主控，发送前请 @ 至少一个角色；@ 多个角色时各自启动。"
    : "还没有消息：从角色列表选择提及对象；不选择时默认交给项目主控。";
}

function legalMentionTitle(id) {
  const role = roleInfo(id);
  if (role?.enabled === false)
    return `${roleDisabledReason(role)}：@${id}${role.name ? `（${role.name}）` : ""}，不会触发新执行`;
  return `已确认提及：单选会触发 @${id}${role?.name ? `（${role.name}）` : ""}，` +
    (peerModeProject() ? "多选各自启动" : "多选由主控协调");
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
  const box = composerBox();
  if (!box) return;
  const selection = window.getSelection();
  if (!selection?.rangeCount) return;
  const range = selection.getRangeAt(0);
  if (composerContainsNode(box, range.commonAncestorContainer))
    savedComposerRange = range.cloneRange();
}

function composerInsertionRange() {
  const box = composerBox();
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
  const box = composerBox();
  if (!box) return null;
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
  const picker = composerPicker();
  if (picker) { picker.hidden = true; picker.innerHTML = ""; }
  mentionPickerRoles = [];
  mentionPickerIndex = 0;
}

function renderMentionPicker(query) {
  const picker = composerPicker();
  if (!picker) return;
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
  composerPicker()?.querySelector("button.active")?.scrollIntoView({ block: "nearest" });
}

function insertComposerMention(id) {
  const box = composerBox();
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
  activateComposer("input", "mention-picker");
  insertComposerMention(id);
}

function selectChannel(id, jump = true) {
  currentChan = id; lastMsgId = 0; lastMsgDate = "";
  firstMsgId = 0; chanHasEarlier = false;
  runCards.clear();
  pendingUploads = [];
  renderPendingUploads();
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

/* automation 消息的作者是脚本 id 或平台内置来源;显示名尽量取脚本名称。 */
function automationAuthorLabel(author) {
  if (author === "task-rule") return "Task 自动规则";
  const automation = (overview.automations || []).find(item => item.id === author);
  return automation ? `⚙ ${automation.name || automation.id}` : author;
}

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

function agentToolReceiptMeta(message) {
  const context = message.context?.agent_tool || {};
  const legacy = String(message.content || "").match(
    /^@(\S+)\s+使用 MissionCrew Tool · `([^`]+)`：/);
  return {
    roleId: String(context.role_id || legacy?.[1] || "Agent"),
    runId: String(context.run_id ?? ""),
    action: String(context.action || legacy?.[2] || "MissionCrew Tool"),
    prefix: legacy?.[0] || "",
  };
}

function agentToolGroupsMatch(left, right) {
  return left?.classList.contains("agent-tool-group")
    && right?.classList.contains("agent-tool-group")
    && left.dataset.roleId === right.dataset.roleId
    && left.dataset.runId === right.dataset.runId;
}

// 频道消息统一显示完整日期时间,翻看历史时不必回找日期分隔线
function fmtMessageTime(date) {
  return date.toLocaleString([], {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function refreshAgentToolGroup(group) {
  const items = [...group.querySelectorAll(".agent-tool-item")];
  if (!items.length) return;
  const count = items.length;
  const first = new Date(Number(items[0].dataset.createdAt) * 1000);
  const last = new Date(Number(items[count - 1].dataset.createdAt) * 1000);
  // 区间起点带完整日期;终点同一天只补时刻,跨天才再写一遍日期
  const end = first.toDateString() === last.toDateString()
    ? last.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : fmtMessageTime(last);
  group.querySelector(".agent-tool-group-role").textContent =
    `@${group.dataset.roleId} · ${count} 次${count > 1 ? "连续" : ""}调用`;
  group.querySelector("summary > .time").textContent = count > 1
    ? `${fmtMessageTime(first)}–${end}` : fmtMessageTime(first);
}

function appendAgentToolReceipt(message, pane, date) {
  const meta = agentToolReceiptMeta(message);
  let group = pane.lastElementChild;
  const probe = document.createElement("div");
  probe.className = "agent-tool-group";
  probe.dataset.roleId = meta.roleId;
  probe.dataset.runId = meta.runId;
  if (!agentToolGroupsMatch(group, probe)) {
    group = document.createElement("div");
    group.className = "msg platform agent-tool-group";
    group.dataset.roleId = meta.roleId;
    group.dataset.runId = meta.runId;
    group.innerHTML = `<span class="avatar agent-tool-avatar">⚙</span>
      <div class="msg-main"><details class="agent-tool-details">
        <summary><span class="author">MissionCrew Tool</span>
          <span class="via agent-tool-group-role"></span><span class="time"></span></summary>
        <div class="agent-tool-items"></div>
      </details></div>`;
    // 回执分组同样带发起角色的色条,多 Agent 并行时能对上归属
    group.querySelector(".agent-tool-details").style.borderLeft =
      `3px solid ${roleColor[meta.roleId] || "#888"}`;
    pane.appendChild(group);
  }
  const item = document.createElement("div");
  item.className = "agent-tool-item";
  item.dataset.msgId = message.id;
  item.dataset.createdAt = message.created_at;
  const content = meta.prefix && message.content.startsWith(meta.prefix)
    ? message.content.slice(meta.prefix.length) : message.content;
  const time = fmtMessageTime(date);
  item.innerHTML = `<div class="agent-tool-item-head">
      <code>${esc(meta.action)}</code><span class="time">${time}</span></div>
    <div class="body markdown-body">${fmtBody(content, true, message.mention_spans)}</div>`;
  group.querySelector(".agent-tool-items").appendChild(item);
  refreshAgentToolGroup(group);
}

function mergeAdjacentAgentToolGroups(pane) {
  let previous = null;
  for (const child of [...pane.children]) {
    if (!child.classList.contains("agent-tool-group")) {
      previous = null;
      continue;
    }
    if (!agentToolGroupsMatch(previous, child)) {
      previous = child;
      continue;
    }
    const keepOpen = previous.querySelector("details").open
      || child.querySelector("details").open;
    previous.querySelector(".agent-tool-items").append(
      ...child.querySelector(".agent-tool-items").children);
    previous.querySelector("details").open = keepOpen;
    child.remove();
    refreshAgentToolGroup(previous);
  }
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
    const isAutomation = m.author_type === "automation";
    const isToolReceipt = m.author_type === "platform" && m.kind === "agent_tool";
    if (isToolReceipt) {
      appendAgentToolReceipt(m, pane, d);
      surface.lastMsgId = Math.max(surface.lastMsgId, m.id);
      continue;
    }
    const color = isAgent ? (roleColor[m.author] || "#888")
                : isHuman ? "var(--accent)"
                : isAutomation ? "#b45309" : "var(--muted)";
    const name = isAgent ? "@" + m.author
      : isAutomation ? automationAuthorLabel(m.author)
      : m.author_type === "platform" ? "系统" : m.author;
    const initial = isAgent || isHuman ? (m.author[0] || "?").toUpperCase()
      : isAutomation ? "⚡" : "⚙";
    const time = fmtMessageTime(d);
    const longReply = isAgent && m.content.length > MESSAGE_FOLD_AT;
    const renderMarkdown = isAgent || isAutomation;
    const attachments = Array.isArray(m.context?.attachments) ? m.context.attachments : [];
    const images = attachments.filter(a => a && a.is_image && a.url);
    const div = document.createElement("div");
    div.className = `msg ${m.author_type}`;
    div.dataset.msgId = m.id;   // 运行过程卡片按触发消息内联定位
    div.innerHTML = `<span class="avatar" style="background:${color}">${esc(initial)}</span>
      <div class="msg-main">
        <div class="head"><span class="author" style="color:${isAgent ? color : "var(--text)"}">${esc(name)}</span>
          ${isAgent ? `<span class="via">${esc(agentExecutionLabel(m))}</span>` : ""}<span class="time">${time}</span></div>
        <div class="body${renderMarkdown ? " markdown-body" : ""}${longReply ? " folded" : ""}"
          style="border-left:3px solid ${esc(color)}">${fmtBody(m.content, renderMarkdown, m.mention_spans)}</div>
        ${images.length ? `<div class="msg-attachments">${images.map(a =>
          `<img src="${esc(a.url)}" alt="${esc(uploadDisplayName(a.name))}" loading="lazy"
            title="点击预览" onclick="openImagePreview(this.src, this.alt)">`).join("")}</div>` : ""}
        ${longReply ? `<button type="button" class="message-fold-toggle" data-size="${m.content.length}"
          aria-expanded="false" onclick="toggleMessageBody(this)">展开完整回复（${m.content.length} 字符）</button>` : ""}
      </div>`;
    pane.appendChild(div);
    surface.lastMsgId = Math.max(surface.lastMsgId, m.id);
  }
  mergeAdjacentAgentToolGroups(pane);
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
  // 分页边界可能正好切在同一 Agent 的连续 Tool 回执中间；拼回完整分组。
  mergeAdjacentAgentToolGroups(pane);
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
      pane.innerHTML = `<div class="chat-empty empty">${chatEmptyHint()}</div>`;
  } catch (e) { /* 服务重启间隙,忽略 */ }
}

function composerPayload(box = document.getElementById("input")) {
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

function restoreComposerPayload(content, mentionSpans = [],
                                box = document.getElementById("input")) {
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

/* ---------------- 频道附件:上传后随消息告知 Agent 本地路径 ---------------- */
let pendingUploads = [];   // 已上传待随下一条消息发送的附件

// 粘贴文本超过任一阈值时不再塞进输入框,改为转成 .txt 附件
const PASTE_FILE_MIN_CHARS = 1500;
const PASTE_FILE_MIN_LINES = 30;

function isLongPaste(text) {
  return text.length > PASTE_FILE_MIN_CHARS
      || text.split("\n").length > PASTE_FILE_MIN_LINES;
}

function uploadDisplayName(name) { return String(name || "").replace(/^\d+(?:-\d+)?-/, ""); }

async function uploadChatFile(file, channelId = currentChan) {
  const filename = encodeURIComponent(file.name || "pasted-image.png");
  const r = await fetch(`/api/chat/${encodeURIComponent(channelId)}/uploads?filename=${filename}`, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    toast(err.detail || `附件上传失败 (${r.status})`, "error", 5000);
    return null;
  }
  return r.json();
}

async function addChatAttachments(files) {
  if (!currentChan || !files.length) return;
  for (const file of files) {
    const item = await uploadChatFile(file);
    if (item) pendingUploads.push(item);
  }
  renderPendingUploads();
}

function handleChatFileInput(input) {
  addChatAttachments([...input.files]);
  input.value = "";
}

/* 拖文件进输入框即作为附件;文件落在页面其他位置一律拦截,
   防止浏览器直接打开文件顶掉页面。角色表的行排序拖拽不带 Files 类型,不受影响。
   聊天主输入框和页面对话输入框都经 bindDropZone 接入,各自决定文件去向。 */
function dragHasFiles(event) {
  return [...(event.dataTransfer?.types || [])].includes("Files");
}

function bindDropZone(wrapId, onFiles) {
  const wrap = document.getElementById(wrapId);
  let depth = 0;   // dragenter/dragleave 在子元素间成对触发,计数避免高亮闪烁
  wrap.addEventListener("dragenter", event => {
    if (!dragHasFiles(event)) return;
    depth += 1;
    wrap.classList.add("drop-target");
  });
  wrap.addEventListener("dragover", event => {
    if (!dragHasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  wrap.addEventListener("dragleave", () => {
    if (depth > 0) depth -= 1;
    if (!depth) wrap.classList.remove("drop-target");
  });
  wrap.addEventListener("drop", event => {
    if (!dragHasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    wrap.classList.remove("drop-target");
    onFiles([...event.dataTransfer.files]);
  });
}

function bindGlobalDropGuard() {
  window.addEventListener("dragover", event => {
    if (dragHasFiles(event)) event.preventDefault();
  });
  window.addEventListener("drop", event => {
    if (dragHasFiles(event)) event.preventDefault();
  });
}

function removePendingUpload(index) {
  pendingUploads.splice(index, 1);
  renderPendingUploads();
}

/* 待发附件条;removeFn 是全局函数名,按下标移除。已上传项带服务端 name/path,
   尚未上传的本地文件(页面对话在发送时才上传)用 display 显示原文件名。 */
function attachmentChipsHtml(items, removeFn) {
  return items.map((a, i) => `
    <span class="attachment-chip" title="${esc(a.path || a.display || a.name)}">
      ${a.is_image && a.url ? `<img src="${esc(a.url)}" alt="">`
                            : `<span class="attachment-kind">📄</span>`}
      <span class="attachment-name">${esc(a.display ?? uploadDisplayName(a.name))}</span>
      <button type="button" class="attachment-remove" title="移除附件"
        onclick="${removeFn}(${i})">×</button>
    </span>`).join("");
}

function renderPendingUploads() {
  const wrap = document.getElementById("composer-attachments");
  if (!wrap) return;
  wrap.hidden = !pendingUploads.length;
  wrap.innerHTML = attachmentChipsHtml(pendingUploads, "removePendingUpload");
}

function attachmentBlock(attachments) {
  const lines = attachments.map(a => `- ${a.is_image ? "图片" : "文件"}: ${a.path}`);
  return "[附件] 用户上传了以下本地文件，需要时直接按路径读取：\n" + lines.join("\n");
}

function openImagePreview(url, name = "") {
  const img = document.getElementById("idlg-img");
  img.src = url;
  img.alt = name;
  document.getElementById("idlg").showModal();
}

async function send() {
  const box = document.getElementById("input");
  const payload = composerPayload();
  const { mentions } = payload;
  const attachments = pendingUploads.slice();
  let content = payload.content;
  if ((!content && !attachments.length) || !currentChan) return;
  if (projChannels().find(channel => channel.id === currentChan)?.archived) {
    toast("频道已归档，请先恢复后再发送消息", "error");
    return;
  }
  // 附件路径追加在正文尾部，不影响前面提及范围的字符偏移
  if (attachments.length)
    content = (content ? content + "\n\n" : "") + attachmentBlock(attachments);
  box.replaceChildren();
  pendingUploads = [];
  renderPendingUploads();
  savedComposerRange = null;
  hideMentionPicker();
  try {
    await api("POST", `/api/chat/${currentChan}/messages`,
              { author: "human", content, mentions,
                context: attachments.length ? { attachments } : {} });
  } catch (e) {
    pendingUploads = attachments;   // 发送失败时附件退回待发区
    renderPendingUploads();
    restoreComposerPayload(payload.content, mentions);  // 还原结构化提及，不降级成文本
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

/* 给一个 contenteditable 输入框绑定完整的提及选择器与提交行为;
   聊天主输入框、页面对话和 Task 派发弹窗都通过它接入同一套逻辑。
   onFiles(files) 接收粘贴的文件与转成附件的长文本,返回 true 表示已接收;
   不传则输入框只收纯文本。 */
function bindComposerEvents(boxId, pickerId, onSubmit, onFiles = null) {
  const box = document.getElementById(boxId);
  box.addEventListener("focus", () => activateComposer(boxId, pickerId));
  box.addEventListener("keydown", e => {
    if (imeComposing(e)) return;
    activateComposer(boxId, pickerId);
    const pickerOpen = !composerPicker().hidden;
    if (pickerOpen && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault(); moveMentionPicker(e.key === "ArrowDown" ? 1 : -1); return;
    }
    if (pickerOpen && e.key === "Escape") { e.preventDefault(); hideMentionPicker(); return; }
    if (pickerOpen && e.key === "Enter" && !e.shiftKey && mentionPickerRoles.length) {
      e.preventDefault(); chooseComposerMention(mentionPickerRoles[mentionPickerIndex].id); return;
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSubmit(); return; }
    if (e.key === "Enter" && e.shiftKey) {
      e.preventDefault(); document.execCommand("insertText", false, "\n");
    }
  });
  box.addEventListener("input", () => {
    activateComposer(boxId, pickerId);
    if (!box.textContent && !box.querySelector(".mention-compose"))
      box.replaceChildren();
    rememberComposerSelection();
    updateMentionPicker();
  });
  box.addEventListener("keyup", rememberComposerSelection);
  box.addEventListener("mouseup", rememberComposerSelection);
  box.addEventListener("mousedown", () => activateComposer(boxId, pickerId));
  box.addEventListener("paste", event => {
    event.preventDefault();
    const files = [...(event.clipboardData?.files || [])];
    if (files.length && onFiles?.(files)) return;
    const text = event.clipboardData.getData("text/plain");
    // 长文本粘贴转为 .txt 附件,输入框里已有的内容保持原样
    if (onFiles && isLongPaste(text)
        && onFiles([new File([text], "粘贴文本.txt", { type: "text/plain" })])) {
      toast("粘贴的长文本已转为附件", "success");
      return;
    }
    document.execCommand("insertText", false, text);
  });
}

// 主输入框的附件立刻上传到当前频道;没有频道时退回纯文本粘贴
function acceptChatFiles(files) {
  if (!currentChan) return false;
  addChatAttachments(files);
  return true;
}

bindComposerEvents("input", "mention-picker", () => send(), acceptChatFiles);
bindDropZone("input-wrap", files => addChatAttachments(files));
bindGlobalDropGuard();
document.addEventListener("selectionchange", rememberComposerSelection);
document.addEventListener("mousedown", event => {
  if (!event.target.closest(".composer-wrap") && !event.target.closest("#input-wrap")
      && !event.target.closest("#role-bar")
      && !event.target.closest("#role-list")) hideMentionPicker();
});
