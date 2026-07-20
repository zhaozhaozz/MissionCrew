/* ---- 项目准则 / Skills / 验证规则全页管理与主控生成入口 ---- */
let selectedGuidelineId;
let selectedSkillId;
let selectedRuleIndex;
let skillRuntimeInstructions = [];
const configEditorDirty = { guidelines: false, skills: false, rules: false };

function markConfigDirty(kind) {
  configEditorDirty[kind] = true;
}

function projectConfigLabel(id) {
  const element = document.getElementById(id);
  if (element) element.textContent = `— 项目「${currentProject || "无"}」`;
}

function fileRefPicker(id, selected = [], kind) {
  const values = [...new Set([...(selected || []), ...docFiles])];
  return `<select id="${id}" multiple size="${Math.min(8, Math.max(3, values.length || 3))}"
      onchange="markConfigDirty('${kind}')">
    ${values.map(path => `<option value="${esc(path)}" ${selected.includes(path) ? "selected" : ""}>${esc(path)}</option>`).join("")}
  </select><div class="muted">可多选项目版本化文档。</div>
  <input id="${id}-extra" placeholder="额外相对路径，逗号分隔，例如 specs/deploy.md"
    oninput="markConfigDirty('${kind}')">`;
}

function readFileRefs(id) {
  const selected = [...document.getElementById(id).selectedOptions].map(option => option.value);
  const extra = document.getElementById(`${id}-extra`).value
    .split(",").map(path => path.trim()).filter(Boolean);
  return [...new Set([...selected, ...extra])];
}

function configListItem(label, detail, selected, onclick) {
  return `<div class="config-list-item ${selected ? "selected" : ""}" onclick="${onclick}">
    <b>${esc(label)}</b><span class="muted">${esc(detail)}</span></div>`;
}

function renderProjectConfigPage(tab, force = false) {
  if (tab === "guidelines") renderGuidelinesPage(force);
  if (tab === "skills") renderSkillsPage(force);
  if (tab === "rules") renderRulesPage(force);
}

async function requestProjectConfig(kind) {
  const project = projObj();
  const input = document.getElementById(`config-request-${kind}`);
  const status = document.getElementById(`config-request-${kind}-status`);
  const request = input?.value.trim();
  if (!project || !request) {
    if (status) status.textContent = project ? "请先描述要生成或修改的内容。" : "请先选择项目。";
    return;
  }
  const channel = projChannels().find(item => item.id === `${project.id}:general` || item.id === "general")
    || projChannels()[0];
  if (!channel) {
    status.textContent = "当前项目没有频道，无法把请求交给主控。";
    return;
  }
  const targets = {
    guidelines: { label: "准则文档", action: "save_guideline" },
    skills: { label: "Skill", action: "save_skill" },
    rules: { label: "验证规则", action: "save_rule" },
    documents: { label: "版本化文档", action: "write_document" },
  };
  const target = targets[kind];
  if (!target) return;
  input.value = "";
  status.textContent = `正在把${target.label}请求交给 @${project.orchestrator_role_id}…`;
  const content = `@${project.orchestrator_role_id} 项目${target.label}生成请求：\n${request}\n\n` +
    `请结合当前项目配置完成请求，并必须使用 missioncrew control action ` +
    `${target.action} 实际保存结果。修改现有条目时沿用它的 id、match 或路径；` +
    `若请求包含多个独立条目，可使用多个对应 action。不要只回复示例文本。`;
  try {
    await api("POST", `/api/chat/${encodeURIComponent(channel.id)}/messages`, {
      author: "human", content,
    });
    status.textContent = `已交给 @${project.orchestrator_role_id}；执行过程和结果可在 #${channel.name || channel.id} 查看。`;
    if (currentChan === channel.id) pollMessages();
  } catch (error) {
    input.value = request;
    status.textContent = "发送失败，请重试。";
  }
}

/* ---- 准则文档 ---- */
function renderGuidelinesPage(force = false) {
  projectConfigLabel("guide-proj-label");
  const guidelines = projObj()?.guidelines || [];
  if (selectedGuidelineId === undefined
      || (selectedGuidelineId !== null && !guidelines.some(item => item.id === selectedGuidelineId)))
    selectedGuidelineId = guidelines[0]?.id ?? null;
  document.getElementById("guideline-page-list").innerHTML = guidelines.map(item =>
    configListItem(item.title || item.id,
      `${item.id}${item.enabled === false ? " · 已停用" : ""}`,
      item.id === selectedGuidelineId,
      `editGuideline('${esc(item.id)}')`)).join("")
    || `<div class="empty">暂无准则文档。</div>`;
  if (force || !configEditorDirty.guidelines) renderGuidelineEditor();
}

function renderGuidelineEditor() {
  const guideline = (projObj()?.guidelines || []).find(item => item.id === selectedGuidelineId);
  document.getElementById("guideline-editor").innerHTML = `
    <h3>${guideline ? "编辑准则文档" : "新建准则文档"}</h3>
    <label>id（保存后不可修改）</label>
    <input id="gf-id" value="${esc(guideline?.id || "")}" ${guideline ? "disabled" : ""}
      placeholder="例如 development" oninput="markConfigDirty('guidelines')">
    <label>标题</label><input id="gf-title" value="${esc(guideline?.title || "")}"
      oninput="markConfigDirty('guidelines')">
    <label>正文（Markdown）</label><textarea id="gf-content" rows="18"
      oninput="markConfigDirty('guidelines')">${esc(guideline?.content || "")}</textarea>
    <label>引用项目文档</label>${fileRefPicker("gf-refs", guideline?.file_refs || [], "guidelines")}
    <label>启用</label><span class="switch ${guideline?.enabled === false ? "" : "on"}" id="gf-enabled"
      role="switch" onclick="this.classList.toggle('on');markConfigDirty('guidelines')"></span>
    <div class="form-actions"><button class="action" onclick="saveGuideline()">保存</button>
      ${guideline ? `<button class="danger" onclick="deleteGuideline('${esc(guideline.id)}')">删除</button>` : ""}</div>`;
}

function editGuideline(id) {
  selectedGuidelineId = id;
  configEditorDirty.guidelines = false;
  if (currentTab !== "guidelines") switchTab("guidelines");
  else renderGuidelinesPage(true);
}

async function saveGuideline() {
  const id = document.getElementById("gf-id").value.trim();
  if (!id) { uiAlert("请输入准则 id"); return; }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/guidelines`, {
    id,
    title: document.getElementById("gf-title").value.trim(),
    content: document.getElementById("gf-content").value,
    file_refs: readFileRefs("gf-refs"),
    enabled: document.getElementById("gf-enabled").classList.contains("on"),
  });
  selectedGuidelineId = id;
  configEditorDirty.guidelines = false;
  await loadOverview();
  renderGuidelinesPage(true);
  toast("准则文档已保存", "success");
}

async function deleteGuideline(id) {
  if (!await uiConfirm(`删除准则文档「${id}」？`)) return;
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/guidelines/${encodeURIComponent(id)}`);
  selectedGuidelineId = undefined;
  configEditorDirty.guidelines = false;
  await loadOverview();
  renderGuidelinesPage(true);
  toast("准则文档已删除", "success");
}

/* ---- Skills ---- */
function renderSkillsPage(force = false) {
  projectConfigLabel("skill-proj-label");
  const skills = projObj()?.skills || [];
  if (selectedSkillId === undefined
      || (selectedSkillId !== null && !skills.some(item => item.id === selectedSkillId)))
    selectedSkillId = skills[0]?.id ?? null;
  document.getElementById("skill-page-list").innerHTML = skills.map(item =>
    configListItem(item.name || item.id,
      `${item.id}${item.enabled === false ? " · 已停用" : ""}`,
      item.id === selectedSkillId,
      `editSkill('${esc(item.id)}')`)).join("")
    || `<div class="empty">暂无 Skill。</div>`;
  if (force || !configEditorDirty.skills) renderSkillEditor();
}

function renderSkillEditor() {
  const skill = (projObj()?.skills || []).find(item => item.id === selectedSkillId);
  skillRuntimeInstructions = Object.entries(skill?.runtime_instructions || {});
  const runtimeOptions = (overview.backends || []).map(runtime =>
    `<label class="chip ${skill?.runtime_ids?.includes(runtime.id) ? "on" : ""}" data-runtime="${esc(runtime.id)}"
      onclick="this.classList.toggle('on');markConfigDirty('skills')">${esc(runtime.name || runtime.id)}</label>`).join("");
  document.getElementById("skill-editor").innerHTML = `
    <h3>${skill ? "编辑 Skill" : "新建 Skill"}</h3>
    <label>id（保存后不可修改）</label>
    <input id="sf-id" value="${esc(skill?.id || "")}" ${skill ? "disabled" : ""}
      placeholder="例如 local-ci" oninput="markConfigDirty('skills')">
    <label>名称</label><input id="sf-name" value="${esc(skill?.name || "")}"
      oninput="markConfigDirty('skills')">
    <label>简介</label><input id="sf-desc" value="${esc(skill?.description || "")}"
      oninput="markConfigDirty('skills')">
    <label>完整执行说明（Markdown）</label><textarea id="sf-instructions" rows="16"
      oninput="markConfigDirty('skills')">${esc(skill?.instructions || "")}</textarea>
    <label>引用项目文档</label>${fileRefPicker("sf-refs", skill?.file_refs || [], "skills")}
    <label>适用 Runtime（全部不选 = 所有 Runtime）</label><div class="chips" id="sf-runtimes">${runtimeOptions}</div>
    <label>适用适配器（逗号分隔，可选）</label><input id="sf-adapters"
      value="${esc((skill?.adapters || []).join(", "))}" placeholder="claude_code, codex"
      oninput="markConfigDirty('skills')">
    <label>Runtime 专用补充说明</label><div id="sf-ri"></div>
    <button class="ghost" type="button" onclick="addSkillRuntimeInstruction()">＋ 添加覆盖</button>
    <label>启用</label><span class="switch ${skill?.enabled === false ? "" : "on"}" id="sf-enabled"
      role="switch" onclick="this.classList.toggle('on');markConfigDirty('skills')"></span>
    <div class="form-actions"><button class="action" onclick="saveSkill()">保存</button>
      ${skill ? `<button class="danger" onclick="deleteSkill('${esc(skill.id)}')">删除</button>` : ""}</div>`;
  renderSkillRuntimeInstructions();
}

function renderSkillRuntimeInstructions() {
  const root = document.getElementById("sf-ri");
  if (!root) return;
  root.innerHTML = skillRuntimeInstructions.map(([key, value], index) => `<div class="row" style="margin-bottom:6px">
    <div style="flex:0 0 180px"><input value="${esc(key)}" placeholder="Runtime id / adapter / default"
      oninput="skillRuntimeInstructions[${index}][0]=this.value;markConfigDirty('skills')"></div>
    <div><input value="${esc(value)}" placeholder="专用补充说明"
      oninput="skillRuntimeInstructions[${index}][1]=this.value;markConfigDirty('skills')"></div>
    <button class="danger" type="button" onclick="removeSkillRuntimeInstruction(${index})">删除</button></div>`).join("")
    || `<div class="muted">暂无专用覆盖。</div>`;
}

function addSkillRuntimeInstruction() {
  skillRuntimeInstructions.push(["", ""]);
  markConfigDirty("skills");
  renderSkillRuntimeInstructions();
}

function removeSkillRuntimeInstruction(index) {
  skillRuntimeInstructions.splice(index, 1);
  markConfigDirty("skills");
  renderSkillRuntimeInstructions();
}

function editSkill(id) {
  selectedSkillId = id;
  configEditorDirty.skills = false;
  if (currentTab !== "skills") switchTab("skills");
  else renderSkillsPage(true);
}

async function saveSkill() {
  const id = document.getElementById("sf-id").value.trim();
  if (!id) { uiAlert("请输入 Skill id"); return; }
  const runtimeInstructions = {};
  for (const [rawKey, value] of skillRuntimeInstructions) {
    const key = rawKey.trim();
    if (key) runtimeInstructions[key] = value;
  }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/skills`, {
    id,
    name: document.getElementById("sf-name").value.trim(),
    description: document.getElementById("sf-desc").value.trim(),
    instructions: document.getElementById("sf-instructions").value,
    file_refs: readFileRefs("sf-refs"),
    runtime_ids: [...document.querySelectorAll("#sf-runtimes .chip.on")].map(item => item.dataset.runtime),
    adapters: document.getElementById("sf-adapters").value.split(",").map(item => item.trim()).filter(Boolean),
    runtime_instructions: runtimeInstructions,
    enabled: document.getElementById("sf-enabled").classList.contains("on"),
  });
  selectedSkillId = id;
  configEditorDirty.skills = false;
  await loadOverview();
  renderSkillsPage(true);
  toast("Skill 已保存", "success");
}

async function deleteSkill(id) {
  if (!await uiConfirm(`删除 Skill「${id}」？`)) return;
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/skills/${encodeURIComponent(id)}`);
  selectedSkillId = undefined;
  configEditorDirty.skills = false;
  await loadOverview();
  renderSkillsPage(true);
  toast("Skill 已删除", "success");
}

/* ---- 验证规则 ---- */
function renderRulesPage(force = false) {
  projectConfigLabel("rule-proj-label");
  const rules = projObj()?.rules || [];
  if (selectedRuleIndex === undefined
      || (selectedRuleIndex !== null && !rules[selectedRuleIndex]))
    selectedRuleIndex = rules.length ? 0 : null;
  document.getElementById("rule-page-list").innerHTML = rules.map((rule, index) =>
    configListItem(rule.note || `规则 ${index + 1}`, JSON.stringify(rule.match),
      index === selectedRuleIndex, `editRule(${index})`)).join("")
    || `<div class="empty">暂无验证规则。</div>`;
  if (force || !configEditorDirty.rules) renderRuleEditor();
}

function renderRuleEditor() {
  const rule = selectedRuleIndex === null ? null : (projObj()?.rules || [])[selectedRuleIndex];
  document.getElementById("rule-editor").innerHTML = `
    <h3>${rule ? "编辑验证规则" : "新建验证规则"}</h3>
    <label>匹配条件（JSON 对象）</label><textarea id="rf-match" rows="5"
      placeholder='{"task_type":"bug","labels":["auth"],"risk":["high"]}'
      oninput="markConfigDirty('rules')">${esc(JSON.stringify(rule?.match || {}, null, 2))}</textarea>
    <div class="muted">支持 task_type、labels（任一标签命中）和 risk。</div>
    <label>所需证据（逗号分隔）</label><input id="rf-evidence"
      value="${esc((rule?.require_evidence || []).join(", "))}" placeholder="reproduction, regression_test"
      oninput="markConfigDirty('rules')">
    <label>所需门禁（逗号分隔）</label><input id="rf-gates"
      value="${esc((rule?.require_gates || []).join(", "))}" placeholder="security_review, human_approval"
      oninput="markConfigDirty('rules')">
    <label>验证角色所需能力（逗号分隔）</label><input id="rf-capabilities"
      value="${esc((rule?.require_capabilities || []).join(", "))}" placeholder="security, review"
      oninput="markConfigDirty('rules')">
    <label>说明</label><textarea id="rf-note" rows="4"
      oninput="markConfigDirty('rules')">${esc(rule?.note || "")}</textarea>
    <div class="form-actions"><button class="action" onclick="saveRule()">保存</button>
      ${rule ? `<button class="danger" onclick="deleteRule(${selectedRuleIndex})">删除</button>` : ""}</div>`;
}

function editRule(index) {
  selectedRuleIndex = index;
  configEditorDirty.rules = false;
  if (currentTab !== "rules") switchTab("rules");
  else renderRulesPage(true);
}

function commaList(id) {
  return document.getElementById(id).value.split(",").map(item => item.trim()).filter(Boolean);
}

async function saveProjectRules(rules) {
  const project = projObj();
  await api("POST", "/api/projects", {
    id: project.id,
    name: project.name,
    description: project.description,
    charter: project.charter,
    orchestrator_role_id: project.orchestrator_role_id,
    max_chain_runs: project.max_chain_runs,
    rules_yaml: rulesToYaml(rules),
  });
}

async function saveRule() {
  let match;
  try {
    match = JSON.parse(document.getElementById("rf-match").value || "{}");
    if (!match || Array.isArray(match) || typeof match !== "object") throw new Error();
  } catch (error) {
    uiAlert("匹配条件必须是 JSON 对象");
    return;
  }
  const rules = [...(projObj()?.rules || [])];
  const rule = {
    match,
    require_evidence: commaList("rf-evidence"),
    require_gates: commaList("rf-gates"),
    require_capabilities: commaList("rf-capabilities"),
    note: document.getElementById("rf-note").value.trim(),
  };
  if (selectedRuleIndex === null) {
    rules.push(rule);
    selectedRuleIndex = rules.length - 1;
  } else {
    rules[selectedRuleIndex] = rule;
  }
  await saveProjectRules(rules);
  configEditorDirty.rules = false;
  await loadOverview();
  renderRulesPage(true);
  toast("验证规则已保存", "success");
}

async function deleteRule(index) {
  if (!await uiConfirm(`删除验证规则 ${index + 1}？`)) return;
  const rules = [...(projObj()?.rules || [])];
  rules.splice(index, 1);
  await saveProjectRules(rules);
  selectedRuleIndex = undefined;
  configEditorDirty.rules = false;
  await loadOverview();
  renderRulesPage(true);
  toast("验证规则已删除", "success");
}
