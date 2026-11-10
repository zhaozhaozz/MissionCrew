/* ---- 频道(当前项目) ---- */
// 频道内有排队/执行/等待用户的运行时亮起；轮询到新状态后就地更新，不必重绘侧栏。
// attrs 是 data-channel-id(单个频道)或 data-channel-group(收起的分组,汇总组内频道)
function channelMarkersHtml(attrs, runCount, callbackCount) {
  return `<span class="channel-running-marker" ${attrs}
    title="${channelRunningTitle(runCount)}" role="img"
    aria-label="${channelRunningTitle(runCount)}" ${runCount ? "" : "hidden"}><i></i></span>`
    // 后台命令(跨 turn 存活、不阻塞对话)另用沙漏标记,由 Runtime 状态轮询点亮
    + `<span class="channel-background-marker" ${attrs} role="img" hidden>⏳</span>`
    // 有角色登记了回调、正在等到点或等事件的频道,与频道标题行的「⏰ N」同一个图标
    + `<span class="channel-callback-marker" ${attrs}
    title="${channelCallbackTitle(callbackCount)}" role="img"
    aria-label="${channelCallbackTitle(callbackCount)}" ${callbackCount ? "" : "hidden"}>⏰</span>`;
}

// topic 非空表示条目在分组内:缩进并只显示 `/` 后的话题名
function channelSidebarItem(channel, topic = "") {
  const general = channelIsGeneral(channel);
  // 归档频道只读、内容专属频道由平台维护,不能编辑;general 只能编辑,不能归档或删除
  const editable = !channel.archived && !channelIsContent(channel);
  const status = channel.archived ? `<span class="channel-state">已归档</span>` : "";
  const runMarker = channelMarkersHtml(`data-channel-id="${esc(channel.id)}"`,
    channelRunningCount(channel), channelCallbackCount(channel));
  const actions = !editable && general ? "" : `<span class="channel-item-actions">
    <button class="channel-more" type="button" aria-label="频道操作" title="频道操作"
      onclick="toggleChannelActions(event,this)">•••</button>
    <span class="channel-actions-menu" hidden onclick="event.stopPropagation()">
      ${editable ? `<button type="button" data-channel-id="${esc(channel.id)}"
          onclick="openChannelEditDialog(this.dataset.channelId)">编辑频道</button>` : ""}
      ${general ? "" : channel.archived
        ? `<button type="button" data-channel-id="${esc(channel.id)}"
             onclick="restoreChannel(this.dataset.channelId)">恢复频道</button>`
        : `<button type="button" data-channel-id="${esc(channel.id)}"
             onclick="archiveChannel(this.dataset.channelId)">归档频道</button>`}
      ${general ? "" : `<button class="danger-text" type="button" data-channel-id="${esc(channel.id)}"
        onclick="deleteChannel(this.dataset.channelId)">删除频道</button>`}
    </span>
  </span>`;
  return `<div class="side-item channel-item ${topic ? "channel-subitem" : ""} ${
      channel.archived ? "archived" : ""} ${
      channel.id === currentChan && currentTab === "chat" ? "selected" : ""}"
      data-channel-id="${esc(channel.id)}" onclick="selectChannel(this.dataset.channelId)"
      title="${esc(channel.purpose || "")}">
    <span class="channel-icon" aria-hidden="true"></span>
    <span class="channel-name">${esc(topic || channel.name || channel.id)}</span>${runMarker}${status}${actions}</div>`;
}

/* ---- 频道分组 ----
   频道名里的 `/` 把频道归入侧栏分组:`backend/api` 显示在可折叠的 `backend` 分组下,
   条目只写 `api`。只按第一个 `/` 分一层,组名或话题为空时不分组;页面对话频道名里
   带文档路径,不参与分组。分组只影响侧栏展示,id、排序与频道其他行为都不变。 */
function channelGroupParts(channel) {
  const name = channel.name || "";
  const at = name.indexOf("/");
  if (at < 0 || channelIsContent(channel)) return null;
  const group = name.slice(0, at).trim(), topic = name.slice(at + 1).trim();
  return group && topic ? { group, topic } : null;
}

const channelGroupMemberIds = group => projChannels()
  .filter(channel => channelGroupParts(channel)?.group === group).map(channel => channel.id);

// 频道条目上的标记对应一个频道;收起的分组标题上的标记汇总组内全部频道
const markerChannelIds = marker => marker.dataset.channelGroup === undefined
  ? [marker.dataset.channelId] : channelGroupMemberIds(marker.dataset.channelGroup);

// 收起的分组按「项目 + 组名」存浏览器,默认展开
const collapsedChannelGroups = new Set(
  JSON.parse(localStorage.getItem("mc.channelGroupsCollapsed") || "[]"));
const channelGroupKey = group => JSON.stringify([currentProject, group]);

function setChannelGroupCollapsed(group, collapsed) {
  const key = channelGroupKey(group);
  if (collapsed === collapsedChannelGroups.has(key)) return;
  collapsed ? collapsedChannelGroups.add(key) : collapsedChannelGroups.delete(key);
  localStorage.setItem("mc.channelGroupsCollapsed", JSON.stringify([...collapsedChannelGroups]));
}

function toggleChannelGroup(group) {
  setChannelGroupCollapsed(group, !collapsedChannelGroups.has(channelGroupKey(group)));
  renderSidebar();
}

// 选中(含链接直达)收起分组里的频道时展开该组,由调用方随后渲染侧栏
function expandChannelGroupOf(channelId) {
  const channel = overview.channels.find(item => item.id === channelId);
  const parts = channel && channelGroupParts(channel);
  if (parts) setChannelGroupCollapsed(parts.group, false);
}

// 分组出现在组内最靠前成员的位置,组内沿用列表排序(general 置顶、按最近活动)
function channelSidebarHtml(channels) {
  const rows = [], groups = new Map();
  for (const channel of channels) {
    const parts = channelGroupParts(channel);
    if (!parts) { rows.push(channel); continue; }
    if (!groups.has(parts.group)) {
      const group = { name: parts.group, members: [] };
      groups.set(parts.group, group);
      rows.push(group);
    }
    groups.get(parts.group).members.push({ channel, topic: parts.topic });
  }
  return rows.map(row => row.members ? channelGroupHtml(row) : channelSidebarItem(row)).join("");
}

function channelGroupHtml(group) {
  const closed = collapsedChannelGroups.has(channelGroupKey(group.name));
  // 收起时当前频道不可见:分组标题改用强调色,运行标记也汇总到标题上
  const holdsCurrent = currentTab === "chat"
    && group.members.some(({ channel }) => channel.id === currentChan);
  const runCount = group.members.reduce(
    (sum, { channel }) => sum + channelRunningCount(channel), 0);
  const callbackCount = group.members.reduce(
    (sum, { channel }) => sum + channelCallbackCount(channel), 0);
  // 折叠三角在图标列左侧的留白里,分组图标与组外频道的 # 同列,组内频道缩进一级
  const head = `<div class="side-item channel-group ${
      closed && holdsCurrent ? "holds-current" : ""}"
      data-group="${esc(group.name)}" onclick="toggleChannelGroup(this.dataset.group)"
      role="button" aria-expanded="${!closed}" title="${closed ? "展开" : "收起"}分组">
    <span class="caret">${closed ? "▸" : "▾"}</span>
    <span class="channel-group-icon" aria-hidden="true"></span>
    <span class="channel-name">${esc(group.name)}</span>
    ${closed ? channelMarkersHtml(
        `data-channel-group="${esc(group.name)}"`, runCount, callbackCount) : ""}
    <span class="channel-group-count">${group.members.length}</span></div>`;
  return head + (closed ? "" : group.members.map(
    ({ channel, topic }) => channelSidebarItem(channel, topic)).join(""));
}

const channelRunningCount = channel => Number(channel?.active_run_count || 0);
const channelRunningTitle = count => `${count || 1} 个 Agent 正在运行`;

/* 侧栏频道的运行标记；总览 8s 一轮，当前频道与内容频道轮询会更早更新计数。 */
function refreshChannelRunningMarkers() {
  const counts = new Map(overview.channels.map(
    channel => [channel.id, channelRunningCount(channel)]));
  document.querySelectorAll(".channel-running-marker").forEach(marker => {
    const count = markerChannelIds(marker).reduce((sum, id) => sum + (counts.get(id) || 0), 0);
    marker.hidden = count === 0;
    if (count) {
      marker.title = channelRunningTitle(count);
      marker.setAttribute("aria-label", marker.title);
    }
  });
}

/* 频道消息轮询拿到的 active_runs 更新计数，返回标记是否需要刷新。 */
function setChannelRunningCount(channelId, count) {
  const channel = overview.channels.find(item => item.id === channelId);
  if (!channel || channelRunningCount(channel) === count) return false;
  channel.active_run_count = count;
  return true;
}

/* 侧栏频道的等待回调标记:计数随频道列表下发(8s 一轮),当前频道的消息轮询与页面上的
   取消操作会更早更新。 */
const channelCallbackCount = channel => Number(channel?.pending_callback_count || 0);
const channelCallbackTitle = count => `${count || 1} 个回调等待中：到点或等到事件时重新启动登记的角色`;

function refreshChannelCallbackMarkers() {
  const counts = new Map(overview.channels.map(
    channel => [channel.id, channelCallbackCount(channel)]));
  document.querySelectorAll(".channel-callback-marker").forEach(marker => {
    const count = markerChannelIds(marker).reduce((sum, id) => sum + (counts.get(id) || 0), 0);
    marker.hidden = count === 0;
    if (count) {
      marker.title = channelCallbackTitle(count);
      marker.setAttribute("aria-label", marker.title);
    }
  });
}

function setChannelCallbackCount(channelId, count) {
  const channel = overview.channels.find(item => item.id === channelId);
  if (!channel || channelCallbackCount(channel) === count) return false;
  channel.pending_callback_count = count;
  return true;
}

function renderChannelFilter() {
  // 列表只含当前筛选范围,计数用服务端的全量口径;拉取前回退本地计算
  const local = projChannels();
  const counts = channelCounts || {
    all: local.length,
    active: local.filter(channel => !channel.archived).length,
    archived: local.filter(channel => channel.archived).length,
  };
  document.getElementById("channel-filter-all-count").textContent = counts.all;
  document.getElementById("channel-filter-active-count").textContent = counts.active;
  document.getElementById("channel-filter-archived-count").textContent = counts.archived;
  document.querySelectorAll("#channel-filter-menu [data-filter]").forEach(button => {
    const selected = button.dataset.filter === channelFilter;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-checked", String(selected));
  });
  document.getElementById("channel-filter-btn").classList.toggle("active", channelFilter !== "all");
}

function toggleChannelFilter(event) {
  event.stopPropagation();
  const menu = document.getElementById("channel-filter-menu");
  menu.hidden = !menu.hidden;
  document.getElementById("channel-filter-btn").setAttribute("aria-expanded", String(!menu.hidden));
  document.querySelectorAll(".channel-actions-menu").forEach(item => { item.hidden = true; });
}

function setChannelFilter(value) {
  if (!CHANNEL_FILTERS.has(value)) return;
  channelFilter = value;
  localStorage.setItem("mc.channelFilter", value);
  document.getElementById("channel-filter-menu").hidden = true;
  document.getElementById("channel-filter-btn").setAttribute("aria-expanded", "false");
  renderSidebar();
  void refreshChannels().then(() => renderSidebar());   // 列表按新筛选重拉
}

function toggleChannelActions(event, button) {
  event.stopPropagation();
  const menu = button.nextElementSibling;
  const opening = menu.hidden;
  document.querySelectorAll(".channel-actions-menu").forEach(item => { item.hidden = true; });
  document.getElementById("channel-filter-menu").hidden = true;
  menu.hidden = !opening;
}

// 频道头元信息可收起:偏好按浏览器记住;未设置时窄屏默认收起、桌面默认展开
function channelHeadCollapsed() {
  const saved = localStorage.getItem("mc.channelHeadCollapsed");
  if (saved !== null) return saved === "1";
  return window.matchMedia("(max-width: 820px)").matches;
}

function applyChannelHeadState() {
  const head = document.getElementById("channel-head");
  const collapsed = channelHeadCollapsed();
  head.classList.toggle("collapsed", collapsed);
  head.querySelector(".channel-head-toggle")?.setAttribute("aria-expanded", String(!collapsed));
}

function toggleChannelHead() {
  localStorage.setItem("mc.channelHeadCollapsed", channelHeadCollapsed() ? "0" : "1");
  applyChannelHeadState();
}

function renderChannelState() {
  const channel = projChannels().find(item => item.id === currentChan);
  const archived = Boolean(channel?.archived);
  const head = document.getElementById("channel-head");
  head.hidden = !channel;
  if (channel) {
    const displayName = channel.name || channel.id;
    head.innerHTML = `
      <div class="channel-title-row" onclick="toggleChannelHead()" title="展开/收起频道信息">
        <h2># ${esc(displayName)}</h2>
        ${channelIsGeneral(channel) ? `<span class="badge">默认频道</span>` : ""}
        ${channelIsContent(channel) ? `<span class="badge">内容专属频道</span>` : ""}
        <span class="badge">${archived ? "已归档" : "活跃"}</span>
        <span class="channel-background-marker channel-head-marker"
          data-channel-id="${esc(channel.id)}" hidden>⏳ <b></b> 个后台命令</span>
        <button class="channel-tasks-btn channel-callbacks-btn" id="channel-callbacks-btn"
          type="button" hidden aria-haspopup="dialog"
          onclick="openChannelCallbacks(event)"></button>
        <button class="channel-tasks-btn" id="channel-tasks-btn" type="button" hidden
          aria-haspopup="dialog" aria-controls="channel-tasks-pop" aria-expanded="false"
          onclick="toggleChannelTasksPopover(event)"></button>
        <button class="channel-head-toggle" type="button" aria-controls="channel-meta">▾</button>
      </div>
      <div class="channel-meta" id="channel-meta">
        <span><b>用途</b>${esc(channel.purpose || "未说明")}</span>
        <span><b>工作目录</b><code>${esc(channel.workdir || "平台内置工作区")}</code></span>
        <span><b>创建者</b>${esc(channel.created_by_role_id ? "@" + channel.created_by_role_id : "human/platform")}</span>
      </div>`;
    applyChannelHeadState();
    refreshRuntimeIndicators();   // 头部标记刚重建,用最近一次 Runtime 快照点亮
  }
  renderChannelTasks();
  renderChannelCallbacksButton();
  const banner = document.getElementById("channel-archive-banner");
  banner.hidden = !archived;
  document.getElementById("composer").classList.toggle("channel-archived", archived);
  const input = document.getElementById("input");
  input.contentEditable = String(!archived);
  input.setAttribute("aria-disabled", String(archived));
  document.querySelector("button.send").disabled = archived;
  document.getElementById("clear-context-btn").disabled = archived;
  document.getElementById("stop-chat-btn").disabled = archived;
  document.querySelectorAll("#role-bar button").forEach(button => { button.disabled = archived; });
}

/* ---- 频道关联 Task ----
   频道标题行右侧的「Task N」按钮预览绑定当前频道的未完成 Task 数,点击弹出悬浮的竖条
   列表(状态 + 标题),默认只看未完成的,可切到全部;点条目原地弹出详情。数据由服务端
   按频道过滤后拉取,与看板的任务列表互不影响;选中频道、切到聊天页时立即拉取,之后随
   总览 8s 轮询刷新。频道没有任何关联 Task 时不显示按钮。频道头每次轮询都整块重绘,
   所以弹层放在频道头之外(fixed),按按钮位置定位。 */
const CHANNEL_TASK_SCOPES = new Set(["open", "all"]);
let channelTaskScope = localStorage.getItem("mc.channelTaskScope") || "open";
if (!CHANNEL_TASK_SCOPES.has(channelTaskScope)) channelTaskScope = "open";
let channelTasks = { channelId: null, tasks: [] };
let channelTasksSignature = "";
let channelTasksPopChannel = null;   // 弹层打开时所属的频道;null 表示关闭

async function refreshChannelTasks() {
  const project = currentProject, channelId = currentChan;
  if (!project || !channelId) return;
  const r = await fetch(`/api/projects/${encodeURIComponent(project)}/tasks` +
    `?scope=active&channel=${encodeURIComponent(channelId)}`);
  if (!r.ok) return;
  const data = await r.json();
  if (project !== currentProject || channelId !== currentChan) return;   // 已切换:丢弃
  channelTasks = { channelId, tasks: data.tasks };
  renderChannelTasks();
}

function setChannelTaskScope(value) {
  if (!CHANNEL_TASK_SCOPES.has(value)) return;
  channelTaskScope = value;
  localStorage.setItem("mc.channelTaskScope", value);
  renderChannelTasks();
}

function toggleChannelTasksPopover(event) {
  event?.stopPropagation();   // 标题行本身的点击是展开/收起频道信息
  channelTasksPopChannel = channelTasksPopChannel ? null : currentChan;
  renderChannelTasks();
}

function closeChannelTasksPopover() {
  if (!channelTasksPopChannel) return;
  channelTasksPopChannel = null;
  renderChannelTasks();
}

// 右边缘与按钮对齐、紧贴按钮下方,高度用到视口底部为止,列表在弹层内滚动
function placeChannelTasksPopover(pop, button) {
  const rect = button.getBoundingClientRect();
  pop.style.top = `${Math.round(rect.bottom + 6)}px`;
  pop.style.right = `${Math.max(8, Math.round(window.innerWidth - rect.right))}px`;
  pop.style.maxHeight = `${Math.max(160, Math.round(window.innerHeight - rect.bottom - 18))}px`;
}

function renderChannelTasks() {
  const button = document.getElementById("channel-tasks-btn");
  const pop = document.getElementById("channel-tasks-pop");
  const tasks = (channelTasks.channelId === currentChan ? channelTasks.tasks : [])
    .slice().sort((left, right) => taskActivity(right) - taskActivity(left)
      || String(left.id).localeCompare(String(right.id)));
  const open = tasks.filter(task => taskStatus(task) !== "已完成");
  // 切换频道、离开聊天页或频道已没有关联 Task 时收起弹层
  if (channelTasksPopChannel && (channelTasksPopChannel !== currentChan
      || currentTab !== "chat" || !tasks.length || !button))
    channelTasksPopChannel = null;
  const opened = Boolean(channelTasksPopChannel);
  if (button) {   // 没有选中频道时频道头不渲染,也就没有按钮
    button.hidden = !tasks.length;
    button.innerHTML = `Task <b>${open.length}</b>`;
    button.title = `${open.length} 个未完成 · 共 ${tasks.length} 个关联 Task`;
    button.classList.toggle("active", opened);
    button.setAttribute("aria-expanded", String(opened));
  }
  pop.hidden = !opened;
  if (!opened) { pop.innerHTML = ""; channelTasksSignature = ""; return; }
  placeChannelTasksPopover(pop, button);
  const shown = channelTaskScope === "open" ? open : tasks;
  // 总览轮询会反复调用:内容没变就不重建,保留列表滚动位置与焦点
  const signature = JSON.stringify([currentChan, channelTaskScope, tasks.length,
    shown.map(task => [task.id, task.title, task.summary, taskStatus(task)])]);
  if (signature === channelTasksSignature) return;
  channelTasksSignature = signature;
  const scopeButton = (value, label) => `<button type="button" role="radio"
    aria-checked="${channelTaskScope === value}" class="${channelTaskScope === value ? "selected" : ""}"
    onclick="setChannelTaskScope('${value}')">${label}</button>`;
  pop.innerHTML = `
    <div class="channel-tasks-pop-head">
      <b>频道 Task</b><span class="channel-tasks-count">${open.length} 个未完成 · 共 ${tasks.length}</span>
      <button class="channel-tasks-close" type="button" aria-label="关闭"
        onclick="closeChannelTasksPopover()">×</button>
    </div>
    <div class="channel-tasks-scope" role="radiogroup" aria-label="显示范围">
      ${scopeButton("open", "未完成")}${scopeButton("all", "全部")}
    </div>
    <ul class="channel-task-list">
      ${shown.map(task => `<li><button type="button" class="channel-task"
          data-task-id="${esc(task.id)}" onclick="openTask(this.dataset.taskId, false)"
          title="${esc(task.summary || task.title)}">
          ${statusPillHtml(taskStatus(task), task.source_id) || '<span class="pill">无状态</span>'}
          <span class="channel-task-title">${esc(task.title)}</span></button></li>`).join("")
        || '<li class="empty">没有未完成的 Task</li>'}
    </ul>`;
}

window.addEventListener("resize", () => { if (channelTasksPopChannel) renderChannelTasks(); });
document.addEventListener("keydown", event => {
  // 有模态框(如 Task 详情)时 Esc 先关模态框,弹层留着便于接着看下一个
  if (event.key === "Escape" && !document.querySelector("dialog[open]"))
    closeChannelTasksPopover();
});

/* ---- 频道回调 ----
   角色经 callback.create 登记的一次性回调(到点、某频道有新消息或某 Task 有更新时,
   回到本频道重新启动该角色)。数据随频道消息轮询下发,标题行显示「⏰ N」,点开列出
   等待中的回调,人可以逐个取消;停止频道 Agent 时平台会一并取消。
   列表里每条回调的条件拆成标签:所盯的对象(频道 / Task / Webhook 来源 / GitHub 来源)
   按类型着色,点击跳到该对象(来源打开其配置);时间标签带实时倒计时。 */
let channelCallbacks = { channelId: null, items: [] };

function updateChannelCallbacks(channelId, items) {
  channelCallbacks = { channelId, items: Array.isArray(items) ? items : [] };
  if (setChannelCallbackCount(channelId, channelCallbacks.items.length))
    refreshChannelCallbackMarkers();
  renderChannelCallbacksButton();
  if (fdlg.open && fdlg.dataset.view === "channel-callbacks") renderChannelCallbacksDialog();
}

function currentChannelCallbacks() {
  return channelCallbacks.channelId === currentChan ? channelCallbacks.items : [];
}

function renderChannelCallbacksButton() {
  const button = document.getElementById("channel-callbacks-btn");
  if (!button) return;
  const items = currentChannelCallbacks();
  button.hidden = !items.length;
  button.innerHTML = `⏰ <b>${items.length}</b>`;
  button.title = `${items.length} 个等待中的回调：到点或所盯的频道 / Task 有更新时重新启动登记的角色`;
}

let callbackRemainingTimer = null;

function openChannelCallbacks(event) {
  event?.stopPropagation();   // 标题行本身的点击是展开/收起频道信息
  fdlg.dataset.view = "channel-callbacks";
  clearInterval(callbackRemainingTimer);
  callbackRemainingTimer = setInterval(tickCallbackRemaining, 1000);
  fdlg.addEventListener("close", () => {
    delete fdlg.dataset.view;
    clearInterval(callbackRemainingTimer);
  }, { once: true });
  renderChannelCallbacksDialog(true);
  // 盯事件的回调要知道来源是 Webhook 还是 GitHub 轮询:来源列表按需取一次,到手后重画
  if (currentChannelCallbacks().some(item => item.watch?.kind === "event"))
    loadWebhookSources().then(() => {
      if (fdlg.open && fdlg.dataset.view === "channel-callbacks") renderChannelCallbacksDialog();
    }).catch(() => {});
}

// 剩余时间只留两级单位,够判断"快到了没有";到点后返回空串(等轮询线程处理)
function fmtRemaining(seconds) {
  const s = Math.floor(seconds);
  if (!(s > 0)) return "";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s % 60} 秒`;
  return `${s} 秒`;
}

function tickCallbackRemaining() {
  const now = Date.now() / 1000;
  document.querySelectorAll(".callback-remaining[data-until]").forEach(el => {
    const left = fmtRemaining(Number(el.dataset.until) - now);
    el.textContent = left ? `还剩 ${left}` : el.dataset.done;
  });
}

const callbackWatchChannel = target => projChannels().find(
  channel => channel.id === target || channel.id.endsWith(`:${target}`));

// 所盯对象的标签:类型决定配色与点击去向
function callbackWatchHtml(watch) {
  if (!watch) return "";
  const target = esc(watch.target);
  if (watch.kind === "channel")
    return `<button type="button" class="callback-chip cb-channel" data-target="${target}"
      title="打开这个频道" onclick="openCallbackChannel(this.dataset.target)"><span
      class="cb-kind">频道</span><b>#${esc(callbackWatchChannel(watch.target)?.name || watch.target)}</b>
      <span>有新消息</span></button>`;
  if (watch.kind === "task")
    return `<button type="button" class="callback-chip cb-task" data-target="${target}"
      title="打开这个 Task" onclick="openCallbackTask(this.dataset.target)"><span
      class="cb-kind">Task</span><b>${target}</b> <span>有更新</span></button>`;
  const source = webhookSourceById(watch.target);
  const github = source?.kind === "github";
  const what = [watch.event, watch.action].filter(Boolean).join(" ") || "任意";
  return `<button type="button" class="callback-chip ${github ? "cb-github" : "cb-webhook"}"
    data-target="${target}" title="打开这个来源的配置" onclick="openCallbackSource(this.dataset.target)"><span
    class="cb-kind">${github ? "GitHub 来源" : source ? "Webhook 来源" : "事件来源"}</span><b>${
    esc(source?.name || watch.target)}</b> <span>收到 ${esc(what)} 事件</span></button>`;
}

// 盯事件的补充条件与检查进度,跟在标签后面
function callbackEventDetail(watch) {
  if (watch?.kind !== "event") return "";
  const parts = [];
  if (watch.repository) parts.push(`仓库 ${watch.repository}`);
  if (watch.has_filter) parts.push("且通过过滤脚本");
  if (watch.progress?.checked) parts.push(`已检查 ${watch.progress.checked} 个事件`);
  if (watch.progress?.errors) parts.push(`过滤脚本出错 ${watch.progress.errors} 次`);
  return parts.join(" · ");
}

// 时间标签:fire_at 到点一定回调(同时盯着更新时它是"最晚");只有 expires_at 的回调
// 到时仍无更新就过期,不再启动角色
function callbackTimeHtml(item) {
  const until = item.fire_at || item.expires_at;
  if (!until) return "";
  const expiring = !item.fire_at;
  const waitingEvent = item.watch?.kind === "event";
  const label = expiring ? "过期" : waitingEvent ? "最晚回调" : "到点回调";
  const title = expiring ? "到这个时间仍没有更新，回调过期，不再启动角色"
    : item.watch ? "到这个时间还没等到，也会回调" : "到这个时间回调";
  return `<span class="callback-chip ${expiring ? "cb-expire" : "cb-alarm"}" title="${title}"><span
    class="cb-kind">${expiring ? "⌛" : "⏰"} ${label}</span><b>${
    esc(fmtMessageTime(new Date(until * 1000)))}</b>
    <span class="callback-remaining" data-until="${Number(until)}"
      data-done="${expiring ? "即将过期" : "即将触发"}"></span></span>`;
}

function callbackConditionsHtml(item) {
  const watch = callbackWatchHtml(item.watch), time = callbackTimeHtml(item);
  if (!watch && !time) return `<span class="callback-when">${esc(localizeUtcTimes(item.conditions))}</span>`;
  // 频道 / Task 的更新与到点是"哪个先到算哪个"
  const either = watch && time && item.fire_at && item.watch.kind !== "event";
  const detail = callbackEventDetail(item.watch);
  return `${watch}${either ? '<span class="muted">或</span>' : ""}${time}${
    detail ? `<span class="callback-detail muted">${esc(detail)}</span>` : ""}`;
}

function openCallbackChannel(target) {
  const channel = callbackWatchChannel(target);
  if (!channel) { toast("这个频道已归档或已删除", "info"); return; }
  fdlg.close();
  selectChannel(channel.id);
}

function openCallbackTask(taskId) {
  fdlg.close();
  openTask(taskId, false).catch(() => {});
}

async function openCallbackSource(sourceId) {
  try { await loadWebhookSources(); } catch (_) { /* 用已有缓存 */ }
  if (!webhookSourceById(sourceId)) { toast("这个来源已删除", "info"); return; }
  // 来源编辑框复用同一个对话框:先关掉列表(摘掉视图标记),轮询刷新才不会把编辑框盖掉
  delete fdlg.dataset.view;
  fdlg.close();
  editWebhookSource(sourceId);
}

function renderChannelCallbacksDialog(opening = false) {
  const items = currentChannelCallbacks();
  const body = items.length ? `<ul class="callback-list">${items.map(item => `
      <li>
        <div class="callback-head">
          <b>#${item.id}</b><span class="callback-role">@${esc(item.role_id)}</span>
          <button class="ghost compact danger" type="button"
            onclick="cancelChannelCallback(${item.id})">取消</button>
        </div>
        <div class="callback-conditions">${callbackConditionsHtml(item)}</div>
        <div class="callback-note">${esc(item.note)}</div>
        <div class="callback-meta muted">登记于 ${esc(fmtMessageTime(new Date(item.created_at * 1000)))}</div>
      </li>`).join("")}</ul>`
    : '<p class="empty">本频道没有等待中的回调</p>';
  const actions = `<button class="ghost" onclick="fdlg.close()">关闭</button>`;
  if (opening) {
    openFormDialog("等待中的回调", body, actions);
  } else {
    document.getElementById("fdlg-body").innerHTML = body;
  }
  tickCallbackRemaining();
}

// 确认框叠在回调列表之上(独立的 udlg),列表随本地状态原地刷新
async function cancelChannelCallback(callbackId) {
  if (!await uiConfirm(`取消回调 #${callbackId}？取消后到点或有更新时不会再启动该角色。`)) return;
  try {
    await api("POST", `/api/chat/callbacks/${callbackId}/cancel`);
    toast(`已取消回调 #${callbackId}`, "success");
  } catch (e) { /* api() 已提示;已结束的回调同样从列表移除 */ }
  updateChannelCallbacks(currentChan, currentChannelCallbacks().filter(item => item.id !== callbackId));
}

function openChannelDialog() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  const repoOpts = (projObj()?.repos || []).filter(r => r.path).map(r =>
    `<option value="${esc(r.path)}">${esc(r.name || r.id)} — ${esc(r.path)}</option>`).join("");
  openFormDialog("新建频道", `
    <label>频道名(字母、数字、下划线、连字符;用 / 归入侧栏分组)</label>
    <input type="text" id="nc-id" placeholder="例如 backend-repo 或 backend/api">
    <label>频道用途/任务边界</label>
    <input type="text" id="nc-purpose" placeholder="例如：结算模块需求澄清与实现讨论">
    <label>工作目录(可选;默认平台内置工作区)</label>
    ${repoOpts ? `<select id="nc-workdir-sel" onchange="document.getElementById('nc-workdir').value=this.value">
      <option value="">(平台内置工作区)</option>${repoOpts}</select>` : ""}
    <input type="text" id="nc-workdir" placeholder="或直接输入路径 ~/code/myrepo" style="margin-top:6px">`,
    `<button class="action" onclick="createChannel()">创建频道</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
  setTimeout(() => document.getElementById("nc-id")?.focus(), 60);
}

async function createChannel() {
  const name = document.getElementById("nc-id").value.trim();
  if (!name) { uiAlert("频道名不能为空"); return; }
  // id 由服务端按名称推导:`backend/api` 的 id 是 `backend-api`
  await api("POST", "/api/chat/channels", {
    name,
    project_id: currentProject,
    purpose: document.getElementById("nc-purpose").value.trim(),
    workdir: document.getElementById("nc-workdir").value.trim() || null,
  });
  fdlg.close();
  await loadOverview(); renderSidebar();
  toast("频道已创建", "success");
}

// 名称/用途/工作目录;入口在侧栏频道的「•••」菜单(归档与内容专属频道不提供)
function openChannelEditDialog(id) {
  const channel = projChannels().find(item => item.id === id);
  if (!channel) return;
  document.querySelectorAll(".channel-actions-menu").forEach(item => { item.hidden = true; });
  const repoOpts = (projObj()?.repos || []).filter(r => r.path).map(r =>
    `<option value="${esc(r.path)}" ${r.path === channel.workdir ? "selected" : ""}>` +
    `${esc(r.name || r.id)} — ${esc(r.path)}</option>`).join("");
  openFormDialog(`编辑频道 #${channel.name || channel.id}`, `
    <label>显示名称(group/topic 形式归入侧栏分组)</label>
    <input type="text" id="ec-name" value="${esc(channel.name || "")}">
    <label>频道用途/任务边界</label>
    <input type="text" id="ec-purpose" value="${esc(channel.purpose || "")}">
    <label>工作目录(留空 = 平台内置工作区;从下一轮执行起生效)</label>
    ${repoOpts ? `<select onchange="document.getElementById('ec-workdir').value=this.value">
      <option value="">(平台内置工作区)</option>${repoOpts}</select>` : ""}
    <input type="text" id="ec-workdir" value="${esc(channel.workdir || "")}"
      placeholder="或直接输入路径 ~/code/myrepo" style="margin-top:6px">`,
    `<button class="action" data-channel-id="${esc(channel.id)}"
       onclick="saveChannelEdit(this.dataset.channelId)">保存</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
}

async function saveChannelEdit(id) {
  await api("PATCH", `/api/chat/channels/${encodeURIComponent(id)}`, {
    name: document.getElementById("ec-name").value.trim(),
    purpose: document.getElementById("ec-purpose").value.trim(),
    workdir: document.getElementById("ec-workdir").value.trim(),
  });
  fdlg.close();
  await loadOverview(); renderSidebar();
  toast("频道已更新", "success");
}

async function deleteChannel(id) {
  const channel = projChannels().find(item => item.id === id);
  const content = channelIsContent(channel || {});
  const message = content
    ? `永久清空频道 #${id} 的全部对话记录？对应的内容页不会删除；下次在页面发消息时会创建全新对话。此操作不可恢复。`
    : `将频道 #${id} 移入项目回收站？Runtime 会话将停止，消息记录仍保留用于审计。`;
  if (!id || !await uiConfirm(message, content ? "永久清空对话" : "回收频道")) return;
  await api("DELETE", `/api/chat/channels/${id}`);
  if (currentChan === id) currentChan = null;
  resetConfigChatChannel(id);
  await loadOverview(); renderSidebar();
  toast(content ? "内容页对话已永久清空" : "频道已移入回收站", "success");
}

async function archiveChannel(id) {
  if (!id || !await uiConfirm(`归档频道 #${id}？归档后频道只读，Agent 默认不会看到它。`, "归档频道")) return;
  const result = await api("POST", `/api/chat/channels/${id}/archive`);
  setConfigChatChannelArchived(id, true);
  await loadOverview();
  if (currentChan === id && channelFilter === "active") {
    const next = visibleProjChannels()[0];
    if (next) selectChannel(next.id, false);
  }
  renderSidebar();
  toast(`频道已归档${result.stopped_runtimes ? `，已停止 ${result.stopped_runtimes} 个持久实例` : ""}`, "success");
}

async function restoreChannel(id) {
  if (!id) return;
  await api("POST", `/api/chat/channels/${id}/restore`);
  setConfigChatChannelArchived(id, false);
  if (channelFilter === "archived") {
    channelFilter = "active";
    localStorage.setItem("mc.channelFilter", channelFilter);
  }
  await loadOverview(); renderSidebar();
  toast("频道已恢复", "success");
}

document.addEventListener("mousedown", event => {
  if (!event.target.closest(".channel-filter-wrap")) {
    const menu = document.getElementById("channel-filter-menu");
    menu.hidden = true;
    document.getElementById("channel-filter-btn").setAttribute("aria-expanded", "false");
  }
  if (!event.target.closest(".channel-item-actions"))
    document.querySelectorAll(".channel-actions-menu").forEach(item => { item.hidden = true; });
  // 点进弹层、按钮本身或模态框(Task 详情)都不收起关联 Task 弹层
  if (!event.target.closest("#channel-tasks-pop, #channel-tasks-btn, dialog"))
    closeChannelTasksPopover();
});
