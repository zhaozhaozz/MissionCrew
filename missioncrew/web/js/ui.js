const dlg = document.getElementById("dlg");
const pdlg = document.getElementById("pdlg");
const tdlg = document.getElementById("tdlg");
const udlg = document.getElementById("udlg");
const fdlg = document.getElementById("fdlg");   // 设置页各类新增/编辑表单的弹窗容器
const ddlg = document.getElementById("ddlg");   // 本地目录选择弹窗

// 输入法(IME)组合期间按下的 Enter 是"候选上屏确认",不是提交:
// 所有 Enter 触发提交的输入框都要先经过这个守卫,面板消失后的回车才生效。
// isComposing 覆盖标准浏览器;keyCode 229 兜底部分输入法引擎。
function imeComposing(e) {
  return e.isComposing || e.keyCode === 229;
}

/* ---- 页面内弹窗控件:替代浏览器原生 alert / confirm / prompt ---- */
let _udlgResolve = null;
function _udlgClose(value) {
  const r = _udlgResolve;
  _udlgResolve = null;
  udlg.close();
  if (r) r(value);
}
udlg.addEventListener("cancel", e => { e.preventDefault(); _udlgClose(null); });
function _udlgOpen(title, bodyHtml, actionsHtml) {
  document.getElementById("udlg-title").textContent = title;
  document.getElementById("udlg-body").innerHTML = bodyHtml;
  document.getElementById("udlg-actions").innerHTML = actionsHtml;
  udlg.showModal();
}
function uiAlert(message, title = "提示") {
  return new Promise(res => {
    _udlgResolve = res;
    _udlgOpen(title, `<div style="white-space:pre-wrap">${esc(String(message))}</div>`,
      `<button class="action" onclick="_udlgClose()">知道了</button>`);
  });
}
function uiConfirm(message, title = "请确认") {
  return new Promise(res => {
    _udlgResolve = res;
    _udlgOpen(title, `<div style="white-space:pre-wrap">${esc(String(message))}</div>`,
      `<button class="action" onclick="_udlgClose(true)">确定</button>
       <button class="ghost" onclick="_udlgClose(false)">取消</button>`);
  });
}
function uiPrompt(message, { value = "", placeholder = "", title = "请输入" } = {}) {
  return new Promise(res => {
    _udlgResolve = res;
    _udlgOpen(title,
      `<div style="white-space:pre-wrap;margin-bottom:8px">${esc(String(message))}</div>
       <input type="text" id="udlg-input" value="${esc(value)}" placeholder="${esc(placeholder)}"
         onkeydown="if(event.key==='Enter'&&!imeComposing(event))_udlgClose(this.value)">`,
      `<button class="action" onclick="_udlgClose(document.getElementById('udlg-input').value)">确定</button>
       <button class="ghost" onclick="_udlgClose(null)">取消</button>`);
    setTimeout(() => document.getElementById("udlg-input")?.focus(), 60);
  });
}
function openFormDialog(title, bodyHtml, actionsHtml) {
  document.getElementById("fdlg-title").textContent = title;
  document.getElementById("fdlg-body").innerHTML = bodyHtml;
  document.getElementById("fdlg-actions").innerHTML = actionsHtml;
  fdlg.showModal();
  // 打开后聚焦第一个可输入控件,减少一次点击
  setTimeout(() => fdlg.querySelector("#fdlg-body input:not([disabled]), #fdlg-body textarea")
    ?.focus(), 60);
}
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---- 主题:明/暗切换,选择跨会话记忆;缺省跟随系统 ---- */
function isDarkTheme() {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
}
function updateThemeBtn() {
  document.getElementById("theme-btn").textContent = isDarkTheme() ? "☀️" : "🌙";
}
function toggleTheme() {
  const next = isDarkTheme() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("mc.theme", next);
  updateThemeBtn();
}
(function initTheme() {
  const t = localStorage.getItem("mc.theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  updateThemeBtn();
})();

/* ---- Toast 通知:操作结果的轻量反馈(错误仍由 api() 统一上报) ---- */
function toast(msg, type = "info", ms = 3200) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  const dismiss = () => { el.classList.add("out"); setTimeout(() => el.remove(), 190); };
  el.onclick = dismiss;
  document.getElementById("toasts").appendChild(el);
  setTimeout(dismiss, ms);
}
let overview = {
  projects: [], tasks: [], roles: [], role_templates: [],
  channels: [], backends: [], boards: [],
};
let currentProject = localStorage.getItem("mc.project") || null;
let currentTab = "chat";
let currentChan = null;
let lastMsgId = 0;
let roleColor = {};
let routeRestored = false;   // 首次加载按 URL 还原视图后才允许写 hash

// 项目是第一层级:聊天、看板、角色、频道都只看当前项目
const projRoles = () => overview.roles.filter(r => r.project_id === currentProject);
const globalRoleTemplates = () => overview.role_templates || [];
const projChannels = () => overview.channels.filter(c => c.project_id === currentProject);
const projTasks = () => overview.tasks.filter(t => t.project_id === currentProject);
const projBoards = () => (overview.boards || []).filter(b => b.project_id === currentProject);
