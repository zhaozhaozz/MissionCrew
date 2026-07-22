/* ---- 频道(当前项目) ---- */
function channelSidebarItem(channel) {
  const general = channelIsGeneral(channel);
  const status = channel.archived ? `<span class="channel-state">已归档</span>` : "";
  const actions = general ? "" : `<span class="channel-item-actions">
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
    <span class="channel-name"># ${esc(channel.name || channel.id)}</span>${status}${actions}</div>`;
}

function renderChannelFilter() {
  const all = projChannels();
  const active = all.filter(channel => !channel.archived).length;
  const archived = all.length - active;
  document.getElementById("channel-filter-all-count").textContent = all.length;
  document.getElementById("channel-filter-active-count").textContent = active;
  document.getElementById("channel-filter-archived-count").textContent = archived;
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
}

function toggleChannelActions(event, button) {
  event.stopPropagation();
  const menu = button.nextElementSibling;
  const opening = menu.hidden;
  document.querySelectorAll(".channel-actions-menu").forEach(item => { item.hidden = true; });
  document.getElementById("channel-filter-menu").hidden = true;
  menu.hidden = !opening;
}

function renderChannelState() {
  const channel = projChannels().find(item => item.id === currentChan);
  const archived = Boolean(channel?.archived);
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

function renderChanTable() {
  const rows = projChannels().map(c => `<tr>
    <td><b># ${esc(c.name || c.id)}</b> <span class="muted">${esc(c.id)}</span></td>
    <td>${esc(c.purpose || "(未说明)")}</td>
    <td class="muted">${esc(c.workdir || "(平台内置工作区)")}</td>
    <td class="muted">${esc(c.created_by_role_id ? "@" + c.created_by_role_id : "human/platform")}</td>
    <td>${c.archived ? `<span class="badge">已归档</span>` : `<span class="badge">活跃</span>`}</td>
    <td>${channelIsGeneral(c) ? `<span class="muted">默认频道</span>` : `
      ${c.archived
        ? `<button class="ghost" data-channel-id="${esc(c.id)}" onclick="restoreChannel(this.dataset.channelId)">恢复</button>`
        : `<button class="ghost" data-channel-id="${esc(c.id)}" onclick="archiveChannel(this.dataset.channelId)">归档</button>`}
      <button class="danger" data-channel-id="${esc(c.id)}" onclick="deleteChannel(this.dataset.channelId)">删除</button>`}</td>
  </tr>`).join("");
  document.getElementById("chan-table").innerHTML =
    `<tr><th>频道</th><th>用途</th><th>工作目录</th><th>创建者</th><th>状态</th><th></th></tr>` + rows;
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
  await loadOverview(); renderChanTable(); renderSidebar();
  toast("频道已创建", "success");
}

async function deleteChannel(id) {
  if (!id || !await uiConfirm(`将频道 #${id} 移入项目回收站？Runtime 会话将停止，消息记录仍保留用于审计。`, "回收频道")) return;
  await api("DELETE", `/api/chat/channels/${id}`);
  if (currentChan === id) currentChan = null;
  await loadOverview(); renderChanTable(); renderSidebar();
  toast("频道已移入回收站", "success");
}

async function archiveChannel(id) {
  if (!id || !await uiConfirm(`归档频道 #${id}？归档后频道只读，Agent 默认不会看到它。`, "归档频道")) return;
  const result = await api("POST", `/api/chat/channels/${id}/archive`);
  await loadOverview();
  if (currentChan === id && channelFilter === "active") {
    const next = visibleProjChannels()[0];
    if (next) selectChannel(next.id, false);
  }
  renderChanTable(); renderSidebar();
  toast(`频道已归档${result.stopped_runtimes ? `，已停止 ${result.stopped_runtimes} 个持久实例` : ""}`, "success");
}

async function restoreChannel(id) {
  if (!id) return;
  await api("POST", `/api/chat/channels/${id}/restore`);
  if (channelFilter === "archived") {
    channelFilter = "active";
    localStorage.setItem("mc.channelFilter", channelFilter);
  }
  await loadOverview(); renderChanTable(); renderSidebar();
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
