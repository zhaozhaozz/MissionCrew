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

function roleDisabledReason(role) {
  if (!role?.usage_auto_disabled) return "角色已停用，需在项目设置中重新启用";
  const reset = Number(role.usage_disabled_until || 0);
  const suffix = reset ? `，预计 ${new Date(reset * 1000).toLocaleString()} 自动恢复`
    : "，将在下次检测到可用额度后自动恢复";
  return `账户用量已耗尽，角色由用量联动自动停用${suffix}`;
}

function roleUsageLinkageField(role) {
  const enabled = role?.usage_linkage_enabled === true;
  return `<label class="role-usage-linkage-field"
    onclick="const control=this.querySelector('#rf-usage-linkage');control.classList.toggle('on');control.setAttribute('aria-checked',String(control.classList.contains('on')))">
    <span><b>账户用量联动</b><small>仅当该角色直接消耗所选 Runtime 的账户限额时开启；使用第三方 LLM API 时保持关闭。</small></span>
    <span class="switch ${enabled ? "on" : ""}" id="rf-usage-linkage" role="switch"
      aria-checked="${enabled}"></span>
  </label>`;
}

/* ---- 角色标识色:原生取色器 + 预设色板(与内置角色模板同色系) ---- */
const ROLE_COLOR_PRESETS = ["#d97706", "#3564d7", "#2e9e5b", "#8b5cf6",
                            "#c98a1b", "#c94b3c", "#0e9488", "#64748b"];

function normalizeHexColor(value, fallback = "#3564d7") {
  const v = String(value || "").trim();
  if (/^#[0-9a-fA-F]{6}$/.test(v)) return v.toLowerCase();
  if (/^#[0-9a-fA-F]{3}$/.test(v))   // #abc -> #aabbcc(原生取色器只认 6 位)
    return ("#" + [...v.slice(1)].map(c => c + c).join("")).toLowerCase();
  return fallback;
}

function colorFieldHtml(id, value) {
  const swatches = ROLE_COLOR_PRESETS.map(c =>
    `<span class="swatch" style="background:${c}" title="${c}"
       onclick="document.getElementById('${id}').value='${c}'"></span>`).join("");
  return `<span class="color-field">
    <input type="color" id="${id}" value="${normalizeHexColor(value)}">${swatches}</span>`;
}

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
  const box = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  el.addEventListener("animationend", () => el.classList.add("shown"), { once: true });
  const dismiss = () => {
    el.classList.add("out");
    setTimeout(() => { el.remove(); if (!box.children.length) lowerToasts(box); }, 190);
  };
  el.onclick = dismiss;
  box.appendChild(el);
  raiseToasts(box);
  setTimeout(dismiss, ms);
}
// 提示容器是 manual popover。showModal() 的弹窗在顶层(top layer),普通 z-index 压不过它,
// 同层内后 show 的在上面:每次新增提示都重新 show 一次,保证盖过此刻已打开的弹窗及其模糊
// 遮罩(表单校验失败的报错正是在弹窗仍开着时出现);没有提示时收起,不长期占着顶层。
// 已知限制:模态弹窗打开期间,弹窗子树之外的节点都是 inert,提示只能看、点不掉,靠超时自动
// 消失;弹窗关掉后恢复可点击。不支持 popover 的浏览器退回普通 fixed 定位
function raiseToasts(box) {
  if (typeof box.showPopover !== "function") return;
  if (box.matches(":popover-open")) box.hidePopover();
  box.showPopover();
}
function lowerToasts(box) {
  if (typeof box.hidePopover === "function" && box.matches(":popover-open")) box.hidePopover();
}

// 后台刷新可能替换滚动容器本身；按选择器记录位置，重建后恢复到新节点。
function captureScrollPositions(selectors) {
  return selectors.map(selector => {
    const element = document.querySelector(selector);
    return element ? { selector, top: element.scrollTop, left: element.scrollLeft } : null;
  }).filter(Boolean);
}

function restoreScrollPositions(positions) {
  for (const position of positions || []) {
    const element = document.querySelector(position.selector);
    if (!element) continue;
    element.scrollTop = position.top;
    element.scrollLeft = position.left;
  }
}

// 把 element 滚进 container 视野:已完整可见则不动,否则滚到容器中部(带出前后相邻条目)。
// 只改容器自身的 scrollTop,不像 scrollIntoView 那样连带滚动页面与其他祖先;
// element 没有布局(所在区域 display:none)时返回 false,由调用方稍后重试
function revealWithinScrollBox(container, element) {
  if (!container || !element || !element.getClientRects().length) return false;
  const box = container.getBoundingClientRect();
  const rect = element.getBoundingClientRect();
  if (rect.top >= box.top && rect.bottom <= box.bottom) return true;
  container.scrollTop += rect.top - box.top - (box.height - rect.height) / 2;
  return true;
}

function captureKeyedScrollPositions(root) {
  if (!root) return [];
  return [...root.querySelectorAll("[data-scroll-key]")].map(element => ({
    key: element.dataset.scrollKey,
    top: element.scrollTop,
    left: element.scrollLeft,
  }));
}

function restoreKeyedScrollPositions(root, positions) {
  if (!root) return;
  const elements = [...root.querySelectorAll("[data-scroll-key]")];
  for (const position of positions || []) {
    const element = elements.find(candidate => candidate.dataset.scrollKey === position.key);
    if (!element) continue;
    element.scrollTop = position.top;
    element.scrollLeft = position.left;
  }
}

/* 看板滚轮:任务看板(内置与自定义共用 .taskboard-grid)横向排列,
   纵向滚轮落在列标题、列空白处或装得下的列上时转为横向滚动看板;只有指针
   落在纵向溢出的列里,才保留浏览器默认行为纵向滚动该列。两个看板都会整体
   重绘 DOM,所以在 document 上委托一次,不随渲染重复绑定。 */
const BOARD_GRID_SELECTOR = ".taskboard-grid";
const WHEEL_LINE_PX = 40;   // deltaMode=1(Firefox 按行计)时每行折算的像素

document.addEventListener("wheel", event => {
  if (event.ctrlKey || event.shiftKey) return;   // 缩放与浏览器原生横滚不干预
  const grid = event.target.closest?.(BOARD_GRID_SELECTOR);
  if (!grid || grid.scrollWidth <= grid.clientWidth) return;
  if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;   // 触控板横向手势走原生
  const list = event.target.closest(".col-list");
  if (list && list.scrollHeight > list.clientHeight + 1) return;   // 溢出的列:纵向滚该列
  event.preventDefault();
  const unit = event.deltaMode === 1 ? WHEEL_LINE_PX
    : event.deltaMode === 2 ? grid.clientWidth : 1;
  grid.scrollLeft += event.deltaY * unit;
}, { passive: false });

function isNearScrollBottom(element, threshold = 40) {
  return !element || element.scrollHeight - element.scrollTop - element.clientHeight < threshold;
}

let overview = {
  projects: [], tasks: [], roles: [], role_templates: [],
  channels: [], backends: [], boards: [], automations: [],
};
let currentProject = localStorage.getItem("mc.project") || null;
let currentTab = "chat";
let currentChan = null;
let lastMsgId = 0;
let firstMsgId = 0;          // 已加载的最早消息 id,作为向上翻页游标
let chanHasEarlier = false;  // 当前频道是否还有更早历史未加载
let chanLoadingEarlier = false;
let roleColor = {};
let routeRestored = false;   // 首次加载按 URL 还原视图后才允许写 hash
const CHANNEL_FILTERS = new Set(["all", "active", "archived"]);
let channelFilter = localStorage.getItem("mc.channelFilter") || "active";
if (!CHANNEL_FILTERS.has(channelFilter)) channelFilter = "active";
// 频道/任务列表按当前项目 + 筛选状态向服务端按需拉取;
// counts 是服务端全量口径的计数(供筛选菜单),scopeKey 防重复拉取与竞态
let channelCounts = null;
let taskCounts = null;
let tasksScopeKey = null;

// 项目是第一层级:聊天、看板、角色、频道都只看当前项目
const projRoles = () => overview.roles.filter(r => r.project_id === currentProject);
const activeProjRoles = () => projRoles().filter(role => role.enabled !== false);
const globalRoleTemplates = () => overview.role_templates || [];
const channelIsGeneral = channel => channel.id === "general" || channel.id.endsWith(":general");
const channelIsContent = channel => Boolean(channel.content_kind && channel.content_key);
const channelActivity = channel => Number(channel.last_message_at || channel.created_at || 0);
const projChannels = () => overview.channels
  .filter(channel => channel.project_id === currentProject)
  .sort((left, right) => {
    const generalOrder = Number(channelIsGeneral(right)) - Number(channelIsGeneral(left));
    return generalOrder || channelActivity(right) - channelActivity(left)
      || String(left.id).localeCompare(String(right.id));
  });
const visibleProjChannels = () => projChannels().filter(channel =>
  channelFilter === "all" || (channelFilter === "archived" ? channel.archived : !channel.archived));
const taskActivity = task => Number(task.updated_at || task.created_at || 0);
const projTasks = () => overview.tasks
  .filter(task => task.project_id === currentProject)
  .sort((left, right) => taskActivity(right) - taskActivity(left)
    || String(left.id).localeCompare(String(right.id)));
const projBoards = () => (overview.boards || []).filter(b => b.project_id === currentProject);
// 任务看板是平台内置面板：参与面板导航，但不进入自定义 Board 的 CRUD。
const BUILTIN_TASK_PANEL = Object.freeze({
  id: "__missioncrew_tasks__", name: "任务看板", kind: "tasks", builtin: true,
  description: "平台内置 Issue 看板；处理任务会在绑定 Channel 中通知主控",
});
// board.kind 是面板形态(widgets/taskboard),侧栏点击分派用 builtin 区分
const projPanels = () => currentProject ? [BUILTIN_TASK_PANEL,
  ...projBoards().map(board => ({ ...board, builtin: false }))] : [];

/* ---- 本地代码仓（Project.repos）----
   git 管理的仓库用 git 分支图标区分，普通本地目录仍用文件夹图标。 */
const GIT_ICON_SVG = `<svg class="git-icon" viewBox="0 0 16 16" aria-hidden="true"
  fill="currentColor"><g fill="none" stroke="currentColor" stroke-width="1.5"
  stroke-linecap="round"><path d="M4.5 4.2v7.6"/><path d="M4.5 8h4.5a2 2 0 0 0 2-2V5"/></g>
  <circle cx="4.5" cy="2.6" r="1.7"/><circle cx="4.5" cy="13.4" r="1.7"/>
  <circle cx="11" cy="3.4" r="1.7"/></svg>`;
const repoIsGit = repo => repo?.kind === "git";
const repoIcon = repo => repoIsGit(repo) ? GIT_ICON_SVG : "📁";
const repoKindLabel = repo => repoIsGit(repo) ? "git 仓" : "本地目录";
