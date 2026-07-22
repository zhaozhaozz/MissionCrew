/* ---- 角色(当前项目) ---- */
function abilityPills(role) {
  return (role.capabilities || []).map(c =>
    `<span class="pill">${esc(traitMeta.abilities[c] || c)}</span>`).join("");
}

function renderRoleTable() {
  const project = overview.projects.find(p => p.id === currentProject);
  const rows = projRoles().map(r => {   // 已按 sort_order 排好(服务端顺序)
    const exec = r.runtime_id
      ? `${esc(r.runtime_id)} / ${esc(r.model || "(CLI 默认)")}` +
        (r.effort ? ` / effort ${esc(r.effort)}` : "")
      : `<span class="muted">未绑定(请编辑角色选择 runtime)</span>`;
    return `<tr data-id="${esc(r.id)}">
      <td class="drag-handle" draggable="true" title="拖动排序"
          ondragstart="roleDragStart(event)" ondragend="roleDragEnd()">⠿</td>
      <td><span class="role-dot" style="background:${esc(r.color || "#888")};display:inline-block"></span>
          <b>@${esc(r.id)}</b> ${esc(r.name)} ${r.id === project?.orchestrator_role_id ? `<span class="pill">主控</span>` : ""}</td>
      <td class="muted">${esc(r.preference || "—")}</td>
      <td>${abilityPills(r) || "—"}</td>
      <td class="muted">${exec}</td>
      <td><button class="ghost" onclick="editRole('${r.id}')">编辑</button></td></tr>`;
  }).join("");
  const table = document.getElementById("role-table");
  table.innerHTML =
    `<tr><th></th><th>角色</th><th>偏好</th><th>能力</th><th>Runtime / 模型</th><th></th></tr>` + rows;
  table.ondragover = roleDragOver;              // 插入点判定放在表级,行随拖动实时移位
  table.ondrop = e => e.preventDefault();       // 阻止浏览器对放置数据的默认处理
}

// 拖动排序:拖手柄实时移动整行,松手后提交项目全部角色 id 的新顺序,
// 服务端整体重排 sort_order。顺序影响设置页、侧栏角色列表和提示词名册。
let _dragRoleRow = null, _dragRoleFrom = "";

const _roleRowIds = () =>
  [...document.querySelectorAll("#role-table tr[data-id]")].map(t => t.dataset.id);

function roleDragStart(e) {
  _dragRoleRow = e.target.closest("tr");
  _dragRoleFrom = _roleRowIds().join(",");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", _dragRoleRow.dataset.id);  // Firefox 必需
  e.dataTransfer.setDragImage(_dragRoleRow, 16, 16);              // 拖影用整行而非手柄格
  _dragRoleRow.classList.add("dragging");
}

function roleDragOver(e) {
  if (!_dragRoleRow) return;
  e.preventDefault();                            // 声明本表可放置
  const over = e.target.closest("tr[data-id]");
  if (!over || over === _dragRoleRow) return;
  const mid = over.getBoundingClientRect();
  const after = e.clientY > mid.top + mid.height / 2;
  over.parentNode.insertBefore(_dragRoleRow, after ? over.nextSibling : over);
}

async function roleDragEnd() {
  if (!_dragRoleRow) return;
  _dragRoleRow.classList.remove("dragging");
  _dragRoleRow = null;
  const ids = _roleRowIds();
  if (ids.join(",") === _dragRoleFrom) return;   // 顺序没变,不发请求
  try {
    await api("POST", "/api/roles/reorder", { project_id: currentProject, ids });
    await loadOverview();
  } finally {   // 成功按新数据重绘;失败回退到服务端顺序
    renderRoleTable(); renderSidebar();
  }
}

function editRole(id, templateId = "") {
  // runtime 是定义角色时的必选项:没有已启用的 runtime 就没法新建,提前引导
  if (!id && !overview.backends.some(b => b.enabled)) {
    uiAlert("没有已启用的 runtime,请先在全局设置的 Runtime 页注册并启用,再创建角色。");
    return;
  }
  const template = !id && templateId
    ? globalRoleTemplates().find(role => role.id === templateId)
    : null;
  if (templateId && !template) {
    uiAlert(`全局角色模板 @${templateId} 不存在，可能已被删除。`);
    return;
  }
  const r = projRoles().find(x => x.id === id) || template || {
    id: "", name: "", description: "", capabilities: [], preference: "",
    runtime_id: "", model: "", effort: "", color: "#3564d7" };
  const abilityChips = Object.entries(traitMeta.abilities).map(([k, label]) =>
    `<span class="chip ${(r.capabilities || []).includes(k) ? "on" : ""}" data-cap="${k}"
       onclick="this.classList.toggle('on')">${esc(label)}</span>`).join("");
  // 停用的 runtime 不可选(角色当前绑定的除外,保留以如实显示现状)
  const backendOpts = (r.runtime_id ? "" : `<option value="" disabled selected>选择 runtime…</option>`) +
    overview.backends.map(b =>
      `<option value="${esc(b.id)}" ${r.runtime_id === b.id ? "selected" : ""}
         ${!b.enabled && r.runtime_id !== b.id ? "disabled" : ""}>` +
      `${esc(b.id)} — ${esc(b.name)}${b.enabled ? "" : "(已停用)"}</option>`).join("");
  window._editingRoleModel = r.model;    // 供模型下拉初始化选中
  window._editingRoleEffort = r.effort;  // 供 effort 下拉初始化选中
  window._editingProjectRoleId = id || "";  // 新建流程遇到同 id 时要求用户确认覆盖
  const existingIds = new Set(projRoles().map(role => role.id));
  const templateOptions = globalRoleTemplates().map(role =>
    `<option value="${esc(role.id)}" ${templateId === role.id ? "selected" : ""}>` +
    `@${esc(role.id)} — ${esc(role.name || role.id)}` +
    `${existingIds.has(role.id) ? "（项目已有同名角色，保存时可确认覆盖）" : ""}</option>`
  ).join("");
  const importControl = id ? "" : `
    <label>从全局角色模板导入（可选）</label>
    <select id="rf-template" onchange="importGlobalRoleTemplate(this.value)">
      <option value="" ${templateId ? "" : "selected"}>不使用模板，从空白角色开始</option>
      ${templateOptions}
    </select>
    <p class="muted">导入只会把模板配置填入下方表单，全局模板不会被改动；若 id 与项目现有角色相同，保存时需确认覆盖。</p>`;
  openFormDialog(id ? `编辑角色 @${id}` : "新建角色", `
    ${importControl}
    <div class="row">
      <div><label>角色 id(@ 提及名)</label><input type="text" id="rf-id" value="${esc(r.id)}" ${id ? "disabled" : ""}></div>
      <div><label>显示名</label><input type="text" id="rf-name" value="${esc(r.name)}"></div>
      <div><label>标识色</label><input type="text" id="rf-color" value="${esc(r.color)}"></div>
    </div>
    <div class="row">
      <div><label>Runtime(定义角色时固定,必选)</label>
        <select id="rf-backend" onchange="window._editingRoleModel=null;window._editingRoleEffort=null;refreshModelOptions();refreshEffortOptions()">${backendOpts}</select></div>
      <div><label>模型(清单来自 runtime)</label>
        <select id="rf-model"></select></div>
      <div><label>Effort(推理力度,仅部分 runtime 支持)</label>
        <select id="rf-effort"></select></div>
    </div>
    <label>角色定位/人格(专长画像,供调度选人;自由文本,平台原样装配、不改写;任务由 @ 消息提供)</label>
    <textarea id="rf-desc" rows="3">${esc(r.description)}</textarea>
    <label>角色能力(固定选项;在名册中展示,供主控按能力选人)</label>
    <div class="chips" id="rf-caps">${abilityChips}</div>
    <label>角色偏好(自由文本:风格/领域,如"前端"、"后端,偏好 Go";供主控选人参考)</label>
    <input type="text" id="rf-pref" value="${esc(r.preference || "")}">`,
    `<button class="action" onclick="saveRole()">保存</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>
     ${id ? `<button class="danger" onclick="deleteRole('${id}')">删除角色</button>` : ""}`);
  refreshModelOptions();
  refreshEffortOptions();
}

function importGlobalRoleTemplate(templateId) {
  // 复用角色编辑器的完整渲染路径，确保 Runtime/模型/Effort 下拉同步刷新。
  fdlg.close();
  editRole(null, templateId);
}

// 每个角色必须先选 runtime;模型清单向 runtime 本体动态查询(仿 Multica),
// 配置阶梯(带档位/成本)与 runtime 目录合并展示,服务端缓存 10 分钟。
const modelCatalogCache = {};   // backend id -> {configured, discovered}

async function refreshModelOptions() {
  const bid = document.getElementById("rf-backend").value;
  const sel = document.getElementById("rf-model");
  const cur = window._editingRoleModel;
  if (!bid) {
    sel.innerHTML = `<option value="">先选择 runtime</option>`;
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  let catalog = modelCatalogCache[bid];
  if (!catalog) {
    sel.innerHTML = `<option value="">加载模型清单…</option>`;
    try {
      catalog = await api("GET", `/api/backends/${encodeURIComponent(bid)}/models`);
      modelCatalogCache[bid] = catalog;
    } catch (e) {   // 查询失败:退回配置阶梯
      const b = overview.backends.find(x => x.id === bid);
      catalog = { configured: b?.models || [], discovered: [] };
    }
    if (document.getElementById("rf-backend")?.value !== bid) return;  // 期间已切换
  }
  const configured = catalog.configured || [];
  const configuredNames = new Set(configured.map(m => String(m.name ?? "")));
  let opts = configured.map(m => {
    const name = String(m.name ?? "");
    return `<option value="${esc(name)}" ${cur === name ? "selected" : ""}>` +
      `${esc(name || "(CLI 默认)")}${m.tier ? ` (${esc(m.tier)} · ${m.cost})` : ""}</option>`;
  }).join("");
  if (!configuredNames.has(""))
    opts = `<option value="" ${cur === "" ? "selected" : ""}>(CLI 默认)</option>` + opts;
  const extra = (catalog.discovered || []).filter(n => !configuredNames.has(n));
  if (extra.length)
    opts += `<optgroup label="来自 runtime">` + extra.map(n =>
      `<option value="${esc(n)}" ${cur === n ? "selected" : ""}>${esc(n)}</option>`).join("") +
      `</optgroup>`;
  if (cur && !configuredNames.has(cur) && !extra.includes(cur))
    opts += `<option value="${esc(cur)}" selected>${esc(cur)}(当前值)</option>`;
  sel.innerHTML = opts;
}

// effort(推理力度)是 adapter 级静态选项(词表来自 /api/traits):
// 选中的 runtime 支持才可配置,不支持时下拉禁用、保存为空。
function refreshEffortOptions() {
  const sel = document.getElementById("rf-effort");
  const bid = document.getElementById("rf-backend").value;
  const adapter = overview.backends.find(x => x.id === bid)?.adapter;
  const levels = (adapter && traitMeta.effort_options?.[adapter]) || [];
  const cur = window._editingRoleEffort ?? sel.value;
  window._editingRoleEffort = null;
  if (!levels.length) {
    sel.innerHTML = `<option value="">${bid ? "(该 runtime 不支持)" : "先选择 runtime"}</option>`;
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  sel.innerHTML = `<option value="">(CLI 默认)</option>` + levels.map(l =>
    `<option value="${esc(l)}" ${cur === l ? "selected" : ""}>${esc(l)}</option>`).join("");
}

async function saveRole() {
  const capabilities = [...document.querySelectorAll("#rf-caps .chip.on")].map(c => c.dataset.cap);
  const runtime_id = document.getElementById("rf-backend").value;
  const body = {
    id: document.getElementById("rf-id").value.trim(),
    project_id: currentProject,
    name: document.getElementById("rf-name").value.trim(),
    color: document.getElementById("rf-color").value.trim(),
    description: document.getElementById("rf-desc").value,
    capabilities,
    preference: document.getElementById("rf-pref").value.trim(),
    runtime_id,
    model: document.getElementById("rf-model").value,
    effort: document.getElementById("rf-effort").value,
  };
  if (!body.id) { uiAlert("角色 id 不能为空"); return; }
  if (!runtime_id) { uiAlert("请为角色选择 runtime(定义时固定执行组合)"); return; }
  const overwriting = !window._editingProjectRoleId &&
    projRoles().some(role => role.id === body.id);
  if (overwriting && !await uiConfirm(
    `项目中已存在角色 @${body.id}。确认用当前表单配置覆盖它？原角色的排序位置会保留。`,
    "确认覆盖角色",
  )) return;
  await api("POST", "/api/roles", body);
  await loadOverview();
  fdlg.close();
  renderRoleTable(); renderSidebar();
  toast(overwriting ? "角色已覆盖" : "角色已保存", "success");
}

async function deleteRole(id) {
  if (!await uiConfirm(`将项目「${currentProject}」的角色 @${id} 移入回收站？`)) return;
  await api("DELETE", `/api/roles/${id}?project_id=${encodeURIComponent(currentProject)}`);
  await loadOverview();
  fdlg.close();
  renderRoleTable(); renderSidebar();
  toast("角色已移入回收站", "success");
}
