/* ---- 项目准则 / Skills / 验证规则全页管理与底部主控对话 ---- */
let selectedGuidelineId;
let selectedSkillId;
let selectedRuleIndex;
const configEditorDirty = { guidelines: false, skills: false, rules: false };
const CONFIG_CHAT_TABS = new Set(["guidelines", "skills", "rules", "docs"]);
const CONFIG_CHAT_TARGETS = {
  guidelines: { label: "准则文档", action: "save_guideline" },
  skills: { label: "Skill", action: "save_skill" },
  rules: { label: "验证规则", action: "save_rule" },
  docs: { label: "版本化文档", action: "write_document" },
};
const CONFIG_FIELD_LABELS = {
  "gf-id": "准则 id", "gf-title": "标题", "gf-content": "正文",
  "sf-id": "Skill id", "sf-name": "名称", "sf-desc": "简介",
  "sf-instructions": "完整执行说明",
  "rf-match": "匹配条件", "rf-evidence": "所需证据", "rf-gates": "所需门禁",
  "rf-capabilities": "验证能力", "rf-note": "说明",
  "doc-new-path": "文档路径", "doc-content": "文档正文",
};
let configChatSelection = null;
let configChatPolling = false;
const configChatThreads = new Map();

function markConfigDirty(kind) {
  configEditorDirty[kind] = true;
  updateConfigChatContext();
}

function projectConfigLabel(id) {
  const element = document.getElementById(id);
  if (element) element.textContent = `— 项目「${currentProject || "无"}」`;
}

function renderProjectConfigPage(tab, force = false) {
  if (tab === "guidelines") renderGuidelinesPage(force);
  if (tab === "skills") renderSkillsPage(force);
  if (tab === "rules") renderRulesPage(force);
}

function configChatContext() {
  const project = projObj();
  const target = CONFIG_CHAT_TARGETS[currentTab];
  if (!project || !CONFIG_CHAT_TABS.has(currentTab) || !target) return null;
  let item = "未选择条目";
  let itemKey = "none";
  if (currentTab === "guidelines") {
    const guideline = project.guidelines?.find(value => value.id === selectedGuidelineId);
    const draftTitle = valueOf("gf-title").trim();
    const draftId = valueOf("gf-id").trim();
    item = guideline ? `${draftTitle || guideline.title || guideline.id}（id: ${guideline.id}）`
                     : `新建准则（${draftTitle || draftId || "未命名"}，未保存）`;
    itemKey = guideline?.id || "new";
  } else if (currentTab === "skills") {
    const skill = project.skills?.find(value => value.id === selectedSkillId);
    const draftName = valueOf("sf-name").trim();
    const draftId = valueOf("sf-id").trim();
    item = skill ? `${draftName || skill.name || skill.id}（id: ${skill.id}）`
                 : `新建 Skill（${draftName || draftId || "未命名"}，未保存）`;
    itemKey = skill?.id || "new";
  } else if (currentTab === "rules") {
    const rule = selectedRuleIndex === null ? null : project.rules?.[selectedRuleIndex];
    const draftMatch = valueOf("rf-match").replace(/\s+/g, " ").trim().slice(0, 200);
    item = rule ? `规则 ${selectedRuleIndex + 1}（match: ${draftMatch || JSON.stringify(rule.match)}）`
                : "新建验证规则（未保存）";
    itemKey = rule ? `index-${selectedRuleIndex}` : "new";
  } else if (currentTab === "docs") {
    const draftPath = document.getElementById("doc-new-path")?.value.trim();
    item = docMode === "new" ? `新建文档（${draftPath || "路径未填写"}）`
                             : (docSelected || "未选择文档");
    itemKey = docMode === "new" ? "new" : (docSelected || "none");
  }
  return {
    ...target, tab: currentTab, item,
    key: `${project.id}:${currentTab}:${itemKey}`,
  };
}

function valueOf(id) {
  return document.getElementById(id)?.value ?? "";
}

function clippedDraftText(value) {
  const clipped = clippedConfigText(value);
  return clipped.truncated ? `${clipped.text}\n…（草稿过长，已截断）` : clipped.text;
}

function currentConfigDraft(context) {
  if (context.tab === "guidelines") return {
    id: valueOf("gf-id"), title: valueOf("gf-title"),
    content: clippedDraftText(valueOf("gf-content")),
    enabled: document.getElementById("gf-enabled")?.classList.contains("on") ?? true,
    unsaved_changes: configEditorDirty.guidelines,
  };
  if (context.tab === "skills") return {
    id: valueOf("sf-id"), name: valueOf("sf-name"), description: valueOf("sf-desc"),
    instructions: clippedDraftText(valueOf("sf-instructions")),
    enabled: document.getElementById("sf-enabled")?.classList.contains("on") ?? true,
    unsaved_changes: configEditorDirty.skills,
  };
  if (context.tab === "rules") return {
    original_match: selectedRuleIndex === null ? null : projObj()?.rules?.[selectedRuleIndex]?.match,
    match_json: valueOf("rf-match"), require_evidence: valueOf("rf-evidence"),
    require_gates: valueOf("rf-gates"), require_capabilities: valueOf("rf-capabilities"),
    note: valueOf("rf-note"), unsaved_changes: configEditorDirty.rules,
  };
  return {
    path: docMode === "new" ? valueOf("doc-new-path") : docSelected,
    mode: docMode, viewing_revision: docViewingRevision,
    content: document.getElementById("doc-content")
      ? clippedDraftText(valueOf("doc-content")) : null,
  };
}

function clippedConfigText(value, limit = 30000) {
  const text = String(value ?? "");
  return text.length <= limit ? { text, truncated: false }
                              : { text: text.slice(0, limit), truncated: true };
}

function selectedLineRange(value, start, end) {
  const first = value.slice(0, start).split("\n").length;
  const inclusiveEnd = end > start ? end - 1 : end;
  const last = value.slice(0, inclusiveEnd).split("\n").length;
  return { first, last };
}

function setConfigChatSelection(selection) {
  configChatSelection = selection;
  updateConfigChatContext();
}

function clearConfigChatSelection() {
  setConfigChatSelection(null);
}

function configFieldLabel(element) {
  if (CONFIG_FIELD_LABELS[element.id]) return CONFIG_FIELD_LABELS[element.id];
  if (element.id?.endsWith("-extra")) return "额外引用路径";
  const direct = element.previousElementSibling;
  if (direct?.tagName === "LABEL") return direct.textContent.trim();
  return element.getAttribute("aria-label") || element.placeholder || element.name
    || element.id || "当前字段";
}

function captureConfigChatSelection() {
  const context = configChatContext();
  if (!context) return;
  const active = document.activeElement;
  const currentView = document.getElementById(`${currentTab}-view`);
  if (active && currentView?.contains(active)
      && (active.tagName === "TEXTAREA" || active.tagName === "INPUT")
      && typeof active.selectionStart === "number") {
    const start = active.selectionStart;
    const end = active.selectionEnd;
    if (start === end) { clearConfigChatSelection(); return; }
    const lines = selectedLineRange(active.value, start, end);
    setConfigChatSelection({
      context_key: context.key, field: configFieldLabel(active),
      line_start: lines.first, line_end: lines.last,
      basis: "source", ...clippedConfigText(active.value.slice(start, end), 12000),
    });
    return;
  }
  const browserSelection = window.getSelection();
  if (!browserSelection || browserSelection.rangeCount === 0) return;
  const range = browserSelection.getRangeAt(0);
  const node = range.commonAncestorContainer.nodeType === Node.TEXT_NODE
    ? range.commonAncestorContainer.parentElement : range.commonAncestorContainer;
  const source = node?.closest?.(".doc-body, .doc-pane > pre");
  if (!source) return;
  if (browserSelection.isCollapsed) { clearConfigChatSelection(); return; }
  const prefix = document.createRange();
  prefix.selectNodeContents(source);
  prefix.setEnd(range.startContainer, range.startOffset);
  const selected = browserSelection.toString();
  const first = prefix.toString().split("\n").length;
  const last = first + selected.split("\n").length - 1;
  setConfigChatSelection({
    context_key: context.key, field: "文档阅读视图", line_start: first,
    line_end: last, basis: "rendered", ...clippedConfigText(selected, 12000),
  });
}

function configChatThread(context, create = false) {
  let thread = configChatThreads.get(context.key);
  if (!thread && create) {
    thread = { channelId: null, roots: new Set(), cursor: 0, entries: [],
               running: false, refreshedAfterReply: false };
    configChatThreads.set(context.key, thread);
  }
  return thread;
}

function renderConfigChatThread(context) {
  const root = document.getElementById("config-chat-thread");
  if (!root) return;
  const thread = configChatThread(context);
  const entries = thread?.entries.slice(-16) || [];
  root.classList.toggle("has-messages", Boolean(entries.length));
  root.innerHTML = entries.map(entry => {
    const long = entry.content.length > 1200;
    const body = long
      ? `<details><summary>展开完整回复（${entry.content.length} 字符）</summary>` +
        `<div class="content">${esc(entry.content)}</div></details>`
      : `<div class="content">${esc(entry.content)}</div>`;
    return `<div class="config-chat-message ${entry.author_type}">
      <span class="who">${entry.author_type === "human" ? "你" : "@" + esc(entry.author)}</span>${body}</div>`;
  }).join("");
  if (entries.length) root.scrollTop = root.scrollHeight;
}

function updateConfigChatContext() {
  const panel = document.getElementById("config-chat");
  if (!panel) return;
  const context = configChatContext();
  panel.classList.toggle("visible", Boolean(context));
  if (!context) return;
  if (configChatSelection?.context_key !== context.key) configChatSelection = null;
  document.getElementById("config-chat-context").textContent =
    `当前页面：${context.label} · 当前对象：${context.item}`;
  const selection = document.getElementById("config-chat-selection");
  const clear = document.getElementById("config-chat-clear-selection");
  if (configChatSelection) {
    const end = configChatSelection.line_end === configChatSelection.line_start
      ? `${configChatSelection.line_start}`
      : `${configChatSelection.line_start}–${configChatSelection.line_end}`;
    const preview = configChatSelection.text.replace(/\s+/g, " ").slice(0, 150);
    selection.textContent = `已选择：${configChatSelection.field} 第 ${end} 行 · ${preview}`;
    selection.classList.add("has-selection");
    clear.style.display = "inline-block";
  } else {
    selection.textContent = "未选择文本；主控仍会收到当前页面与当前对象。";
    selection.classList.remove("has-selection");
    clear.style.display = "none";
  }
  document.getElementById("config-chat-input").placeholder =
    `询问或修改${context.label}「${context.item}」…（Enter 发送）`;
  const thread = configChatThread(context);
  if (thread) document.getElementById("config-chat-status").textContent = thread.running
    ? "项目主控正在处理…"
    : (thread.entries.some(entry => entry.author_type === "agent") ? "项目主控已回复。" : "");
  renderConfigChatThread(context);
}

async function sendConfigChat() {
  const project = projObj();
  const context = configChatContext();
  const input = document.getElementById("config-chat-input");
  const status = document.getElementById("config-chat-status");
  const request = input.value.trim();
  if (!project || !context || !request) {
    status.textContent = project ? "请输入要询问或修改的内容。" : "请先选择项目。";
    return;
  }
  const channel = projChannels().find(item =>
    item.id === `${project.id}:general` || item.id === "general" || item.id.endsWith(":general"))
    || projChannels()[0];
  if (!channel) { status.textContent = "当前项目没有频道，无法联系主控。"; return; }
  const selection = configChatSelection?.context_key === context.key
    ? configChatSelection : null;
  const payload = {
    page_kind: context.tab, page_label: context.label, current_item: context.item,
    current_draft: currentConfigDraft(context),
    selection: selection ? {
      field: selection.field, line_start: selection.line_start,
      line_end: selection.line_end, line_basis: selection.basis,
      selected_text: selection.text, selected_text_truncated: selection.truncated,
    } : null,
    user_message: request,
  };
  // 草稿或选区中的 @role 只是正文，转成 JSON Unicode 转义，避免聊天提及解析器
  // 把它误当成额外调度；整条消息只应触发开头显式指定的项目主控。
  const serializedPayload = JSON.stringify(payload, null, 2).replace(/@/g, "\\u0040");
  const content = `@${project.orchestrator_role_id} 项目配置页协作消息（JSON）：\n` +
    `${serializedPayload}\n\n` +
    `这是围绕当前页面的对话：若用户只是提问、解释或讨论，只需回答，不要写入；` +
    `若用户明确要求创建或修改，则使用 ${context.action} 控制动作实际保存完整结果。` +
    `优先处理 selection 指定的字段和行；修改现有条目时沿用当前 id、match 或路径。`;
  input.value = "";
  status.textContent = `正在发送给 @${project.orchestrator_role_id}…`;
  try {
    const response = await api("POST", `/api/chat/${encodeURIComponent(channel.id)}/messages`, {
      author: "human", content,
    });
    const thread = configChatThread(context, true);
    thread.channelId = channel.id;
    thread.roots.add(response.id);
    thread.cursor = Math.max(thread.cursor, response.id);
    thread.running = true;
    thread.refreshedAfterReply = false;
    thread.entries.push({ id: response.id, author: "human", author_type: "human", content: request });
    renderConfigChatThread(context);
    status.textContent = `@${project.orchestrator_role_id} 正在处理；回复会显示在此处。`;
    if (currentChan === channel.id) pollMessages();
    pollConfigChat();
  } catch (error) {
    input.value = request;
    status.textContent = "发送失败，请重试。";
  }
}

async function pollConfigChat() {
  if (configChatPolling) return;
  const context = configChatContext();
  if (!context) return;
  const thread = configChatThread(context);
  if (!thread?.channelId || !thread.roots.size) return;
  configChatPolling = true;
  try {
    const response = await fetch(`/api/chat/${encodeURIComponent(thread.channelId)}/messages?after_id=${thread.cursor}`);
    if (!response.ok) return;
    const data = await response.json();
    for (const message of data.messages || []) {
      thread.cursor = Math.max(thread.cursor, message.id);
      if (!thread.roots.has(message.root_id) || thread.entries.some(entry => entry.id === message.id)) continue;
      thread.entries.push(message);
    }
    thread.running = (data.active_runs || []).some(run => thread.roots.has(run.root_id));
    const stillCurrent = configChatContext()?.key === context.key;
    if (stillCurrent) {
      const status = document.getElementById("config-chat-status");
      status.textContent = thread.running ? "项目主控正在处理…"
        : (thread.entries.some(entry => entry.author_type === "agent") ? "项目主控已回复。" : status.textContent);
      renderConfigChatThread(context);
    }
    if (!thread.running && !thread.refreshedAfterReply
        && thread.entries.some(entry => entry.author_type === "agent")) {
      thread.refreshedAfterReply = true;
      loadOverview().catch(() => {});
    }
  } catch (error) { /* 服务重启或瞬时网络错误，下轮继续 */ }
  finally { configChatPolling = false; }
}

document.addEventListener("selectionchange", captureConfigChatSelection);
document.addEventListener("select", captureConfigChatSelection, true);
document.getElementById("config-chat-input").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !imeComposing(event)) {
    event.preventDefault();
    sendConfigChat();
  }
});

/* ---- 准则文档 ---- */
function renderGuidelinesPage(force = false) {
  projectConfigLabel("guide-proj-label");
  const guidelines = projObj()?.guidelines || [];
  if (selectedGuidelineId === undefined
      || (selectedGuidelineId !== null && !guidelines.some(item => item.id === selectedGuidelineId)))
    selectedGuidelineId = guidelines[0]?.id ?? null;
  if (force || !configEditorDirty.guidelines) renderGuidelineEditor();
  updateConfigChatContext();
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
    <div class="muted">关联项目文档请写成相对 Markdown 链接，例如 [部署说明](runbooks/deploy.md)；Agent 会在需要时读取。</div>
    <label>启用</label><span class="switch ${guideline?.enabled === false ? "" : "on"}" id="gf-enabled"
      role="switch" onclick="this.classList.toggle('on');markConfigDirty('guidelines')"></span>
    <div class="form-actions"><button class="action" onclick="saveGuideline()">保存</button>
      ${guideline ? `<button class="danger" onclick="deleteGuideline('${esc(guideline.id)}')">删除</button>` : ""}</div>`;
}

function editGuideline(id) {
  selectedGuidelineId = id;
  configChatSelection = null;
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
  if (force || !configEditorDirty.skills) renderSkillEditor();
  updateConfigChatContext();
}

function renderSkillEditor() {
  const skill = (projObj()?.skills || []).find(item => item.id === selectedSkillId);
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
    <div class="muted">关联项目文档请写成相对 Markdown 链接，例如 [本地 CI](runbooks/local-ci.md)；Agent 会在需要时读取。</div>
    <label>启用</label><span class="switch ${skill?.enabled === false ? "" : "on"}" id="sf-enabled"
      role="switch" onclick="this.classList.toggle('on');markConfigDirty('skills')"></span>
    <div class="form-actions"><button class="action" onclick="saveSkill()">保存</button>
      ${skill ? `<button class="danger" onclick="deleteSkill('${esc(skill.id)}')">删除</button>` : ""}</div>`;
}

function editSkill(id) {
  selectedSkillId = id;
  configChatSelection = null;
  configEditorDirty.skills = false;
  if (currentTab !== "skills") switchTab("skills");
  else renderSkillsPage(true);
}

async function saveSkill() {
  const id = document.getElementById("sf-id").value.trim();
  if (!id) { uiAlert("请输入 Skill id"); return; }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/skills`, {
    id,
    name: document.getElementById("sf-name").value.trim(),
    description: document.getElementById("sf-desc").value.trim(),
    instructions: document.getElementById("sf-instructions").value,
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
  if (force || !configEditorDirty.rules) renderRuleEditor();
  updateConfigChatContext();
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
  configChatSelection = null;
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
