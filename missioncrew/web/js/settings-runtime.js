/* ---------------- 全局设置:运行时(仿 Multica Runtime 页) ----------------
   只列支持的工具 + 安装状态/版本/路径 + 启停;成本/能力/档位在角色层配置 */
async function renderGlobalSettings() {
  await ensureTraits();
  renderGlobalRoleTable();
  await renderBackendTable();
  await renderModelProviders();
  // 能查到最新版就直接展示:首次进入自动静默查询,不用等用户点按钮
  if (!Object.keys(updateHints).length) checkUpdates(true);
}

/* ---------------- 全局设置:新项目角色模板 ---------------- */
function renderGlobalRoleTable() {
  const table = document.getElementById("global-role-table");
  if (!table) return;
  const rows = globalRoleTemplates().map((role, index) => {
    const execution = role.runtime_id
      ? `${esc(role.runtime_id)} / ${esc(role.model || "(CLI 默认)")}` +
        (role.effort ? ` / effort ${esc(role.effort)}` : "")
      : `<span class="muted">未绑定</span>`;
    return `<tr data-id="${esc(role.id)}">
      <td class="drag-handle" draggable="true" title="拖动排序"
          ondragstart="globalRoleDragStart(event)" ondragend="globalRoleDragEnd()">⠿</td>
      <td><span class="role-dot" style="background:${esc(role.color || "#888")};display:inline-block"></span>
          <b>@${esc(role.id)}</b> ${esc(role.name)}
          ${index === 0 ? `<span class="pill">新项目默认主控</span>` : ""}
          ${role.usage_linkage_enabled ? `<span class="pill">用量联动</span>` : ""}</td>
      <td class="muted">${esc(role.preference || "—")}</td>
      <td>${abilityPills(role) || "—"}</td>
      <td class="muted">${execution}</td>
      <td><button class="ghost" onclick="editGlobalRoleTemplate('${role.id}')">编辑</button></td></tr>`;
  }).join("");
  table.innerHTML =
    `<tr><th></th><th>角色</th><th>偏好</th><th>能力</th><th>Runtime / 模型</th><th></th></tr>` +
    (rows || `<tr><td colspan="6" class="empty">尚未配置角色模板</td></tr>`);
  table.ondragover = globalRoleDragOver;
  table.ondrop = event => event.preventDefault();
}

let _dragGlobalRoleRow = null, _dragGlobalRoleFrom = "";
const _globalRoleRowIds = () =>
  [...document.querySelectorAll("#global-role-table tr[data-id]")].map(row => row.dataset.id);

function globalRoleDragStart(event) {
  _dragGlobalRoleRow = event.target.closest("tr");
  _dragGlobalRoleFrom = _globalRoleRowIds().join(",");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", _dragGlobalRoleRow.dataset.id);
  event.dataTransfer.setDragImage(_dragGlobalRoleRow, 16, 16);
  _dragGlobalRoleRow.classList.add("dragging");
}

function globalRoleDragOver(event) {
  if (!_dragGlobalRoleRow) return;
  event.preventDefault();
  const over = event.target.closest("tr[data-id]");
  if (!over || over === _dragGlobalRoleRow) return;
  const box = over.getBoundingClientRect();
  over.parentNode.insertBefore(
    _dragGlobalRoleRow,
    event.clientY > box.top + box.height / 2 ? over.nextSibling : over,
  );
}

async function globalRoleDragEnd() {
  if (!_dragGlobalRoleRow) return;
  _dragGlobalRoleRow.classList.remove("dragging");
  _dragGlobalRoleRow = null;
  const ids = _globalRoleRowIds();
  if (ids.join(",") === _dragGlobalRoleFrom) return;
  try {
    await api("POST", "/api/role-templates/reorder", { ids });
    await loadOverview();
  } finally {
    renderGlobalRoleTable();
  }
}

function editGlobalRoleTemplate(id) {
  if (!id && !overview.backends.some(backend => backend.enabled)) {
    uiAlert("没有已启用的 runtime，请先检测并启用，再创建全局角色模板。");
    return;
  }
  const role = globalRoleTemplates().find(item => item.id === id) || {
    id: "", name: "", description: "", capabilities: [], preference: "",
    runtime_id: "", model: "", effort: "", color: "#3564d7",
    usage_linkage_enabled: false,
  };
  const abilityChips = Object.entries(traitMeta.abilities).map(([key, label]) =>
    `<span class="chip ${(role.capabilities || []).includes(key) ? "on" : ""}" data-cap="${key}"
       onclick="this.classList.toggle('on')">${esc(label)}</span>`).join("");
  const backendOptions = (role.runtime_id ? "" :
    `<option value="" disabled selected>选择 runtime…</option>`) + overview.backends.map(backend =>
      `<option value="${esc(backend.id)}" ${role.runtime_id === backend.id ? "selected" : ""}
         ${!backend.enabled && role.runtime_id !== backend.id ? "disabled" : ""}>` +
      `${esc(backend.id)} — ${esc(backend.name)}${backend.enabled ? "" : "(已停用)"}</option>`
    ).join("");
  window._editingRoleModel = role.model;
  window._editingRoleEffort = role.effort;
  openFormDialog(id ? `编辑全局角色模板 @${id}` : "新建全局角色模板", `
    <div class="row">
      <div><label>角色 id(@ 提及名)</label><input type="text" id="rf-id" value="${esc(role.id)}" ${id ? "disabled" : ""}></div>
      <div><label>显示名</label><input type="text" id="rf-name" value="${esc(role.name)}"></div>
      <div><label>标识色</label>${colorFieldHtml("rf-color", role.color)}</div>
    </div>
    <div class="row">
      <div><label>Runtime(复制到新项目后固定)</label>
        <select id="rf-backend" onchange="window._editingRoleModel=null;window._editingRoleEffort=null;refreshModelOptions();refreshEffortOptions()">${backendOptions}</select></div>
      <div><label>模型(清单来自 runtime)</label><select id="rf-model"></select></div>
      <div><label>Effort(推理力度)</label><select id="rf-effort"></select></div>
    </div>
    ${roleUsageLinkageField(role)}
    <label>角色定位/人格(给角色本人与主控看:写清"是谁、怎么工作"的专长画像;平台原样装配、不改写,任务由 @ 消息提供)</label>
    <textarea id="rf-desc" rows="3">${esc(role.description)}</textarea>
    <label>角色偏好(给主控选人看:何时该选它的领域/风格短标签,顿号分隔,如"前端"、"只审不改";名册中与能力并列展示)</label>
    <input type="text" id="rf-pref" value="${esc(role.preference || "")}">
    <label>角色能力(固定选项,同样给主控选人看;名册中与偏好并列展示为标签)</label>
    <div class="chips" id="rf-caps">${abilityChips}</div>`,
    `<button class="action" onclick="saveGlobalRoleTemplate()">保存</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>
     ${id ? `<button class="danger" onclick="deleteGlobalRoleTemplate('${id}')">删除模板</button>` : ""}`);
  refreshModelOptions();
  refreshEffortOptions();
}

async function saveGlobalRoleTemplate() {
  const runtimeId = document.getElementById("rf-backend").value;
  const body = {
    id: document.getElementById("rf-id").value.trim(),
    name: document.getElementById("rf-name").value.trim(),
    color: document.getElementById("rf-color").value.trim(),
    description: document.getElementById("rf-desc").value,
    capabilities: [...document.querySelectorAll("#rf-caps .chip.on")]
      .map(chip => chip.dataset.cap),
    preference: document.getElementById("rf-pref").value.trim(),
    runtime_id: runtimeId,
    model: document.getElementById("rf-model").value,
    effort: document.getElementById("rf-effort").value,
    usage_linkage_enabled: document.getElementById("rf-usage-linkage").classList.contains("on"),
  };
  if (!body.id) { uiAlert("角色 id 不能为空"); return; }
  if (!runtimeId) { uiAlert("请为角色选择 runtime"); return; }
  await api("POST", "/api/role-templates", body);
  await loadOverview();
  fdlg.close();
  renderGlobalRoleTable();
  await renderBackendTable();
  toast("全局角色模板已保存", "success");
}

async function deleteGlobalRoleTemplate(id) {
  if (!await uiConfirm(`删除全局角色模板 @${id}？已有项目角色不会受影响。`)) return;
  await api("DELETE", `/api/role-templates/${encodeURIComponent(id)}`);
  await loadOverview();
  fdlg.close();
  renderGlobalRoleTable();
  await renderBackendTable();
  toast("全局角色模板已删除", "success");
}

let updateHints = {};   // 检查更新的结果:{id: {latest, update_available, installed}}
let updatingIds = new Set();

function closeRuntimeRolePopovers() {
  document.querySelectorAll(".runtime-role-popover").forEach(pop => { pop.hidden = true; });
  document.querySelectorAll(".role-count-btn").forEach(btn => btn.setAttribute("aria-expanded", "false"));
}

function toggleRuntimeRolePopover(event, rowIndex) {
  event.stopPropagation();
  const pop = document.getElementById(`runtime-role-popover-${rowIndex}`);
  const btn = event.currentTarget;
  const shouldOpen = pop.hidden;
  closeRuntimeRolePopovers();
  if (shouldOpen) {
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
  }
}

function roleUsageCell(t, rowIndex) {
  const roles = t.role_users || [];
  if (!roles.length) return `<span class="muted">0</span>`;
  const items = roles.map(r => {
    const project = r.project_name || r.project_id || "未归属项目";
    const model = r.model ? ` · 模型 ${esc(r.model)}` : "";
    return `<div class="runtime-role-item">
      <div><b>@${esc(r.id)}</b>${r.name ? ` · ${esc(r.name)}` : ""}</div>
      <div class="runtime-role-meta">${esc(project)}${model}</div>
    </div>`;
  }).join("");
  return `<span class="runtime-role-usage">
    <button type="button" class="role-count-btn" aria-expanded="false"
      onclick="toggleRuntimeRolePopover(event, ${rowIndex})">${roles.length}</button>
    <div id="runtime-role-popover-${rowIndex}" class="runtime-role-popover" hidden
      onclick="event.stopPropagation()">
      <div class="runtime-role-popover-title">固定使用此运行时的角色</div>${items}
    </div>
  </span>`;
}

document.addEventListener("click", closeRuntimeRolePopovers);
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeRuntimeRolePopovers();
});

function updateCell(t) {
  if (!t.registered || !t.installed) return `<span class="muted">—</span>`;
  const hint = updateHints[t.id];
  if (updatingIds.has(t.id)) return `<span class="muted">更新中…</span>`;
  const btn = label =>
    `<button class="ghost" onclick="runUpdate('${esc(t.id)}')">${label}</button>`;
  if (hint && hint.update_available)
    return `<span class="pill" style="color:var(--warn);border-color:var(--warn)">
        可更新 → ${esc(hint.latest)}</span> ${t.updatable ? btn("更新") : ""}`;
  if (hint && hint.latest && !hint.update_available)   // 能查到最新版就展示出来
    return hint.installed
      ? `<span class="muted">已是最新 (${esc(hint.latest)})</span>`
      : `<span class="muted" title="已装版本未知,无法比对">最新版 ${esc(hint.latest)}</span>`;
  if (t.updatable)   // 没有可靠的最新版来源(如 kimi/trae),仍提供手动更新
    return btn("更新");
  return `<span class="muted" title="安装方式未知或由宿主程序托管">不支持自动更新</span>`;
}

async function renderBackendTable() {
  const tools = await (await fetch("/api/backends/tools")).json();
  // 已安装工具优先；稳定排序会保留每组在支持矩阵中的原始顺序。
  tools.sort((a, b) => Number(b.installed) - Number(a.installed));
  closeRuntimeRolePopovers();
  const rows = tools.map((t, rowIndex) => {
    const status = !t.installed
      ? `<span class="muted">未安装</span>`
      : `<span style="color:var(--ok)">✓ 已安装</span>` +
        (t.version ? ` <span class="pill">${esc(t.version)}</span>` : "");
    const toggle = t.registered
      ? `<span class="switch ${t.enabled ? "on" : ""}" role="switch"
           aria-checked="${t.enabled}" title="${t.enabled ? "已启用,点击停用" : "已停用,点击启用"}"
           onclick="toggleBackend('${esc(t.id)}', ${!t.enabled})"></span>`
      : "";
    return `<tr>
      <td><b>${esc(t.binary)}</b><br><span class="muted">${esc(t.adapter)}</span></td>
      <td>${status}</td>
      <td class="muted">${esc(t.path || "—")}</td>
      <td>${roleUsageCell(t, rowIndex)}</td>
      <td>${updateCell(t)}</td>
      <td>${toggle}</td></tr>`;
  }).join("");
  document.getElementById("backend-table").innerHTML =
    `<tr><th>工具</th><th>状态</th><th>路径</th>` +
    `<th title="仅统计固定到此运行时的角色">使用角色</th><th>更新</th><th></th></tr>` + rows;
}

async function checkUpdates(silent = false) {
  const btn = document.getElementById("btn-check-updates");
  btn.disabled = true; btn.textContent = "检查中…";
  try {
    const results = await api("POST", "/api/backends/check_updates");
    updateHints = Object.fromEntries(results.map(r => [r.id, r]));
    await renderBackendTable();
    if (!silent && !results.some(r => r.update_available)) {
      // "查询不到最新版"不等于"已是最新":断网/无 npm 源时如实说明
      const unknown = results.filter(r => !r.latest).map(r => r.id);
      if (unknown.length === results.length && results.length)
        uiAlert("无法从 npm 源获取任何最新版本信息(可能离线),无法确认是否有更新。");
      else
        uiAlert("可检查的工具都已是最新版本。" +
              (unknown.length ? `\n无法确认(无版本源): ${unknown.join(", ")}` : ""));
    }
  } finally {
    btn.disabled = false; btn.textContent = "检查更新";
  }
}

async function runUpdate(id) {
  const hint = updateHints[id];
  const target = hint && hint.latest ? `到 ${hint.latest}` : "到最新版本";
  if (!await uiConfirm(`更新 ${id} ${target}?更新期间该工具不会被派发新的执行。`)) return;
  updatingIds.add(id);
  await renderBackendTable();
  try {
    const r = await api("POST", `/api/backends/${encodeURIComponent(id)}/update`);
    uiAlert(r.ok
      ? `更新完成:${r.old_version || "?"} → ${r.version || "?"}`
      : `更新失败:\n${r.log || "(无输出)"}`);
    if (r.ok) delete updateHints[id];   // 失败时版本未变,"可更新"提示仍有效
  } finally {
    updatingIds.delete(id);
    await renderBackendTable();          // 先清"更新中…",再刷新全局数据
    try { await loadOverview(); } catch (e) { /* 服务重启间隙,忽略 */ }
    await renderBackendTable();
  }
}

async function toggleBackend(id, enabled) {
  await api("POST", "/api/backends", { id, enabled });
  await loadOverview(); renderBackendTable();
  toast(enabled ? `已启用 ${id}` : `已停用 ${id}`, "success");
}

async function detectBackends() {
  const d = await api("POST", "/api/backends/detect");
  uiAlert(`检测到: ${d.found.join(", ") || "无"}\n新注册: ${d.added.join(", ") || "无"}\n已刷新: ${d.updated.join(", ") || "无"}`);
  updateHints = {};        // 已装版本可能已变,旧比对结果作废
  await loadOverview();
  await renderBackendTable();
  checkUpdates(true);      // 用新版本静默重新比对
}
