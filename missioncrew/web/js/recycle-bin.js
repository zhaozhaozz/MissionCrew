/* ---- 项目统一回收站 ---- */
let recycleBinItems = [];

const RECYCLE_TYPE_LABELS = Object.freeze({
  document: "文档",
  guideline: "准则",
  skill: "Skill",
  dashboard: "面板",
  task: "Task",
  channel: "频道",
  role: "角色",
  project_resource: "项目资源",
});

function recycleSize(size) {
  const value = Number(size || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

async function renderRecycleBin(force = false) {
  if (!currentProject || (currentTab !== "recycle-bin" && !force)) return;
  const projectId = currentProject;
  const result = await api(
    "GET", `/api/projects/${encodeURIComponent(projectId)}/recycle-bin`);
  if (projectId !== currentProject) return;
  recycleBinItems = result.items || [];
  document.getElementById("recycle-bin-project").textContent = `— 项目「${projectId}」`;
  renderRecycleBinTable();
}

function renderRecycleBinTable() {
  const table = document.getElementById("recycle-bin-table");
  if (!table) return;
  const filter = document.getElementById("recycle-bin-filter")?.value || "all";
  const visible = recycleBinItems.filter(item =>
    filter === "all" || item.resource_type === filter);
  document.getElementById("recycle-bin-summary").textContent =
    `共 ${recycleBinItems.length} 项${filter === "all" ? "" : `，当前显示 ${visible.length} 项`}`;
  document.getElementById("empty-recycle-bin-btn").disabled = !recycleBinItems.length;
  const rows = visible.map(item => `<tr>
    <td><span class="badge">${esc(RECYCLE_TYPE_LABELS[item.resource_type] || item.resource_type)}</span></td>
    <td><b>${esc(item.name || item.resource_id)}</b><div class="muted recycle-resource-id">${esc(item.resource_id)}</div></td>
    <td>${new Date(Number(item.deleted_at) * 1000).toLocaleString()}</td>
    <td>${esc(item.actor || "human")}</td>
    <td>${recycleSize(item.size)}</td>
    <td class="recycle-row-actions">
      <button class="ghost compact" data-item-id="${esc(item.id)}"
        onclick="restoreRecycleItem(this.dataset.itemId)">恢复</button>
      <button class="danger compact" data-item-id="${esc(item.id)}"
        onclick="purgeRecycleItem(this.dataset.itemId)">永久删除</button>
    </td>
  </tr>`).join("");
  table.innerHTML = `<tr><th>类型</th><th>名称 / 原标识</th><th>删除时间</th>` +
    `<th>操作者</th><th>大小</th><th></th></tr>` +
    (rows || `<tr><td colspan="6" class="empty">${
      recycleBinItems.length ? "当前筛选下没有条目" : "回收站为空"}</td></tr>`);
}

async function restoreRecycleItem(itemId) {
  const item = recycleBinItems.find(candidate => candidate.id === itemId);
  if (!item || !await uiConfirm(
    `恢复${RECYCLE_TYPE_LABELS[item.resource_type] || "资源"}「${item.name || item.resource_id}」？`,
    "恢复回收项")) return;
  await api("POST",
    `/api/projects/${encodeURIComponent(currentProject)}/recycle-bin/${encodeURIComponent(itemId)}/restore`);
  await loadOverview();
  await renderRecycleBin(true);
  toast("资源已恢复", "success");
}

async function purgeRecycleItem(itemId) {
  const item = recycleBinItems.find(candidate => candidate.id === itemId);
  if (!item || !await uiConfirm(
    `永久删除「${item.name || item.resource_id}」的回收副本？此操作不可撤销。`,
    "永久删除")) return;
  await api("DELETE",
    `/api/projects/${encodeURIComponent(currentProject)}/recycle-bin/${encodeURIComponent(itemId)}`);
  await renderRecycleBin(true);
  toast("回收项已永久删除", "success");
}

async function emptyRecycleBin() {
  if (!recycleBinItems.length || !await uiConfirm(
    `永久删除项目「${currentProject}」回收站中的 ${recycleBinItems.length} 项资源？此操作不可撤销。`,
    "清空回收站")) return;
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/recycle-bin`);
  await renderRecycleBin(true);
  toast("回收站已清空", "success");
}
