/* ---- 频道(当前项目) ---- */
function channelSidebarItem(channel) {
  const general = channelIsGeneral(channel);
  const managed = general;
  const status = channel.archived ? `<span class="channel-state">已归档</span>` : "";
  // 频道内有排队/执行/等待用户的运行时亮起；轮询到新状态后就地更新，不必重绘侧栏
  const runCount = channelRunningCount(channel);
  const runMarker = `<span class="channel-running-marker" data-channel-id="${esc(channel.id)}"
    title="${channelRunningTitle(runCount)}" role="img"
    aria-label="${channelRunningTitle(runCount)}" ${runCount ? "" : "hidden"}><i></i></span>`
    // 后台命令(跨 turn 存活、不阻塞对话)另用沙漏标记,由 Runtime 状态轮询点亮
    + `<span class="channel-background-marker" data-channel-id="${esc(channel.id)}"
    role="img" hidden>⏳</span>`;
  const actions = managed ? "" : `<span class="channel-item-actions">
    <button class="channel-more" type="button" aria-label="频道操作" title="频道操作"
      onclick="toggleChannelActions(event,this)">•••</button>
    <span class="channel-actions-menu" hidden onclick="event.stopPropagation()">
      ${channel.archived
        ? `<button type="button" data-channel-id="${esc(channel.id)}"
             onclick="restoreChannel(this.dataset.channelId)">恢复频道</button>`
        : `<button type="button" data-channel-id="${esc(channel.id)}"
             onclick="archiveChannel(this.dataset.channelId)">归档频道</button>`}
      <button class="danger-text" type="button" data-channel-id="${esc(channel.id)}"
        onclick="deleteChannel(this.dataset.channelId)">删除频道</button>
    </span>
  </span>`;
  return `<div class="side-item channel-item ${channel.archived ? "archived" : ""} ${
      channel.id === currentChan && currentTab === "chat" ? "selected" : ""}"
      data-channel-id="${esc(channel.id)}" onclick="selectChannel(this.dataset.channelId)"
      title="${esc(channel.purpose || "")}">
    <span class="channel-name"># ${esc(channel.name || channel.id)}</span>${runMarker}${status}${actions}</div>`;
}

const channelRunningCount = channel => Number(channel?.active_run_count || 0);
const channelRunningTitle = count => `${count || 1} 个 Agent 正在运行`;

/* 侧栏频道的运行标记；总览 8s 一轮，当前频道与内容频道轮询会更早更新计数。 */
function refreshChannelRunningMarkers() {
  const counts = new Map(overview.channels.map(
    channel => [channel.id, channelRunningCount(channel)]));
  document.querySelectorAll(".channel-running-marker").forEach(marker => {
    const count = counts.get(marker.dataset.channelId) || 0;
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

function openChannelDialog() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  const repoOpts = (projObj()?.repos || []).filter(r => r.path).map(r =>
    `<option value="${esc(r.path)}">${esc(r.name || r.id)} — ${esc(r.path)}</option>`).join("");
  openFormDialog("新建频道", `
    <label>频道名(字母、数字、下划线、连字符)</label>
    <input type="text" id="nc-id" placeholder="例如 backend-repo">
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
  const id = document.getElementById("nc-id").value.trim();
  if (!id) { uiAlert("频道名不能为空"); return; }
  await api("POST", "/api/chat/channels", {
    id, name: id,
    project_id: currentProject,
    purpose: document.getElementById("nc-purpose").value.trim(),
    workdir: document.getElementById("nc-workdir").value.trim() || null,
  });
  fdlg.close();
  await loadOverview(); renderSidebar();
  toast("频道已创建", "success");
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
});
