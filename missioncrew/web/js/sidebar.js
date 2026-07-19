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
    const div = document.createElement("div");
    div.className = `msg ${m.author_type}`;
    div.innerHTML = `<span class="avatar" style="background:${color}">${esc(initial)}</span>
      <div class="msg-main">
        <div class="head"><span class="author" style="color:${isAgent ? color : "var(--text)"}">${esc(name)}</span>
          ${isAgent ? `<span class="via">agent</span>` : ""}<span class="time">${time}</span></div>
        <div class="body">${fmtBody(m.content, isAgent)}</div>
      </div>`;
    pane.appendChild(div);
    lastMsgId = Math.max(lastMsgId, m.id);
  }
  if (list.length && nearBottom) pane.scrollTop = pane.scrollHeight;
}

async function pollMessages() {
  if (!currentChan) return;
  try {
    const r = await fetch(`/api/chat/${currentChan}/messages?after_id=${lastMsgId}`);
    if (!r.ok) return;
    const d = await r.json();
    appendMessages(d.messages);
    const pane = document.getElementById("msgs");
    if (!pane.children.length)
      pane.innerHTML = `<div class="chat-empty empty">还没有消息:@角色 布置工作,对话会显示在这里</div>`;
    document.getElementById("running-bar").innerHTML = d.active_runs.map(run =>
      `<div class="running"><span class="dot">●</span> @${esc(run.role_id)} 正在工作
       ${run.backend_id ? "(后端 " + esc(run.backend_id) + ")" : "(排队中)"}…</div>`).join("");
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
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
// 输入框随内容自动增高(上限 160px),发送后复位
inputBox.addEventListener("input", () => {
  inputBox.style.height = "auto";
  inputBox.style.height = Math.min(inputBox.scrollHeight, 160) + "px";
});
document.getElementById("board-request").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); requestBoard(); }
});

