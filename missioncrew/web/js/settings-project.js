/* ---------------- 项目设置(当前项目:信息 / 角色 / 频道) ---------------- */
function rulesToYaml(rules) {
  return (rules || []).map(r => {
    const lines = [`- match: ${JSON.stringify(r.match)}`];
    if (r.require_evidence?.length) lines.push(`  require_evidence: ${JSON.stringify(r.require_evidence)}`);
    if (r.require_gates?.length) lines.push(`  require_gates: ${JSON.stringify(r.require_gates)}`);
    if (r.require_capabilities?.length) lines.push(`  require_capabilities: ${JSON.stringify(r.require_capabilities)}`);
    if (r.note) lines.push(`  note: ${JSON.stringify(r.note)}`);
    return lines.join("\n");
  }).join("\n");
}

async function renderProjSettings() {
  await ensureTraits();
  const label = `— 项目「${esc(currentProject || "无")}」`;
  document.getElementById("proj-label").textContent = label;
  document.getElementById("role-proj-label").textContent = label;
  document.getElementById("chan-proj-label").textContent = label;
  const p = overview.projects.find(x => x.id === currentProject);
  const form = document.getElementById("proj-form");
  if (!p) { form.innerHTML = `<div class="empty">暂无项目,点击侧栏 ＋ 新建。</div>`; return; }
  const orchestratorOptions = projRoles().map(r =>
    `<option value="${esc(r.id)}" ${r.id === p.orchestrator_role_id ? "selected" : ""}>@${esc(r.id)} — ${esc(r.name)} · ${esc(r.runtime_id)}/${esc(r.model || "CLI 默认")}</option>`
  ).join("");
  form.innerHTML = `
    <div class="row">
      <div><label>项目 id</label><input type="text" value="${esc(p.id)}" disabled></div>
      <div><label>名称</label><input type="text" id="pf-name" value="${esc(p.name)}"></div>
    </div>
    <label>一句话描述</label><input type="text" id="pf-desc" value="${esc(p.description)}">
    <label>项目主控角色（唯一；其固定 Runtime / 模型负责项目与其他角色调度）</label>
    <select id="pf-orchestrator">${orchestratorOptions}</select>
    <label>项目章程(目标、范围、业务边界;开发/测试/部署等完整规范写成下方"准则文档")</label>
    <textarea id="pf-charter" rows="4">${esc(p.charter)}</textarea>
    <label>验证准则(YAML 列表:match / require_evidence / require_gates / require_capabilities / note)</label>
    <textarea id="pf-rules" rows="8" spellcheck="false">${esc(rulesToYaml(p.rules))}</textarea>
    <div class="form-actions">
      <button class="action" onclick="saveProject()">保存项目信息</button>
      <button class="danger" onclick="deleteProject('${esc(p.id)}')">删除项目</button>
    </div>`;
  document.getElementById("guide-proj-label").textContent = label;
  document.getElementById("skill-proj-label").textContent = label;
  document.getElementById("res-proj-label").textContent = label;
  renderRoleTable(); renderChanTable();
  renderResourceTable();
  renderGuidelines(); renderSkills();
  loadDocFiles();   // 文档库页独立成侧栏入口,这里只取引用选择器的数据
}

/* ---- 项目资源管理 ---- */
function renderResourceTable() {
  const res = projObj()?.repos || [];
  const rows = res.map(r => `<tr>
    <td>${r.kind === "git" ? "🔗 git 仓" : "📁 本地路径"}</td>
    <td><b>${esc(r.name || r.id)}</b></td>
    <td class="muted">${esc(r.path || "—")}</td>
    <td class="muted">${esc(r.remote || "—")}</td>
    <td>${r.path ? `<button class="ghost" data-id="${esc(r.id)}" title="重新探测 git 绑定与远程"
        onclick="refreshResource(this.dataset.id)">刷新</button>` : ""}
      <button class="danger" data-id="${esc(r.id)}"
      onclick="deleteResource(this.dataset.id)">移除</button></td></tr>`).join("");
  document.getElementById("res-table").innerHTML =
    `<tr><th>类型</th><th>名称</th><th>本地路径</th><th>git 远程</th><th></th></tr>` +
    (rows || `<tr><td colspan="5" class="empty">暂无资源</td></tr>`);
}

async function addResourceFromForm() {
  const target = document.getElementById("res-target").value.trim();
  if (!target) { uiAlert("请输入本地路径或 git 地址"); return; }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/resources`,
            { target, name: document.getElementById("res-name").value.trim() });
  fdlg.close();
  await loadOverview();
  renderResourceTable(); renderSidebar();
  toast("资源已添加", "success");
}

async function refreshResource(id) {
  const r = await api("POST",
    `/api/projects/${encodeURIComponent(currentProject)}/resources/${encodeURIComponent(id)}/refresh`);
  await loadOverview();
  renderResourceTable(); renderSidebar();
  toast(r.kind === "git"
    ? `已刷新:git 仓,远程 ${r.remote || "(未配置)"}`
    : "已刷新:普通本地路径(未检测到 git 仓)", "success");
}

async function deleteResource(id) {
  if (!await uiConfirm(`移除资源「${id}」?(只解除关联,不删除磁盘内容)`)) return;
  await api("DELETE",
    `/api/projects/${encodeURIComponent(currentProject)}/resources/${encodeURIComponent(id)}`);
  await loadOverview();
  renderResourceTable(); renderSidebar();
  toast("资源已移除", "success");
}

/* ---- 准则文档管理(按篇 CRUD,走专用端点) ---- */
function projObj() { return overview.projects.find(x => x.id === currentProject); }

function fileRefPicker(elemId, selected) {
  const sel = new Set(selected || []);
  const opts = [...new Set([...docFiles, ...sel])].map(f =>
    `<option value="${esc(f)}" ${sel.has(f) ? "selected" : ""}>${esc(f)}</option>`).join("");
  return `<label>引用文档库文件(多选;内容会注入执行上下文)</label>
    <select id="${elemId}" multiple size="5">${opts || ""}</select>
    <label>额外引用路径(逗号分隔,可引用尚未创建的文件)</label>
    <input type="text" id="${elemId}-extra" placeholder="specs/deploy.md, guides/style.md">`;
}

function readFileRefs(elemId) {
  const picked = [...document.getElementById(elemId).selectedOptions].map(o => o.value);
  const extra = document.getElementById(`${elemId}-extra`).value
    .split(",").map(s => s.trim()).filter(Boolean);
  return [...new Set([...picked, ...extra])];
}

function renderGuidelines() {
  const p = projObj();
  if (!p) return;
  const rows = (p.guidelines || []).map(g => `<tr>
    <td><b>${esc(g.title || g.id)}</b><br><span class="muted">${esc(g.id)}</span></td>
    <td class="muted">${(g.content || "").length} 字</td>
    <td>${(g.file_refs || []).map(f => `<span class="pill">${esc(f)}</span>`).join("") || "—"}</td>
    <td>${g.enabled === false ? `<span class="muted">已停用</span>` : `<span style="color:var(--ok)">启用</span>`}</td>
    <td><button class="ghost" onclick="editGuideline('${esc(g.id)}')">编辑</button>
        <button class="danger" onclick="deleteGuideline('${esc(g.id)}')">删除</button></td></tr>`).join("");
  document.getElementById("guide-table").innerHTML =
    `<tr><th>篇目</th><th>正文</th><th>文件引用</th><th>状态</th><th></th></tr>` +
    (rows || `<tr><td colspan="5" class="empty">暂无准则文档;开发规范、测试规范、部署说明各写一篇。</td></tr>`);
}

function editGuideline(id) {
  const g = (projObj()?.guidelines || []).find(x => x.id === id) ||
    { id: "", title: "", content: "", file_refs: [], enabled: true };
  openFormDialog(id ? `编辑准则文档 ${id}` : "新建准则文档", `
    <div class="row">
      <div><label>文档 id</label><input type="text" id="gf-id" value="${esc(g.id)}" ${id ? "disabled" : ""} placeholder="如 dev-spec / test-spec / deploy"></div>
      <div><label>标题</label><input type="text" id="gf-title" value="${esc(g.title || "")}" placeholder="如 开发规范"></div>
      <div><label style="display:block">状态</label>
        <span class="switch ${g.enabled !== false ? "on" : ""}" id="gf-enabled" role="switch"
          onclick="this.classList.toggle('on')"></span></div>
    </div>
    <label>正文(Markdown 章节;会作为完整文档注入执行上下文)</label>
    <textarea id="gf-content" rows="10" style="height:auto">${esc(g.content || "")}</textarea>
    ${fileRefPicker("gf-refs", g.file_refs)}`,
    `<button class="action" onclick="saveGuideline()">保存</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
}

async function saveGuideline() {
  const id = document.getElementById("gf-id").value.trim();
  if (!id) { uiAlert("文档 id 不能为空"); return; }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/guidelines`, {
    id, title: document.getElementById("gf-title").value.trim(),
    content: document.getElementById("gf-content").value,
    file_refs: readFileRefs("gf-refs"),
    enabled: document.getElementById("gf-enabled").classList.contains("on"),
  });
  fdlg.close();
  await loadOverview(); renderGuidelines(); renderSidebar();
  toast("准则文档已保存", "success");
}

async function deleteGuideline(id) {
  if (!await uiConfirm(`删除准则文档「${id}」?`)) return;
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/guidelines/${encodeURIComponent(id)}`);
  await loadOverview(); renderGuidelines();
  toast("准则文档已删除", "success");
}

/* ---- Skill 管理(按 Runtime 注入) ---- */
let skillRI = [];   // 编辑中的按 Runtime 覆盖说明 [{key, text}]

function skillScopeLabel(s) {
  const scope = [...(s.runtime_ids || []), ...(s.adapters || [])];
  return scope.length ? scope.map(x => `<span class="pill">${esc(x)}</span>`).join("")
                      : `<span class="muted">所有 Runtime</span>`;
}

function renderSkills() {
  const p = projObj();
  if (!p) return;
  const rows = (p.skills || []).map(s => `<tr>
    <td><b>${esc(s.name || s.id)}</b><br><span class="muted">${esc(s.id)}</span></td>
    <td>${skillScopeLabel(s)}</td>
    <td>${(s.file_refs || []).map(f => `<span class="pill">${esc(f)}</span>`).join("") || "—"}</td>
    <td class="muted">${Object.keys(s.runtime_instructions || {}).length ? "有覆盖" : "—"}</td>
    <td>${s.enabled === false ? `<span class="muted">已停用</span>` : `<span style="color:var(--ok)">启用</span>`}</td>
    <td><button class="ghost" onclick="editSkill('${esc(s.id)}')">编辑</button>
        <button class="danger" onclick="deleteSkill('${esc(s.id)}')">删除</button></td></tr>`).join("");
  document.getElementById("skill-table").innerHTML =
    `<tr><th>Skill</th><th>适用 Runtime</th><th>文件引用</th><th>Runtime 覆盖</th><th>状态</th><th></th></tr>` +
    (rows || `<tr><td colspan="6" class="empty">暂无 Skill</td></tr>`);
}

function riRows() {
  const keys = [...new Set([...overview.backends.map(b => b.id),
                            ...overview.backends.map(b => b.adapter), "default"])];
  return skillRI.map((r, i) => `<div class="row" style="margin-top:6px">
      <div style="flex:0 0 200px"><select onchange="skillRI[${i}].key=this.value">
        ${keys.map(k => `<option value="${esc(k)}" ${r.key === k ? "selected" : ""}>${esc(k)}</option>`).join("")}
      </select></div>
      <div><input type="text" value="${esc(r.text)}" oninput="skillRI[${i}].text=this.value"
        placeholder="该 Runtime 的补充/覆盖说明"></div>
      <div style="flex:0 0 60px"><button class="ghost" onclick="skillRI.splice(${i},1);refreshRI()">移除</button></div>
    </div>`).join("");
}

function refreshRI() { document.getElementById("sf-ri").innerHTML = riRows(); }

function editSkill(id) {
  const s = (projObj()?.skills || []).find(x => x.id === id) ||
    { id: "", name: "", description: "", instructions: "", file_refs: [],
      runtime_ids: [], adapters: [], runtime_instructions: {}, enabled: true };
  skillRI = Object.entries(s.runtime_instructions || {}).map(([key, text]) => ({ key, text }));
  const rtChips = overview.backends.map(b =>
    `<span class="chip ${(s.runtime_ids || []).includes(b.id) ? "on" : ""}" data-rt="${esc(b.id)}"
       onclick="this.classList.toggle('on')">${esc(b.id)}</span>`).join("");
  openFormDialog(id ? `编辑 Skill ${id}` : "新建 Skill", `
    <div class="row">
      <div><label>Skill id</label><input type="text" id="sf-id" value="${esc(s.id)}" ${id ? "disabled" : ""}></div>
      <div><label>名称</label><input type="text" id="sf-name" value="${esc(s.name || "")}"></div>
      <div><label style="display:block">状态</label>
        <span class="switch ${s.enabled !== false ? "on" : ""}" id="sf-enabled" role="switch"
          onclick="this.classList.toggle('on')"></span></div>
    </div>
    <label>一句话描述</label><input type="text" id="sf-desc" value="${esc(s.description || "")}">
    <label>完整说明(instructions;注入执行上下文的主体)</label>
    <textarea id="sf-inst" rows="8" style="height:auto">${esc(s.instructions || "")}</textarea>
    ${fileRefPicker("sf-refs", s.file_refs)}
    <label>适用 Runtime(不选 = 注入所有 Runtime)</label>
    <div class="chips" id="sf-rts">${rtChips}</div>
    <label>按 Runtime 覆盖说明(key 可为 runtime id / adapter / default)</label>
    <div id="sf-ri">${riRows()}</div>
    <button class="ghost" style="margin-top:6px" onclick="skillRI.push({key:'default',text:''});refreshRI()">+ 添加覆盖</button>`,
    `<button class="action" onclick="saveSkill()">保存</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
}

async function saveSkill() {
  const id = document.getElementById("sf-id").value.trim();
  if (!id) { uiAlert("Skill id 不能为空"); return; }
  const runtime_instructions = {};
  for (const r of skillRI) if (r.key && r.text.trim()) runtime_instructions[r.key] = r.text;
  const existing = (projObj()?.skills || []).find(x => x.id === id);
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/skills`, {
    id, name: document.getElementById("sf-name").value.trim(),
    description: document.getElementById("sf-desc").value.trim(),
    instructions: document.getElementById("sf-inst").value,
    file_refs: readFileRefs("sf-refs"),
    runtime_ids: [...document.querySelectorAll("#sf-rts .chip.on")].map(c => c.dataset.rt),
    adapters: existing?.adapters || [],   // adapter 维度暂由 YAML/API 配置
    runtime_instructions,
    enabled: document.getElementById("sf-enabled").classList.contains("on"),
  });
  fdlg.close();
  await loadOverview(); renderSkills(); renderSidebar();
  toast("Skill 已保存", "success");
}

async function deleteSkill(id) {
  if (!await uiConfirm(`删除 Skill「${id}」?`)) return;
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/skills/${encodeURIComponent(id)}`);
  await loadOverview(); renderSkills();
  toast("Skill 已删除", "success");
}

async function saveProject() {
  const body = {   // 资源/准则文档/Skills 走专用管理器,此处缺省即保留
    id: currentProject,
    name: document.getElementById("pf-name").value.trim(),
    description: document.getElementById("pf-desc").value.trim(),
    orchestrator_role_id: document.getElementById("pf-orchestrator").value,
    charter: document.getElementById("pf-charter").value,
    rules_yaml: document.getElementById("pf-rules").value,
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

