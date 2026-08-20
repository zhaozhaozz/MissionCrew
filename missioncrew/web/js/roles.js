/* ---- 角色(当前项目) ---- */
function abilityPills(role) {
  return (role.capabilities || []).map(c =>
    `<span class="pill">${esc(traitMeta.abilities[c] || c)}</span>`).join("");
}

/* ---- 角色配置文件导入/导出(全局模板与项目角色共用格式) ---- */
const ROLE_FILE_FORMAT = "missioncrew.roles";
const ROLE_FILE_VERSION = 1;
const ROLE_FILE_FIELDS = [
  "id", "name", "description", "runtime_id", "model", "effort",
  "usage_linkage_enabled", "capabilities", "preference", "color",
];
let roleTransferState = null;

function roleTransferSource(scope) {
  return scope === "global"
    ? { roles: globalRoleTemplates(), label: "全局角色模板", projectId: "" }
    : { roles: projRoles(), label: `项目「${currentProject}」角色`, projectId: currentProject };
}

function roleFileItem(role) {
  return Object.fromEntries(ROLE_FILE_FIELDS.map(field => {
    const fallback = field === "capabilities" ? []
      : field === "usage_linkage_enabled" ? false : "";
    return [field, role[field] ?? fallback];
  }));
}

function roleTransferRows(roles, conflicts = null) {
  return roles.map((role, index) => {
    const conflict = conflicts?.has(role.id) === true;
    const execution = `${esc(role.runtime_id || "未绑定")} / ${esc(role.model || "CLI 默认")}`;
    return `<label class="role-transfer-item ${conflict ? "will-overwrite" : ""}">
      <input type="checkbox" data-role-transfer-index="${index}" checked
        onchange="updateRoleTransferSummary()">
      <span class="role-transfer-main"><b>@${esc(role.id)}</b>
        ${role.name ? `<span class="role-transfer-name">· ${esc(role.name)}</span>` : ""}
        <small>· ${execution}</small></span>
      ${conflicts ? (conflict ? `<span class="pill role-overwrite-pill">将覆盖现有角色</span>`
        : `<span class="pill">新增</span>`) : ""}
    </label>`;
  }).join("");
}

function selectedRoleTransferIndexes() {
  return [...document.querySelectorAll("[data-role-transfer-index]:checked")]
    .map(input => Number(input.dataset.roleTransferIndex));
}

function setAllRoleTransferSelections(checked) {
  document.querySelectorAll("[data-role-transfer-index]").forEach(input => {
    input.checked = checked;
  });
  updateRoleTransferSummary();
}

function updateRoleTransferSummary() {
  const summary = document.getElementById("role-transfer-summary");
  if (!summary || !roleTransferState) return;
  const indexes = selectedRoleTransferIndexes();
  if (roleTransferState.mode === "export") {
    summary.textContent = `已选择 ${indexes.length} / ${roleTransferState.roles.length} 个角色`;
    return;
  }
  const overwritten = indexes
    .map(index => roleTransferState.roles[index].id)
    .filter(id => roleTransferState.conflicts.has(id));
  summary.textContent = overwritten.length
    ? `已选择 ${indexes.length} 个；其中 ${overwritten.length} 个会覆盖：` +
      overwritten.map(id => `@${id}`).join("、")
    : `已选择 ${indexes.length} 个；不会覆盖现有角色`;
  summary.classList.toggle("has-overwrite", overwritten.length > 0);
}

function openRoleExportDialog(scope) {
  const source = roleTransferSource(scope);
  if (!source.roles.length) {
    uiAlert(`${source.label}为空，没有可导出的角色。`);
    return;
  }
  roleTransferState = {
    mode: "export", scope, roles: source.roles.map(roleFileItem),
    projectId: source.projectId, conflicts: new Set(),
  };
  openFormDialog(`导出${source.label}`, `
    <div class="role-transfer-toolbar">
      <span id="role-transfer-summary"></span>
      <button type="button" class="ghost compact" onclick="setAllRoleTransferSelections(true)">全选</button>
      <button type="button" class="ghost compact" onclick="setAllRoleTransferSelections(false)">全不选</button>
    </div>
    <div class="role-transfer-list">${roleTransferRows(roleTransferState.roles)}</div>
    <p class="muted">导出文件只包含角色配置和当前顺序，不包含项目归属与实时启停状态。</p>`,
    `<button class="action" onclick="downloadSelectedRoles()">导出所选</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
  updateRoleTransferSummary();
}

function downloadSelectedRoles() {
  const indexes = selectedRoleTransferIndexes();
  if (!indexes.length) {
    uiAlert("请至少勾选一个要导出的角色。");
    return;
  }
  const state = roleTransferState;
  const payload = {
    format: ROLE_FILE_FORMAT,
    version: ROLE_FILE_VERSION,
    scope: state.scope,
    exported_at: new Date().toISOString(),
    roles: indexes.map(index => state.roles[index]),
  };
  if (state.projectId) payload.project_id = state.projectId;
  const blob = new Blob([JSON.stringify(payload, null, 2) + "\n"],
    { type: "application/json;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  const prefix = state.scope === "global" ? "global" : state.projectId;
  link.download = `missioncrew-${prefix}-roles-${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
  fdlg.close();
  toast(`已导出 ${indexes.length} 个角色`, "success");
}

function chooseRoleImportFile(scope) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,application/json";
  input.onchange = () => input.files?.[0] && previewRoleImportFile(scope, input.files[0]);
  input.click();
}

async function previewRoleImportFile(scope, file) {
  if (file.size > 2 * 1024 * 1024) {
    await uiAlert("角色配置文件不能超过 2 MiB。");
    return;
  }
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch (error) {
    await uiAlert("无法解析 JSON 文件，请确认文件内容完整。", "导入失败");
    return;
  }
  if (payload?.format !== ROLE_FILE_FORMAT || payload?.version !== ROLE_FILE_VERSION ||
      !["global", "project"].includes(payload?.scope) || !Array.isArray(payload?.roles)) {
    await uiAlert("不是受支持的 MissionCrew 角色配置文件。", "导入失败");
    return;
  }
  if (!payload.roles.length || payload.roles.length > 500) {
    await uiAlert("角色配置文件必须包含 1–500 个角色。", "导入失败");
    return;
  }
  const ids = payload.roles.map(role => role?.id);
  if (ids.some(id => typeof id !== "string" || !id) || new Set(ids).size !== ids.length) {
    await uiAlert("角色配置文件包含空 id 或重复 id。", "导入失败");
    return;
  }
  const target = roleTransferSource(scope);
  const conflicts = new Set(target.roles.map(role => role.id).filter(id => ids.includes(id)));
  roleTransferState = {
    mode: "import", scope, projectId: target.projectId,
    roles: payload.roles.map(roleFileItem), conflicts,
  };
  const sourceLabel = payload.scope === "global"
    ? "全局角色模板" : `项目角色${payload.project_id ? `（${esc(payload.project_id)}）` : ""}`;
  openFormDialog(`导入到${target.label}`, `
    <p class="muted">文件来源：${sourceLabel}。请勾选要导入的角色；红色项目会覆盖目标中的同 id 角色。</p>
    <div class="role-transfer-toolbar">
      <span id="role-transfer-summary"></span>
      <button type="button" class="ghost compact" onclick="setAllRoleTransferSelections(true)">全选</button>
      <button type="button" class="ghost compact" onclick="setAllRoleTransferSelections(false)">全不选</button>
    </div>
    <div class="role-transfer-list">${roleTransferRows(roleTransferState.roles, conflicts)}</div>
    <p class="muted">已有角色保留原排序和实时启停状态；新增角色按文件顺序追加并默认启用。</p>`,
    `<button class="action" onclick="importSelectedRoles()">导入所选</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
  updateRoleTransferSummary();
}

async function importSelectedRoles() {
  const state = roleTransferState;
  const indexes = selectedRoleTransferIndexes();
  if (!indexes.length) {
    uiAlert("请至少勾选一个要导入的角色。");
    return;
  }
  const roles = indexes.map(index => state.roles[index]);
  const overwrite_ids = roles.map(role => role.id).filter(id => state.conflicts.has(id));
  const endpoint = state.scope === "global" ? "/api/role-templates/import" : "/api/roles/import";
  const body = { roles, overwrite_ids };
  if (state.scope === "project") body.project_id = state.projectId;
  const result = await api("POST", endpoint, body);
  await loadOverview();
  fdlg.close();
  if (state.scope === "global") {
    renderGlobalRoleTable();
    await renderBackendTable();
  } else {
    renderRoleTable();
    renderSidebar();
    refreshProjectOrchestratorOptions();
  }
  const suffix = result.overwritten_ids.length
    ? `，覆盖 ${result.overwritten_ids.length} 个现有角色` : "";
  toast(`已导入 ${result.imported_ids.length} 个角色${suffix}`, "success");
}

function renderRoleTable() {
  const project = overview.projects.find(p => p.id === currentProject);
  const rows = projRoles().map(r => {   // 已按 sort_order 排好(服务端顺序)
    const enabled = r.enabled !== false;
    const isOrchestrator = r.id === project?.orchestrator_role_id;
    const disabledReason = roleDisabledReason(r);
    const exec = r.runtime_id
      ? `${esc(r.runtime_id)} / ${esc(r.model || "(CLI 默认)")}` +
        (r.effort ? ` / effort ${esc(r.effort)}` : "")
      : `<span class="muted">未绑定(请编辑角色选择 runtime)</span>`;
    return `<tr data-id="${esc(r.id)}">
      <td class="drag-handle" draggable="true" title="拖动排序"
          ondragstart="roleDragStart(event)" ondragend="roleDragEnd()">⠿</td>
      <td><span class="role-dot" style="background:${esc(r.color || "#888")};display:inline-block"></span>
          <b>@${esc(r.id)}</b> ${esc(r.name)}
          ${isOrchestrator ? `<span class="pill">主控</span>` : ""}
          ${r.usage_linkage_enabled ? `<span class="pill">用量联动</span>` : ""}
          ${enabled ? "" : `<span class="pill" title="${esc(disabledReason)}">${r.usage_auto_disabled ? "用量停用" : "停用"}</span>`}</td>
      <td class="muted">${esc(r.preference || "—")}</td>
      <td>${abilityPills(r) || "—"}</td>
      <td class="muted">${exec}</td>
      <td><span class="switch ${enabled ? "on" : ""}" role="switch"
          aria-checked="${enabled}" aria-disabled="${isOrchestrator && enabled}"
          title="${!enabled ? esc(disabledReason) : (isOrchestrator ? "项目主控不能直接停用，请先切换主控" :
            "已启用，点击临时停用；不会中断当前运行")}"
          onclick="toggleRoleEnabled(event,'${r.id}',${!enabled})"></span></td>
      <td><button class="ghost" onclick="editRole('${r.id}')">编辑</button></td></tr>`;
  }).join("");
  const table = document.getElementById("role-table");
  table.innerHTML =
    `<tr><th></th><th>角色</th><th>偏好</th><th>能力</th><th>Runtime / 模型</th><th>启用</th><th></th></tr>` + rows;
  table.ondragover = roleDragOver;              // 插入点判定放在表级,行随拖动实时移位
  table.ondrop = e => e.preventDefault();       // 阻止浏览器对放置数据的默认处理
}

async function toggleRoleEnabled(event, id, enabled) {
  event.stopPropagation();
  const project = overview.projects.find(item => item.id === currentProject);
  if (!enabled && project?.orchestrator_role_id === id) {
    uiAlert("项目主控不能直接停用，请先在项目信息中选择另一个已启用角色作为主控。");
    return;
  }
  await api("POST", `/api/roles/${encodeURIComponent(id)}/enabled`, {
    project_id: currentProject, enabled,
  });
  await loadOverview();
  renderRoleTable();
  refreshProjectOrchestratorOptions();
  toast(
    enabled ? `已启用 @${id}` : `已停用 @${id}；已在运行的任务不会中断`,
    "success",
  );
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
    runtime_id: "", model: "", effort: "", color: "#3564d7", enabled: true,
    usage_linkage_enabled: false };
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
      <div><label>标识色</label>${colorFieldHtml("rf-color", r.color)}</div>
    </div>
    <div class="row">
      <div><label>Runtime(定义角色时固定,必选)</label>
        <select id="rf-backend" onchange="window._editingRoleModel=null;window._editingRoleEffort=null;refreshModelOptions();refreshEffortOptions()">${backendOpts}</select></div>
      <div><label>模型(清单来自 runtime)</label>
        <select id="rf-model" onchange="refreshEffortOptions()"></select></div>
      <div><label>Effort(推理力度,仅部分 runtime 支持)</label>
        <select id="rf-effort"></select></div>
    </div>
    ${roleUsageLinkageField(r)}
    <label>角色定位/人格(给角色本人与主控看:写清"是谁、怎么工作"的专长画像;平台原样装配、不改写,任务由 @ 消息提供)</label>
    <textarea id="rf-desc" rows="3">${esc(r.description)}</textarea>
    <label>角色偏好(给主控选人看:何时该选它的领域/风格短标签,顿号分隔,如"前端"、"只审不改";名册中与能力并列展示)</label>
    <input type="text" id="rf-pref" value="${esc(r.preference || "")}">
    <label>角色能力(固定选项,同样给主控选人看;名册中与偏好并列展示为标签)</label>
    <div class="chips" id="rf-caps">${abilityChips}</div>`,
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

// 每个角色必须先选 runtime;模型下拉先列工具自带清单((CLI 默认) + 稳定别名),
// 再把 runtime 目录里剩下的带版本号型号归入「来自 runtime」,服务端缓存 10 分钟。
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
    } catch (e) {   // 查询失败:退回工具自带清单
      const b = overview.backends.find(x => x.id === bid);
      catalog = { configured: b?.models || [], discovered: [] };
    }
    if (document.getElementById("rf-backend")?.value !== bid) return;  // 期间已切换
  }
  // configured 是模型名数组;旧版接口返回过 {name,tier,cost},一并兼容
  const configured = (catalog.configured || []).map(
    m => String(typeof m === "string" ? m : (m?.name ?? "")));
  const configuredNames = new Set(configured);
  let opts = configured.map(name =>
    `<option value="${esc(name)}" ${cur === name ? "selected" : ""}>` +
    `${esc(name || "(CLI 默认)")}</option>`).join("");
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
  refreshEffortOptions();   // 档位可能按模型不同,模型清单到位后重算一次
}

// effort(推理力度)优先用工具按模型自报的档位(/api/backends/<id>/models 的
// efforts,与模型清单同一次探测),自报不了才回退到 adapter 级静态词表
// (/api/traits)。两者都没有就说明该 runtime 不支持,下拉禁用、保存为空。
function refreshEffortOptions() {
  const sel = document.getElementById("rf-effort");
  const bid = document.getElementById("rf-backend").value;
  const adapter = overview.backends.find(x => x.id === bid)?.adapter;
  const model = document.getElementById("rf-model")?.value || "";
  const perModel = modelCatalogCache[bid]?.efforts?.[model];
  const levels = perModel?.length ? perModel
    : ((adapter && traitMeta.effort_options?.[adapter]) || []);
  const cur = window._editingRoleEffort ?? sel.value;
  if (!levels.length) {
    // 还没渲染出可选项就不消费待选中值:模型清单是异步到的,这里可能只是
    // 早于目录的那一次渲染,消费掉会让角色已存的档位在第二次渲染时丢失。
    sel.innerHTML = `<option value="">${bid ? "(该 runtime 不支持)" : "先选择 runtime"}</option>`;
    sel.disabled = true;
    return;
  }
  window._editingRoleEffort = null;
  sel.disabled = false;
  let opts = `<option value="">(CLI 默认)</option>` + levels.map(l =>
    `<option value="${esc(l)}" ${cur === l ? "selected" : ""}>${esc(l)}</option>`).join("");
  // 换模型后旧档位可能不在新模型的清单里:保留并标注,让用户看见要改什么,
  // 而不是静默改掉他配过的值(保存时 API 会以同样口径拒绝)。
  if (cur && !levels.includes(cur))
    opts += `<option value="${esc(cur)}" selected>${esc(cur)}(当前值,该模型不支持)</option>`;
  sel.innerHTML = opts;
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
    usage_linkage_enabled: document.getElementById("rf-usage-linkage").classList.contains("on"),
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
