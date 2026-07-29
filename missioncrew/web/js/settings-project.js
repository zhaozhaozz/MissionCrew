/* ---------------- 项目设置(当前项目:信息 / 角色 / 频道 / 资源) ---------------- */

function projObj() { return overview.projects.find(project => project.id === currentProject); }

function projectOrchestratorOptions(project, selectedId = project.orchestrator_role_id) {
  return activeProjRoles().map(role =>
    `<option value="${esc(role.id)}" ${role.id === selectedId ? "selected" : ""}>` +
    `@${esc(role.id)} — ${esc(role.name)} · ` +
    `${esc(role.runtime_id)}/${esc(role.model || "CLI 默认")}</option>`
  ).join("");
}

function refreshProjectOrchestratorOptions() {
  const project = projObj();
  const select = document.getElementById("pf-orchestrator");
  if (!project || !select) return;
  const selected = activeProjRoles().some(role => role.id === select.value)
    ? select.value : project.orchestrator_role_id;
  select.innerHTML = projectOrchestratorOptions(project, selected);
}

async function renderProjSettings() {
  await ensureTraits();
  const label = `— 项目「${esc(currentProject || "无")}」`;
  document.getElementById("proj-label").textContent = label;
  document.getElementById("role-proj-label").textContent = label;
  document.getElementById("chan-proj-label").textContent = label;
  document.getElementById("res-proj-label").textContent = label;
  const project = projObj();
  const form = document.getElementById("proj-form");
  if (!project) {
    form.innerHTML = `<div class="empty">暂无项目,点击侧栏 ＋ 新建。</div>`;
    return;
  }
  const orchestratorOptions = projectOrchestratorOptions(project);
  form.innerHTML = `
    <div class="row">
      <div><label>项目 id</label><input type="text" value="${esc(project.id)}" disabled></div>
      <div><label>名称</label><input type="text" id="pf-name" value="${esc(project.name)}"></div>
    </div>
    <label>一句话描述</label><input type="text" id="pf-desc" value="${esc(project.description)}">
    <label>项目主控角色（唯一；其固定 Runtime / 模型负责项目与其他角色调度）</label>
    <select id="pf-orchestrator">${orchestratorOptions}</select>
    <label>单条协作链最大 Agent 执行次数（仅作失控兜底，不限制调度层级）</label>
    <input type="number" id="pf-max-chain-runs" min="1" step="1" value="${esc(project.max_chain_runs || 100)}">
    <label>项目章程（目标、范围、业务边界；完整规范请在“准则文档”全页管理）</label>
    <textarea id="pf-charter" rows="4">${esc(project.charter)}</textarea>
    <div class="form-actions">
      <button class="action" onclick="saveProject()">保存项目信息</button>
      <button class="ghost" onclick="switchTab('guidelines')">准则文档</button>
      <button class="ghost" onclick="switchTab('skills')">Skills</button>
      <button class="danger" onclick="deleteProject('${esc(project.id)}')">删除项目</button>
    </div>`;
  renderRoleTable();
  renderChanTable();
  renderResourceTable();
  loadDocFiles();
}

/* ---- 项目资源管理 ---- */
function renderResourceTable() {
  const resources = projObj()?.repos || [];
  const rows = resources.map(resource => `<tr>
    <td>${resource.kind === "git" ? "🔗 git 仓" : "📁 本地路径"}</td>
    <td><b>${esc(resource.name || resource.id)}</b></td>
    <td class="muted">${esc(resource.path || "—")}</td>
    <td class="muted">${esc(resource.remote || "—")}</td>
    <td>${resource.path ? `<button class="ghost" data-id="${esc(resource.id)}" title="重新探测 git 绑定与远程"
        onclick="refreshResource(this.dataset.id)">刷新</button>` : ""}
      <button class="danger" data-id="${esc(resource.id)}"
      onclick="deleteResource(this.dataset.id)">移除</button></td></tr>`).join("");
  document.getElementById("res-table").innerHTML =
    `<tr><th>类型</th><th>名称</th><th>本地路径</th><th>git 远程</th><th></th></tr>` +
    (rows || `<tr><td colspan="5" class="empty">暂无资源</td></tr>`);
}

async function addResourceFromForm() {
  const target = document.getElementById("res-target").value.trim();
  if (!target) { uiAlert("请输入本地路径或 git 地址"); return; }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/resources`, {
    target, name: document.getElementById("res-name").value.trim(),
  });
  fdlg.close();
  await loadOverview();
  renderResourceTable(); renderSidebar();
  toast("资源已添加", "success");
}

async function refreshResource(id) {
  const resource = await api("POST",
    `/api/projects/${encodeURIComponent(currentProject)}/resources/${encodeURIComponent(id)}/refresh`);
  await loadOverview();
  renderResourceTable(); renderSidebar();
  toast(resource.kind === "git"
    ? `已刷新:git 仓,远程 ${resource.remote || "(未配置)"}`
    : "已刷新:普通本地路径(未检测到 git 仓)", "success");
}

async function deleteResource(id) {
  if (!await uiConfirm(`将资源关联「${id}」移入项目回收站？不会删除磁盘内容。`)) return;
  await api("DELETE",
    `/api/projects/${encodeURIComponent(currentProject)}/resources/${encodeURIComponent(id)}`);
  await loadOverview();
  renderResourceTable(); renderSidebar();
  toast("资源关联已移入回收站", "success");
}

async function saveProject() {
  const body = {
    id: currentProject,
    name: document.getElementById("pf-name").value.trim(),
    description: document.getElementById("pf-desc").value.trim(),
    orchestrator_role_id: document.getElementById("pf-orchestrator").value,
    max_chain_runs: Number(document.getElementById("pf-max-chain-runs").value),
    charter: document.getElementById("pf-charter").value,
  };
  await api("POST", "/api/projects", body);
  await loadOverview(); renderProjSettings();
  toast("项目信息已保存", "success");
}

async function deleteProject(id) {
  if (!await uiConfirm(`删除项目「${id}」及其全部角色、频道与面板?消息、任务记录和归档后的文档历史会保留。`)) return;
  await api("DELETE", `/api/projects/${id}`);
  await loadOverview();
  renderProjSettings();
}
