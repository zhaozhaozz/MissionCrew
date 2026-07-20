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

function openDirPicker(startPath) {
  _pickHidden = false;
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
  const input = document.getElementById("res-target");
  if (input && window._pickPath) input.value = window._pickPath;
  ddlg.close();
}

function openGuidelineFromSidebar(id) {
  switchTab("proj");
  setTimeout(() => editGuideline(id), 300);
}

function openSkillFromSidebar(id) {
  switchTab("proj");
  setTimeout(() => editSkill(id), 300);
}

function quickNewGuideline() {
  switchTab("proj");
  setTimeout(() => editGuideline(null), 300);
}

function quickNewSkill() {
  switchTab("proj");
  setTimeout(() => editSkill(null), 300);
}

function openBoardFromSidebar(id) {
  currentCustomBoard = id;
  customBoardEditing = false;
  boardEditorVisible = false;
  switchTab("custom");
}

function openDocFromSidebar(path) {
  docSelected = path;
  docMode = "view";
  docViewingRevision = null;
  docHistoryOpen = false;
  if (currentTab === "docs") { renderDocTree(); renderDocPane(); renderSidebar(); }
  else switchTab("docs");
}

function insertMention(id) {
  if (currentTab !== "chat") switchTab("chat");
  const box = document.getElementById("input");
  box.value = (box.value ? box.value.replace(/\s*$/, " ") : "") + `@${id} `;
  box.focus();
}

function selectChannel(id, jump = true) {
  currentChan = id; lastMsgId = 0; lastMsgDate = "";
  runCards.clear();
  document.getElementById("msgs").innerHTML = "";
  if (jump && currentTab !== "chat") switchTab("chat");
  renderSidebar(); pollMessages();
  syncUrl();
}

function fmtBody(text, markdown = false) {
  // Agent 回复按轻量 Markdown 渲染(标题/粗体/行内代码/列表),人类与平台消息保持纯文本;
  // miniMarkdown 生成的标签无属性,@ 高亮的正则替换不会破坏标签结构
  let html = markdown ? miniMarkdown(text) : esc(text);
  html = html.replace(/@([\w-]+)/g, (m, id) =>
    roleColor[id] ? `<span class="mention" style="color:${roleColor[id]}">@${id}</span>` : m);
  return html;
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

function appendMessages(list) {
  const pane = document.getElementById("msgs");
  if (list.length) pane.querySelector(".chat-empty")?.remove();
  const nearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120;
  for (const m of list) {
    const d = new Date(m.created_at * 1000);
    const day = d.toLocaleDateString();
    if (day !== lastMsgDate) {
      lastMsgDate = day;
      const sep = document.createElement("div");
      sep.className = "date-sep";
      sep.innerHTML = `<span>${esc(day)}</span>`;
      pane.appendChild(sep);
    }
    const isAgent = m.author_type === "agent";
    const isHuman = m.author_type === "human";
    const color = isAgent ? (roleColor[m.author] || "#888")
                : isHuman ? "var(--accent)" : "var(--muted)";
    const name = isAgent ? "@" + m.author : m.author_type === "platform" ? "系统" : m.author;
    const initial = isAgent || isHuman ? (m.author[0] || "?").toUpperCase() : "⚙";
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const longReply = isAgent && m.content.length > MESSAGE_FOLD_AT;
    const div = document.createElement("div");
    div.className = `msg ${m.author_type}`;
    div.dataset.msgId = m.id;   // 运行过程卡片按触发消息内联定位
    div.innerHTML = `<span class="avatar" style="background:${color}">${esc(initial)}</span>
      <div class="msg-main">
        <div class="head"><span class="author" style="color:${isAgent ? color : "var(--text)"}">${esc(name)}</span>
          ${isAgent ? `<span class="via">${esc(agentExecutionLabel(m))}</span>` : ""}<span class="time">${time}</span></div>
        <div class="body${longReply ? " folded" : ""}">${fmtBody(m.content, isAgent)}</div>
        ${longReply ? `<button type="button" class="message-fold-toggle" data-size="${m.content.length}"
          aria-expanded="false" onclick="toggleMessageBody(this)">展开完整回复（${m.content.length} 字符）</button>` : ""}
      </div>`;
    pane.appendChild(div);
    lastMsgId = Math.max(lastMsgId, m.id);
  }
  if (list.length && nearBottom) pane.scrollTop = pane.scrollHeight;
}

/* ---- 运行过程卡片:内联在触发消息之后,可折叠,实时刷新 ---- */
const runCards = new Map();   // run_id -> {el, key, userToggled}
const RUN_EVENT_META = {
  command:     { label: "命令", cls: "re-command" },
  input:       { label: "输入", cls: "re-input" },
  thinking:    { label: "思考", cls: "re-thinking" },
  tool:        { label: "工具", cls: "re-tool" },
  tool_result: { label: "结果", cls: "re-tool" },
  text:        { label: "输出", cls: "re-text" },
  stdout:      { label: "输出", cls: "re-tool" },
  // stderr 是多数 Agent CLI 的进度/日志通道(codex 连思考都走这里),
  // 不是错误:中性展示,失败与否由卡片头部的状态与错误摘要表达
  stderr:      { label: "日志", cls: "re-log" },
  status:      { label: "状态", cls: "re-status" },
};
const RUN_INPUT_FOLD_AT = 2000;

function mergeRunInputEvents(events) {
  // 存储层会把超 8000 字符的输入分段；展示时重新合并，避免一个 prompt
  // 出现多个「输入」块，也让折叠/展开控制的是完整原文。
  const merged = [];
  for (const event of events) {
    const previous = merged[merged.length - 1];
    if (event.kind === "input" && previous?.kind === "input") {
      previous.content += event.content;
    } else {
      merged.push({ ...event });
    }
  }
  return merged;
}

function runSummary(run) {
  const st = { queued: "排队中", running: "运行中", done: "已完成", failed: "失败" }[run.status] || run.status;
  const live = run.status === "queued" || run.status === "running";
  const secs = run.finished_at ? ` · ${Math.max(1, Math.round(run.finished_at - run.created_at))}s` : "";
  return `<span class="rc-dot ${live ? "live" : run.status}">●</span>
    <b style="color:${roleColor[run.role_id] || "var(--muted)"}">@${esc(run.role_id)}</b>
    <span class="muted">${run.backend_id ? esc(run.backend_id) : "…"} · ${st}${secs}</span>
    ${run.error ? `<span class="rc-err">${esc(run.error).slice(0, 120)}</span>` : ""}`;
}

async function renderRunEvents(run, card) {
  // 静默拉取(不弹 toast,服务重启间隙下轮重试);成功才返回 true,
  // 调用方据此提交 card.key,失败时下轮按 key 未变化重试
  try {
    const r = await fetch(`/api/chat/runs/${run.id}/events`);
    if (!r.ok) return false;
    const d = await r.json();
    const pane = document.getElementById("msgs");
    const outerNear = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120;
    const body = card.el.querySelector(".rc-events");
    const innerNear = !body.childElementCount ||
      body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    const openInputs = new Set([...body.querySelectorAll(".re-fold[open]")]
      .map(el => el.dataset.eventId));
    body.innerHTML = mergeRunInputEvents(d.events).map(e => {
      const meta = RUN_EVENT_META[e.kind] || { label: e.kind, cls: "re-status" };
      if (e.kind === "input" && e.content.length > RUN_INPUT_FOLD_AT) {
        const open = openInputs.has(String(e.id)) ? " open" : "";
        return `<details class="re re-fold ${meta.cls}" data-event-id="${e.id}"${open}>
          <summary><span class="re-k">${esc(meta.label)}</span>
            <span class="re-fold-size">${e.content.length.toLocaleString()} 字符 · 完整原文</span></summary>
          <div class="re-content">${esc(e.content)}</div></details>`;
      }
      return `<div class="re ${meta.cls}"><span class="re-k">${esc(meta.label)}</span>${esc(e.content)}</div>`;
    }).join("") || `<div class="re re-status">(暂无过程输出)</div>`;
    // 内外滚动都只在原本贴底时跟随,不打断正在回看历史的读者
    if (innerNear) body.scrollTop = body.scrollHeight;
    if (outerNear) pane.scrollTop = pane.scrollHeight;
    return true;
  } catch (_) { return false; }
}

function syncRuns(runs) {
  const pane = document.getElementById("msgs");
  const nearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 120;
  for (const run of runs) {
    let card = runCards.get(run.id);
    if (!card) {
      // 内联定位:触发消息之后、同触发的更早卡片之后。触发消息还没
      // 分页加载进来时先不建卡,下轮消息就位后再挂,避免卡片错位搁浅
      let anchor = pane.querySelector(`[data-msg-id="${run.trigger_message_id}"]`);
      if (!anchor && run.trigger_message_id > lastMsgId) continue;
      const el = document.createElement("details");
      el.className = "run-card";
      el.dataset.trigger = run.trigger_message_id;
      el.dataset.runId = run.id;
      el.innerHTML = `<summary></summary><div class="rc-events"></div>`;
      card = { el, key: null, userToggled: false, fetching: false };
      el.querySelector("summary").addEventListener("click", () => { card.userToggled = true; });
      el.addEventListener("toggle", () => {   // 展开时过程流贴底显示最新
        if (el.open) { const b = el.querySelector(".rc-events"); b.scrollTop = b.scrollHeight; }
      });
      while (anchor && anchor.nextElementSibling?.classList?.contains("run-card")
             && Number(anchor.nextElementSibling.dataset.runId) < run.id)
        anchor = anchor.nextElementSibling;
      if (anchor) anchor.after(el); else pane.appendChild(el);
      runCards.set(run.id, card);
    }
    const live = run.status === "queued" || run.status === "running";
    const key = `${run.status}:${run.events_size}`;
    if (card.key !== key && !card.fetching) {
      card.el.querySelector("summary").innerHTML = runSummary(run);
      if (!card.userToggled) card.el.open = live;   // 运行中自动展开,结束自动收起
      if (run.events_size > 0 || !live) {
        card.fetching = true;
        renderRunEvents(run, card).then(ok => {
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

async function pollMessages() {
  if (!currentChan) return;
  const chan = currentChan;   // 响应落地时可能已切频道:丢弃过期响应
  try {
    const r = await fetch(`/api/chat/${chan}/messages?after_id=${lastMsgId}`);
    if (!r.ok || chan !== currentChan) return;
    const d = await r.json();
    if (chan !== currentChan) return;
    appendMessages(d.messages);
    syncRuns(d.runs || []);
    const pane = document.getElementById("msgs");
    if (!pane.children.length)
      pane.innerHTML = `<div class="chat-empty empty">还没有消息:@角色 布置工作,对话会显示在这里</div>`;
  } catch (e) { /* 服务重启间隙,忽略 */ }
}

async function send() {
  const box = document.getElementById("input");
  const content = box.value.trim();
  if (!content || !currentChan) return;
  box.value = "";
  box.style.height = "";   // 复位自动增高
  try {
    await api("POST", `/api/chat/${currentChan}/messages`, { author: "human", content });
  } catch (e) {
    box.value = content;   // 发送失败(频道被删/服务重启):还回输入,不丢内容
    return;
  }
  await pollMessages();
}

const inputBox = document.getElementById("input");
inputBox.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !imeComposing(e)) { e.preventDefault(); send(); }
});
// 输入框随内容自动增高(上限 160px),发送后复位
inputBox.addEventListener("input", () => {
  inputBox.style.height = "auto";
  inputBox.style.height = Math.min(inputBox.scrollHeight, 160) + "px";
});
document.getElementById("board-request").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !imeComposing(e)) { e.preventDefault(); requestBoard(); }
});
