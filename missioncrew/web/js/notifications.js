/* ---- 浏览器通知:一条消息引发的 Agent 协作链整条结束(含失败)时提醒 ----
   偏好是本浏览器的个人设置(系统通知权限本来就按浏览器授予):全局总开关 + 项目级
   关闭名单,项目默认跟随总开关。开启后轮询 /api/chat/runs/completions 的增量:
   页面在前台时页内提示;在后台时发系统通知,系统通知不可用(如局域网 http 不是
   安全上下文)时先在标签标题上计数,回到页面再补页内提示。
   「前台」= 标签页可见且浏览器窗口有焦点:切到别的标签页、最小化、整个浏览器被
   其他应用挡在后面都算后台。同开多个标签页时,只要有一个在前台,后台的标签页就
   不发系统通知,留给前台那个页内提示(前台标签页经共享存储里的心跳让其他页知道)。
   本文件顶层只定义函数,DOM 监听在 initNotifications() 里挂,便于无 DOM 测试纯函数。 */

const NOTIFY_PREFS_KEY = "mc.notify";
const NOTIFY_CLAIMS_KEY = "mc.notify.claims";   // 多个标签页同开时,同一条链只提醒一次
const NOTIFY_CLAIM_TTL_MS = 3600e3;
const NOTIFY_MAX_SEPARATE = 3;                 // 一次到达更多条时合并成一条汇总
const NOTIFY_FOREGROUND_KEY = "mc.notify.foreground";   // 正在前台的标签页的心跳
const NOTIFY_FOREGROUND_TTL_MS = 15e3;         // 三个轮询周期没续上,视为那个标签页已离开

/* ---- 纯函数:偏好与通知文案 ---- */
function normalizeNotifyPrefs(raw) {
  let value = raw;
  if (typeof value === "string") {
    try { value = JSON.parse(value); } catch (_) { value = null; }
  }
  const projects = {};
  const source = value && typeof value.projects === "object" && !Array.isArray(value.projects)
    ? value.projects : {};
  for (const [id, on] of Object.entries(source)) if (on === false) projects[id] = false;
  return { enabled: value?.enabled === true, projects };
}

function notifyEnabledFor(prefs, projectId) {
  return Boolean(projectId) && prefs.enabled && prefs.projects[projectId] !== false;
}

// 打开某个项目即意味着要通知:总开关关着时一并打开,其他项目仍按各自的关闭名单
function withProjectNotify(prefs, projectId, on) {
  const projects = { ...prefs.projects };
  if (on) delete projects[projectId];
  else projects[projectId] = false;
  return { enabled: on ? true : prefs.enabled, projects };
}

function completionNotice(item) {
  const failed = item.status === "failed";
  const detail = failed && item.error ? `执行出错：${item.error}`
    : item.reply ? `@${item.reply.author}：${item.reply.content}`
    : `@${(item.roles || []).join("、@")} 已结束`;
  // 项目名单独成字段:页内提示把它渲染成醒目的项目标签,系统通知把它放在标题最前
  const project = item.project_name || item.project_id || "";
  const headline = `${failed ? "任务失败" : "任务完成"} · #${item.channel_name || item.channel}`;
  return {
    project, headline,
    title: `${project} · ${headline}`,
    body: detail,
    tag: `mc-chain-${item.channel}-${item.root_id}`,
    url: item.resource_url || "",
    projectId: item.project_id || "",
    failed,
  };
}

function summaryNotice(items) {
  const failed = items.filter(item => item.status === "failed").length;
  const names = [...new Set(items.map(item =>
    `${item.project_name || item.project_id} #${item.channel_name || item.channel}`))];
  const headline = `${items.length} 个任务已结束${failed ? `（${failed} 个失败）` : ""}`;
  return {
    project: "", headline,
    title: headline,
    body: names.join("、"),
    tag: "mc-chain-summary",
    url: "",
    projectId: "",
    failed: failed > 0,
  };
}

function noticesFor(items) {
  return items.length > NOTIFY_MAX_SEPARATE ? [summaryNotice(items)] : items.map(completionNotice);
}

/* ---- 状态与持久化 ---- */
let notifyPrefs = { enabled: false, projects: {} };
let notifyCursor = null;     // 服务端时间游标;null = 下一轮只领游标不报历史
let notifyPolling = false;
let notifyUnread = 0;        // 页面不在前台期间到达的提醒数,显示在标签标题上
let notifyPending = [];      // 后台且没有系统通知可用时攒着,回到页面再页内提示
let notifyDeferred = [];     // 别的标签页在前台时留给它提示;它没认领就离开了,再由本页补发
let notifySince = null;      // 本页开始接收增量的服务端时间:此后结束的链本页一定拉得到
const notifyTabId = Math.random().toString(36).slice(2);

function loadNotifyPrefs() {
  try { notifyPrefs = normalizeNotifyPrefs(localStorage.getItem(NOTIFY_PREFS_KEY)); }
  catch (_) { notifyPrefs = normalizeNotifyPrefs(null); }
}

function saveNotifyPrefs() {
  try { localStorage.setItem(NOTIFY_PREFS_KEY, JSON.stringify(notifyPrefs)); } catch (_) {}
}

// insecure | unsupported | default | granted | denied
function notifySupport() {
  if (!window.isSecureContext) return "insecure";
  if (typeof Notification === "undefined") return "unsupported";
  return Notification.permission;
}

function notifyActive() {
  return notifyPrefs.enabled
    && (overview.projects || []).some(project => notifyEnabledFor(notifyPrefs, project.id));
}

// peek=true 只看不占:页面在后台又发不了系统通知时,别抢走前台标签页的提示
function claimNotice(key, peek = false) {
  try {
    const now = Date.now();
    const claims = JSON.parse(localStorage.getItem(NOTIFY_CLAIMS_KEY) || "{}");
    for (const [claimed, at] of Object.entries(claims))
      if (now - at > NOTIFY_CLAIM_TTL_MS) delete claims[claimed];
    if (claims[key]) return false;
    if (peek) return true;
    claims[key] = now;
    localStorage.setItem(NOTIFY_CLAIMS_KEY, JSON.stringify(claims));
  } catch (_) {}
  return true;
}

function pageInForeground() {
  return document.visibilityState === "visible" && document.hasFocus();
}

function readForeground() {
  try { return JSON.parse(localStorage.getItem(NOTIFY_FOREGROUND_KEY) || "null"); }
  catch (_) { return null; }
}

// 前台标签页续心跳;离开前台时立刻清掉自己的记录,让后台标签页接手发系统通知。
// 还没领到游标(notifySince 为空)时不登记:这之前结束的链本页拉不到,不能替别的页兜底
function markForeground() {
  try {
    if (pageInForeground() && notifySince !== null)
      localStorage.setItem(NOTIFY_FOREGROUND_KEY, JSON.stringify(
        { id: notifyTabId, at: Date.now(), since: notifySince }));
    else if (readForeground()?.id === notifyTabId)
      localStorage.removeItem(NOTIFY_FOREGROUND_KEY);
  } catch (_) {}
}

// 另一个标签页正在前台时,返回它开始接收增量的服务端时间;没有则返回 null
function foregroundElsewhereSince() {
  const mark = readForeground();
  if (!mark || mark.id === notifyTabId || typeof mark.since !== "number") return null;
  return Date.now() - mark.at < NOTIFY_FOREGROUND_TTL_MS ? mark.since : null;
}

function notifyTitlePrefix() {
  return notifyUnread ? `(${notifyUnread}) ` : "";
}

/* ---- 轮询与投递 ---- */
async function pollRunCompletions() {
  if (!notifyActive()) { notifyCursor = notifySince = null; markForeground(); return; }
  if (notifyPolling) return;
  notifyPolling = true;
  markForeground();
  try {
    const query = notifyCursor === null ? "" : `?since=${encodeURIComponent(notifyCursor)}`;
    const r = await fetch(`/api/chat/runs/completions${query}`);
    if (!r.ok) return;
    const data = await r.json();
    const first = notifyCursor === null;
    notifyCursor = data.now;
    if (first) { notifySince = data.now; markForeground(); }
    else deliverCompletions(data.completions || []);
  } catch (_) {
    // 服务重启间隙静默跳过,游标不动,恢复后补报
  } finally {
    notifyPolling = false;
  }
}

function deliverCompletions(items) {
  const focused = pageInForeground();
  const system = !focused && notifySupport() === "granted";
  // 另一个标签页正在前台:用户在那里看得到页内提示(或正盯着那个频道),本页不发系统通知
  const elsewhere = focused ? null : foregroundElsewhereSince();
  const fresh = notifyDeferred.splice(0).concat(items).filter(item => {
    // 自己点的停止不用再提醒
    if (item.status === "stopped" || !notifyEnabledFor(notifyPrefs, item.project_id)) return false;
    const key = `${item.channel}#${item.root_id}#${item.finished_at}`;
    if (focused && currentTab === "chat" && currentChan === item.channel) {
      claimNotice(key);   // 正盯着这个频道,回复已经在眼前:认领掉,别的标签页也不用再提醒
      return false;
    }
    if (elsewhere !== null && item.finished_at > elsewhere) {
      // 留给前台标签页;下一轮再看:它认领了就丢弃,它已离开前台就由本页补发
      if (claimNotice(key, true)) notifyDeferred.push(item);
      return false;
    }
    return claimNotice(key, !focused && !system);
  });
  if (!fresh.length) return;
  if (focused) { toastNotices(noticesFor(fresh)); return; }
  notifyUnread += fresh.length;
  updateDocumentTitle();
  if (system) noticesFor(fresh).forEach(showSystemNotice);
  else notifyPending.push(...fresh);
}

function toastNotices(notices) {
  for (const notice of notices)
    toast(`${notice.headline}\n${notice.body}`, notice.failed ? "error" : "success", 8000,
      notice.url ? () => openNoticeTarget(notice.url, notice.projectId) : null, notice.project);
}

function showSystemNotice(notice) {
  try {
    const n = new Notification(notice.title, { body: notice.body, tag: notice.tag, lang: "zh-CN" });
    n.onclick = () => {
      n.close();
      // 新标签页自己会到前台;只有留在本页(原地跳转或无目标)才需要把本页拉回来
      if (!notice.url || !openNoticeTarget(notice.url, notice.projectId)) window.focus();
    };
  } catch (_) {
    // 移动端 Chrome 只允许经 Service Worker 发通知,构造函数直接抛错:退回页内提示
    toastNotices([notice]);
  }
}

// 通知来自别的项目时开新标签页,不把当前项目正在看的页面切走;返回是否开了新标签页。
// 没有当前项目(还没选项目)或新标签页被浏览器拦截时退回原地跳转
function openNoticeTarget(url, projectId = "") {
  if (projectId && currentProject && projectId !== currentProject && window.open(url, "_blank"))
    return true;
  if (location.pathname !== url) history.pushState(null, "", url);
  void applyRoute();
  return false;
}

function onNotifyPageFocus() {
  markForeground();
  if (notifyUnread) { notifyUnread = 0; updateDocumentTitle(); }
  const pending = notifyPending.splice(0);
  if (pending.length) toastNotices(noticesFor(pending));
}

/* ---- 开关 ---- */
// 必须在点击等用户手势里调用,浏览器才会弹授权询问;warn=false 时不重复提示已知的不可用
async function ensureNotifyPermission(warn = true) {
  const support = notifySupport();
  if (support === "default") {
    const result = await Notification.requestPermission().catch(() => "denied");
    if (result !== "granted") toast("浏览器未授予通知权限，任务结束时只在页面内提示", "info", 5000);
  } else if (support !== "granted" && warn) {
    toast("当前浏览器无法发送系统通知，任务结束时只在页面内提示；原因见全局设置", "info", 5000);
  }
}

async function setGlobalNotify(on) {
  notifyPrefs = { ...notifyPrefs, enabled: on };
  saveNotifyPrefs();
  renderNotifyControls();
  if (on) await ensureNotifyPermission();
  renderNotifyControls();
}

async function setProjectNotify(projectId, on) {
  if (!projectId) return;
  const globalWasOff = !notifyPrefs.enabled;
  notifyPrefs = withProjectNotify(notifyPrefs, projectId, on);
  saveNotifyPrefs();
  renderNotifyControls();
  const name = overview.projects.find(project => project.id === projectId)?.name || projectId;
  toast(on ? `已开启「${name}」的任务结束通知${globalWasOff ? "（浏览器通知总开关已一并打开）" : ""}`
           : `已关闭「${name}」的任务结束通知`, "success");
  if (on) await ensureNotifyPermission(globalWasOff);
  renderNotifyControls();
}

function toggleProjectNotify() {
  void setProjectNotify(currentProject, !notifyEnabledFor(notifyPrefs, currentProject));
}

function sendTestNotice() {
  const notice = { project: "", headline: "MissionCrew 测试通知",
                   title: "MissionCrew 测试通知", body: "任务结束时会这样提醒你。",
                   tag: "mc-test", url: "", projectId: "", failed: false };
  if (notifySupport() === "granted") showSystemNotice(notice);
  else toastNotices([notice]);
}

/* ---- 渲染:侧栏铃铛、全局设置、项目设置 ---- */
const BELL_SVG = `<svg viewBox="0 0 16 16" aria-hidden="true">
  <path d="M8 2a4 4 0 0 0-4 4v2.5L2.5 11h11L12 8.5V6a4 4 0 0 0-4-4z"/><path d="M6.5 13a1.5 1.5 0 0 0 3 0"/></svg>`;
const BELL_OFF_SVG = BELL_SVG.replace("</svg>", `<path d="M2.5 2.5l11 11"/></svg>`);

function notifySwitchHtml(on, onclick, title, hint) {
  return `<label class="role-toggle-field" onclick="${onclick}">
    <span><b>${title}</b>${hint ? `<small>${hint}</small>` : ""}</span>
    <span class="switch ${on ? "on" : ""}" role="switch" aria-checked="${on}"></span>
  </label>`;
}

function notifySupportHint() {
  switch (notifySupport()) {
    case "granted": return "已授权系统通知：页面在前台时只在页内提示；切到其他标签页或整个浏览器在后台时弹出系统通知，点击直达频道。";
    case "default": return "尚未授权系统通知：打开开关或点「授权系统通知」时浏览器会询问。";
    case "denied": return "浏览器已拒绝本站的通知权限，只在页面内提示；如需系统通知，请在地址栏的站点设置里改为允许。";
    case "unsupported": return "这个浏览器不支持系统通知，只在页面内提示。";
    default:
      return `当前地址 ${location.origin} 不是 HTTPS 或 localhost，浏览器不允许系统通知，只在页面内提示`
        + `（后台时在标签标题上计数）。可改用 localhost 访问（如 SSH / VS Code 端口转发），`
        + `或在 Chrome 的 chrome://flags/#unsafely-treat-insecure-origin-as-secure 中加入该地址。`;
  }
}

function renderNotifyButton() {
  const btn = document.getElementById("proj-notify");
  if (!btn) return;
  btn.hidden = !currentProject;
  const on = notifyEnabledFor(notifyPrefs, currentProject);
  btn.classList.toggle("notify-on", on);
  btn.setAttribute("aria-pressed", String(on));
  btn.innerHTML = on ? BELL_SVG : BELL_OFF_SVG;
  btn.title = on ? "任务结束通知：已开启（点击关闭本项目）"
    : notifyPrefs.enabled ? "任务结束通知：本项目已关闭（点击开启）"
    : "任务结束通知：浏览器通知未开启（点击为本项目开启）";
}

function renderGlobalNotifySettings() {
  const box = document.getElementById("notify-settings");
  if (!box) return;
  const support = notifySupport();
  const projects = overview.projects || [];
  const rows = projects.map(project => `<tr>
    <td><b>${esc(project.name || project.id)}</b> <span class="muted">${esc(project.id)}</span></td>
    <td><span class="switch ${notifyEnabledFor(notifyPrefs, project.id) ? "on" : ""}" role="switch"
      aria-checked="${notifyEnabledFor(notifyPrefs, project.id)}" data-id="${esc(project.id)}"
      onclick="setProjectNotify(this.dataset.id, !this.classList.contains('on'))"></span></td></tr>`).join("");
  box.innerHTML = `
    ${notifySwitchHtml(notifyPrefs.enabled, `setGlobalNotify(${!notifyPrefs.enabled})`,
      "任务结束时通知", "总开关；关闭后所有项目都不提醒")}
    <p class="muted notify-support">${esc(notifySupportHint())}</p>
    <div class="form-actions">
      ${support === "default" ? `<button class="ghost" onclick="ensureNotifyPermission().then(renderNotifyControls)">授权系统通知</button>` : ""}
      <button class="ghost" onclick="sendTestNotice()">发送测试通知</button>
    </div>
    ${projects.length ? `<table class="mgr notify-project-table">
      <tr><th>项目</th><th>通知</th></tr>${rows}</table>` : ""}`;
}

function renderProjectNotifySetting() {
  const box = document.getElementById("proj-notify-setting");
  if (!box || !currentProject) return;
  const on = notifyEnabledFor(notifyPrefs, currentProject);
  const hint = notifyPrefs.enabled
    ? "与侧栏项目名后的铃铛是同一个开关"
    : "浏览器通知总开关未开启；打开本开关会一并打开总开关";
  box.innerHTML = notifySwitchHtml(on,
    `setProjectNotify(currentProject, ${!on})`, "本项目任务结束时通知", hint);
}

function renderNotifyControls() {
  renderNotifyButton();
  if (currentTab === "settings") renderGlobalNotifySettings();
  if (currentTab === "proj") renderProjectNotifySetting();
}

function initNotifications() {
  loadNotifyPrefs();
  window.addEventListener("focus", onNotifyPageFocus);
  window.addEventListener("blur", markForeground);
  window.addEventListener("pagehide", () => {
    try {
      if (readForeground()?.id === notifyTabId) localStorage.removeItem(NOTIFY_FOREGROUND_KEY);
    } catch (_) {}
  });
  document.addEventListener("visibilitychange", () => {
    if (pageInForeground()) onNotifyPageFocus();
    else markForeground();
  });
  // 其他标签页改了偏好:同步过来,开关显示与是否轮询保持一致
  window.addEventListener("storage", event => {
    if (event.key !== NOTIFY_PREFS_KEY) return;
    loadNotifyPrefs();
    renderNotifyControls();
  });
  renderNotifyButton();
}
