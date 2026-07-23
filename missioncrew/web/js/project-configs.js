/* ---- 项目准则 / Skills 全页管理与底部主控对话 ---- */
let selectedGuidelineName;
let selectedSkillId;
const GUIDELINE_MARKDOWN_PLACEHOLDER = "---\nname: \ndescription: \n---\n\n";
const configEditorDirty = { guidelines: false, skills: false };
let skillLibraryInfo = null;
let skillFolderImportOpen = false;
const CONFIG_CHAT_TABS = new Set(["guidelines", "skills", "docs"]);
const CONFIG_CHAT_TARGETS = {
  guidelines: { label: "准则文档", action: "guideline.save" },
  skills: { label: "Skill", action: "skill.save" },
  docs: { label: "版本化文档", action: "document.publish" },
};
const CONFIG_FIELD_LABELS = {
  "gf-content": "准则 Markdown 文件",
  "sf-id": "Skill id", "sf-content": "SKILL.md 文件",
  "doc-new-path": "文档路径", "doc-content": "文档正文",
};
let configChatSelection = null;
let configChatPolling = false;
const configChatThreads = new Map();
const CONFIG_CHAT_COLLAPSED_KEY = "mc.configChatCollapsed";
const CONFIG_CHAT_HEIGHT_KEY = "mc.configChatHeight";
let configChatCollapsed = localStorage.getItem(CONFIG_CHAT_COLLAPSED_KEY) === "1";
let configChatHeight = Number(localStorage.getItem(CONFIG_CHAT_HEIGHT_KEY)) || 270;

function boundedConfigChatHeight(value) {
  return Math.max(180, Math.min(value, Math.max(180, window.innerHeight - 90)));
}

function applyConfigChatLayout() {
  const panel = document.getElementById("config-chat");
  const toggle = document.getElementById("config-chat-toggle");
  const views = document.getElementById("views");
  if (!panel || !toggle || !views) return;
  configChatHeight = boundedConfigChatHeight(configChatHeight);
  panel.classList.toggle("collapsed", configChatCollapsed);
  panel.style.height = configChatCollapsed ? "" : `${configChatHeight}px`;
  toggle.textContent = configChatCollapsed ? "💬" : "−";
  toggle.setAttribute("aria-label", configChatCollapsed ? "展开主控对话" : "收起主控对话");
  toggle.title = configChatCollapsed ? "展开主控对话" : "收起主控对话";
  views.style.setProperty("--config-chat-space",
    configChatCollapsed ? "86px" : `${configChatHeight + 38}px`);
}

function toggleConfigChatCollapsed() {
  configChatCollapsed = !configChatCollapsed;
  localStorage.setItem(CONFIG_CHAT_COLLAPSED_KEY, configChatCollapsed ? "1" : "0");
  applyConfigChatLayout();
  if (!configChatCollapsed) document.getElementById("config-chat-input")?.focus();
}

function startConfigChatResize(event) {
  if (configChatCollapsed || event.button !== 0) return;
  event.preventDefault();
  const panel = document.getElementById("config-chat");
  if (!panel) return;
  const startY = event.clientY;
  const startHeight = panel.getBoundingClientRect().height;
  document.body.classList.add("resizing-config-chat");
  const move = moveEvent => {
    configChatHeight = boundedConfigChatHeight(startHeight + startY - moveEvent.clientY);
    applyConfigChatLayout();
  };
  const stop = () => {
    document.body.classList.remove("resizing-config-chat");
    localStorage.setItem(CONFIG_CHAT_HEIGHT_KEY, String(Math.round(configChatHeight)));
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", stop);
    window.removeEventListener("pointercancel", stop);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", stop);
  window.addEventListener("pointercancel", stop);
}

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
}

function configChatContext() {
  const project = projObj();
  const target = CONFIG_CHAT_TARGETS[currentTab];
  if (!project || !CONFIG_CHAT_TABS.has(currentTab) || !target) return null;
  let item = "未选择条目";
  let itemKey = "none";
  if (currentTab === "guidelines") {
    const guideline = project.guidelines?.find(value => value.name === selectedGuidelineName);
    const draftName = guidelineFrontmatterValue(valueOf("gf-content"), "name");
    item = guideline ? `${guideline.name}（Markdown 文件${guidelineViewer.viewingRevision
      ? `，历史版本 ${guidelineViewer.viewingRevision.slice(0, 10)}` : ""}）`
                     : `新建准则（${draftName || "name 未填写"}，未保存）`;
    itemKey = guideline?.name || "new";
  } else if (currentTab === "skills") {
    const skill = project.skills?.find(value => value.id === selectedSkillId);
    const draftName = guidelineFrontmatterValue(valueOf("sf-content"), "name");
    const draftId = skill ? skill.id : valueOf("sf-id").trim();
    item = skill ? `${draftName || skill.name || skill.id}（id: ${skill.id}）`
                 : `新建 Skill（${draftName || draftId || "未命名"}，未保存）`;
    itemKey = skill?.id || "new";
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

function guidelineFrontmatterValue(markdown, key) {
  const end = markdown.indexOf("\n---", 4);
  if (!markdown.startsWith("---\n") || end < 0) return "";
  const line = markdown.slice(4, end).split("\n")
    .find(value => value.trimStart().startsWith(`${key}:`));
  return line ? line.slice(line.indexOf(":") + 1).trim().replace(/^(['"])(.*)\1$/, "$2") : "";
}

function currentConfigDraft(context) {
  if (context.tab === "skills") return {
    id: selectedSkillId ?? valueOf("sf-id").trim(),
    markdown: clippedConfigText(valueOf("sf-content")).text,
    frontmatter_contract: {
      required_attributes: ["name", "description"],
      source_of_truth: "后端直接从这份 SKILL.md frontmatter 读取 name、description；其他附加属性原样保留",
    },
    enabled: document.getElementById("sf-enabled")?.classList.contains("on") ?? true,
    unsaved_changes: configEditorDirty.skills,
  };
  return null;
}

function clippedConfigText(value, limit = 30000) {
  const text = String(value ?? "");
  return text.length <= limit ? { text, truncated: false }
                              : { text: text.slice(0, limit), truncated: true };
}

function configPageSnapshot(context) {
  if (context.tab === "guidelines") {
    const guideline = projObj()?.guidelines
      ?.find(value => value.name === selectedGuidelineName);
    const pageKey = selectedGuidelineName
      || guidelineFrontmatterValue(valueOf("gf-content"), "name") || "new-guideline.md";
    return {
      pageKey: pageKey.endsWith(".md") ? pageKey : `${pageKey}.md`,
      content: guidelineViewer.currentText() ?? "",
      metadata: {
        original_name: selectedGuidelineName,
        enabled: document.getElementById("gf-enabled")?.classList.contains("on")
          ?? guideline?.enabled ?? true,
        unsaved_changes: guidelineViewer.hasUnsaved(),
        viewing_revision: guidelineViewer.viewingRevision,
      },
    };
  }
  if (context.tab === "docs") {
    const documentPath = docMode === "new" ? valueOf("doc-new-path").trim() : docSelected;
    if (docPaneContentIdentity !== currentDocContentIdentity()) return null;
    if (!documentPath) return null;
    const documentMetadata = {
      filename: documentPath.split("/").pop(),
      document_path: documentPath,
      resource_url: missionCrewDocumentUrl(currentProject, documentPath),
      mode: docMode === "new" ? "new" : docViewer.mode,
      viewing_revision: docViewer.viewingRevision,
    };
    if (docPaneContentType === "binary") return {
      pageKey: documentPath,
      content: null,
      metadata: {
        ...documentMetadata,
        text_snapshot_available: false,
      },
    };
    const editor = document.getElementById("doc-content");
    const content = editor ? editor.value : docPaneContent;
    if (content === null) return null;
    return {
      pageKey: documentPath,
      content,
      metadata: {
        ...documentMetadata,
        text_snapshot_available: true,
      },
    };
  }
  return null;
}

async function stageConfigPage(channel, context) {
  const snapshot = configPageSnapshot(context);
  if (!snapshot) throw new Error("当前页面还没有可提供给主控的文件信息");
  if (snapshot.content === null) return snapshot.metadata;
  const stored = await api(
    "POST", `/api/chat/${encodeURIComponent(channel.id)}/page-context`, {
      page_kind: context.tab, page_key: snapshot.pageKey, content: snapshot.content,
    });
  return { content_path: stored.path, ...snapshot.metadata };
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
  const source = node?.closest?.(".doc-body, .text-lines, .viewer-edit-preview, .guideline-markdown-preview, .skill-markdown-preview");
  if (!source) return;
  if (browserSelection.isCollapsed) { clearConfigChatSelection(); return; }
  const prefix = document.createRange();
  prefix.selectNodeContents(source);
  prefix.setEnd(range.startContainer, range.startOffset);
  const selected = browserSelection.toString();
  const first = prefix.toString().split("\n").length;
  const last = first + selected.split("\n").length - 1;
  setConfigChatSelection({
    context_key: context.key,
    field: source.classList.contains("skill-markdown-preview") ? "SKILL.md 阅读视图"
      : source.closest("#guidelines-view") ? "准则阅读视图"
      : "文档阅读视图",
    line_start: first,
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
  const renderKey = `${context.key}:${JSON.stringify(entries.map(entry => [
    entry.id, entry.author, entry.author_type, entry.content,
  ]))}`;
  if (root.dataset.renderKey === renderKey) return;
  const sameContext = root.dataset.contextKey === context.key;
  const shouldFollow = !sameContext || isNearScrollBottom(root);
  const expanded = new Set([...root.querySelectorAll(".config-chat-message details[open]")]
    .map(details => details.closest(".config-chat-message")?.dataset.messageId).filter(Boolean));
  root.classList.toggle("has-messages", Boolean(entries.length));
  root.innerHTML = entries.map(entry => {
    const long = entry.content.length > 1200;
    const body = long
      ? `<details ${expanded.has(String(entry.id)) ? "open" : ""}><summary>展开完整回复（${entry.content.length} 字符）</summary>` +
        `<div class="content">${esc(entry.content)}</div></details>`
      : `<div class="content">${esc(entry.content)}</div>`;
    return `<div class="config-chat-message ${entry.author_type}" data-message-id="${esc(entry.id)}">
      <span class="who">${entry.author_type === "human" ? "你" : "@" + esc(entry.author)}</span>${body}</div>`;
  }).join("");
  root.dataset.contextKey = context.key;
  root.dataset.renderKey = renderKey;
  if (entries.length && shouldFollow) root.scrollTop = root.scrollHeight;
}

function updateConfigChatContext() {
  const panel = document.getElementById("config-chat");
  if (!panel) return;
  const context = configChatContext();
  panel.classList.toggle("visible", Boolean(context));
  applyConfigChatLayout();
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
  } else if (context.tab === "docs" && docPaneContentType === "binary" && docSelected) {
    selection.textContent = `非文本文件；主控将收到文件名与文档路径：${docSelected}`;
    selection.classList.remove("has-selection");
    clear.style.display = "none";
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
  status.textContent = `正在发送给 @${project.orchestrator_role_id}…`;
  try {
    const fileBackedPage = context.tab === "guidelines" || context.tab === "docs";
    const currentPage = fileBackedPage ? await stageConfigPage(channel, context) : null;
    const effectiveSelection = currentPage?.text_snapshot_available === false
      ? null : selection;
    const selectionPayload = effectiveSelection ? {
      field: effectiveSelection.field, line_start: effectiveSelection.line_start,
      line_end: effectiveSelection.line_end, line_basis: effectiveSelection.basis,
      ...(fileBackedPage ? { read_from: "current_page.content_path" } : {
        selected_text: effectiveSelection.text,
        selected_text_truncated: effectiveSelection.truncated,
      }),
    } : null;
    const payload = {
      page_kind: context.tab, page_label: context.label, current_item: context.item,
      ...(fileBackedPage ? { current_page: currentPage }
                         : { current_draft: currentConfigDraft(context) }),
      selection: selectionPayload, user_message: request,
    };
    // Skill 草稿中的 @role 只是正文；JSON Unicode 转义避免被误判为额外调度。
    const serializedPayload = JSON.stringify(payload, null, 2).replace(/@/g, "\\u0040");
    const guidelineEditingTip = context.tab === "guidelines"
      ? `当前准则正文没有内嵌在消息中；先读取 current_page.content_path。文件包含 YAML frontmatter 和正文，` +
        `frontmatter 只使用 name、description。保存时把修改后的完整文件放入 ${context.action}.markdown，` +
        `并在修改现有文件时传 current_page.original_name；不要使用 id、title、summary。`
      : "";
    const documentEditingTip = context.tab !== "docs" ? ""
      : currentPage?.text_snapshot_available === false
        ? `当前对象不是 UTF-8 纯文本，因此没有 current_page.content_path、selection 或行号。` +
          `文件名、项目文档相对路径和 Web 地址分别在 current_page.filename、` +
          `current_page.document_path、current_page.resource_url；需要读取或修改原文件时，` +
          `以 MISSIONCREW_DOCUMENTS_DIR 为根解析 document_path，并使用适合该格式的工具。` +
          `若通过 ${context.action} 覆盖二进制文件，使用 content_base64、原 document_path 和 overwrite=true。`
        : `当前文档正文没有内嵌在消息中；先读取 current_page.content_path。保存时把完整结果放入 ` +
          `${context.action}.content，目标路径使用 current_page.document_path。`;
    const skillEditingTip = context.tab === "skills"
      ? `当前编辑对象是一份完整 SKILL.md 文件。current_draft.markdown 包含 YAML frontmatter 和正文；` +
        `frontmatter 必须含 name、description，其他附加属性保持原样。保存时把修改后的完整文件放入 ` +
        `${context.action}.markdown，id 传 current_draft.id（Skill 目录名，不可修改）。`
      : "";
    const content = `@${project.orchestrator_role_id} 项目配置页协作消息（JSON）：\n` +
      `${serializedPayload}\n\n` +
      `这是围绕当前页面的对话：若用户只是提问、解释或讨论，只需回答，不要写入；` +
      `若用户明确要求创建或修改，则使用 ${context.action} 控制动作实际保存完整结果。` +
      `优先处理 selection 指定的字段和行；修改现有条目时沿用当前 name、id、match 或路径。` +
      guidelineEditingTip + documentEditingTip + skillEditingTip;
    input.value = "";
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
    status.textContent = error.message || "发送失败，请重试。";
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
window.addEventListener("resize", applyConfigChatLayout);
applyConfigChatLayout();

/* ---- 准则文档：与版本化文档库共用 viewer.js 统一组件 ---- */
let guidelineEditorSignature = null;

function guidelineEditorStateSignature(guideline) {
  return JSON.stringify([currentProject, selectedGuidelineName, guideline || null]);
}

function selectedGuideline() {
  return (projObj()?.guidelines || []).find(item => item.name === selectedGuidelineName);
}

const guidelineViewer = createTextViewer({
  containerId: "guideline-editor",
  textareaId: "gf-content",
  editorClass: "guideline-markdown-editor",
  previewClass: "guideline-markdown-preview",
  surfaceClass: "guideline-markdown-surface",
  identity: () => JSON.stringify(
    [currentProject, selectedGuidelineName, guidelineViewer.viewingRevision]),
  title: () => selectedGuidelineName || "新建准则",
  metaLine: () => "",
  editKind: () => "markdown",
  hasHistory: () => Boolean(selectedGuidelineName),
  loadView: async revision => {
    if (revision) {
      const version = await api("GET",
        `/api/projects/${encodeURIComponent(currentProject)}/guidelines/` +
        `${encodeURIComponent(selectedGuidelineName)}/history/${encodeURIComponent(revision)}`);
      return { kind: "markdown", content: version.markdown };
    }
    return { kind: "markdown",
             content: selectedGuideline()?.markdown || GUIDELINE_MARKDOWN_PLACEHOLDER };
  },
  loadEdit: () => selectedGuideline()?.markdown || GUIDELINE_MARKDOWN_PLACEHOLDER,
  save: async content => {
    const saved = await api("POST",
      `/api/projects/${encodeURIComponent(currentProject)}/guidelines`, {
        original_name: selectedGuidelineName,
        markdown: content,
        enabled: document.getElementById("gf-enabled")?.classList.contains("on") ?? true,
      });
    selectedGuidelineName = saved.name;
    configEditorDirty.guidelines = false;
    await loadOverview();
    syncUrl();
    toast("准则文档已保存", "success");
  },
  listHistory: () => api("GET",
    `/api/projects/${encodeURIComponent(currentProject)}/guidelines/` +
    `${encodeURIComponent(selectedGuidelineName)}/history`),
  compare: (from, to) => api("POST",
    `/api/projects/${encodeURIComponent(currentProject)}/guidelines/` +
    `${encodeURIComponent(selectedGuidelineName)}/compare`,
    { from_revision: from, to_revision: to }),
  restore: async revision => {
    if (!await uiConfirm(
      `把准则恢复到版本 ${revision.slice(0, 10)}？恢复会写入一个新版本，后续历史仍保留。`))
      throw new Error("cancelled");
    const restored = await api("POST",
      `/api/projects/${encodeURIComponent(currentProject)}/guidelines/` +
      `${encodeURIComponent(selectedGuidelineName)}/restore`, { revision });
    selectedGuidelineName = restored.name;
    configEditorDirty.guidelines = false;
    await loadOverview();
    syncUrl();
    toast(`准则已恢复，新版本 ${restored.revision.slice(0, 10)}`, "success");
  },
  extrasHtml: mode => {
    const guideline = selectedGuideline();
    const deleteButton = guideline
      ? `<button class="danger" type="button" data-name="${esc(guideline.name)}"
          onclick="deleteGuideline(this.dataset.name)">删除</button>` : "";
    if (mode === "edit") {
      return `<label class="guideline-enabled"><span>启用</span>
          <span class="switch ${guideline?.enabled === false ? "" : "on"}" id="gf-enabled"
            role="switch" tabindex="0"
            onclick="this.classList.toggle('on');guidelineViewer.markDirty()"></span></label>
        ${deleteButton}`;
    }
    return deleteButton;
  },
  scrollSelectors: () => ["#guidelines-view", "#gf-content", ".viewer-edit-preview"],
  onChange: () => updateConfigChatContext(),
  onDirty: () => {
    configEditorDirty.guidelines = true;
    updateConfigChatContext();
  },
});

function renderGuidelinesPage(force = false) {
  const guidelines = projObj()?.guidelines || [];
  if (selectedGuidelineName === undefined
      || (selectedGuidelineName !== null
          && !guidelines.some(item => item.name === selectedGuidelineName)))
    selectedGuidelineName = guidelines[0]?.name ?? null;
  const guideline = guidelines.find(item => item.name === selectedGuidelineName);
  const signature = guidelineEditorStateSignature(guideline);
  const changed = signature !== guidelineEditorSignature;
  guidelineEditorSignature = signature;
  // 有未保存修改时保留编辑现场，不被后台刷新覆盖
  if ((force || changed) && !guidelineViewer.hasUnsaved())
    void guidelineViewer.render();
  updateConfigChatContext();
}

async function editGuideline(name) {
  if (!await guidelineViewer.confirmDiscard()) return;
  selectedGuidelineName = name;
  guidelineViewer.activate();
  if (!name) guidelineViewer.mode = "edit";   // 新建准则直接进入编辑模式
  configChatSelection = null;
  configEditorDirty.guidelines = false;
  if (currentTab !== "guidelines") switchTab("guidelines");
  else { renderGuidelinesPage(true); syncUrl(); }
}

async function deleteGuideline(name) {
  if (!await uiConfirm(`将准则文档「${name}」移入项目回收站？`)) return;
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/guidelines/${encodeURIComponent(name)}`);
  selectedGuidelineName = undefined;
  guidelineViewer.activate();
  configEditorDirty.guidelines = false;
  await loadOverview();
  renderGuidelinesPage(true);
  toast("准则文档已移入回收站", "success");
}

/* ---- Skills ---- */
const SKILL_MARKDOWN_PLACEHOLDER = "---\nname: \ndescription: \n---\n\n";
let skillMarkdownMode = "preview";
let skillFileTreeCollapsed = false;
let skillEditorSignature = null;
let skillLibraryLoad = null;

function skillMetadataSignature(skills) {
  return JSON.stringify((skills || []).map(skill => ({
    id: skill.id, name: skill.name, description: skill.description,
    instructions: skill.instructions, enabled: skill.enabled,
  })));
}

function skillEditorStateSignature(skill, packageInfo) {
  return JSON.stringify([
    currentProject, selectedSkillId, skill || null,
    packageInfo ? packageInfo.content_version : null,
  ]);
}

function renderSkillsPage(force = false) {
  projectConfigLabel("skill-proj-label");
  const skills = projObj()?.skills || [];
  if (selectedSkillId === undefined
      || (selectedSkillId !== null && !skills.some(item => item.id === selectedSkillId)))
    selectedSkillId = skills[0]?.id ?? null;
  if (skillLibraryInfo?.project_id === currentProject
      && skillMetadataSignature(skillLibraryInfo.skills) !== skillMetadataSignature(skills))
    skillLibraryInfo = null;
  const skill = skills.find(item => item.id === selectedSkillId);
  const packageInfo = skill ? selectedSkillPackage() : null;
  const signature = skillEditorStateSignature(skill, packageInfo);
  if (force || (!configEditorDirty.skills && signature !== skillEditorSignature))
    renderSkillEditor(skill, packageInfo, signature);
  renderSkillLibraryStatus();
  if (skillLibraryInfo?.project_id !== currentProject) loadSkillLibraryInfo();
  updateConfigChatContext();
}

function loadSkillLibraryInfo() {
  const projectId = currentProject;
  if (!projectId) return Promise.resolve();
  if (skillLibraryLoad?.projectId === projectId) return skillLibraryLoad.promise;
  const request = (async () => {
    try {
      const info = await api("GET", `/api/projects/${encodeURIComponent(projectId)}/skills/library`);
      if (projectId !== currentProject) return;
      skillLibraryInfo = { ...info, project_id: projectId };
      renderSkillsPage();
    } catch (_) { /* api() 已显示错误 */ }
  })();
  const tracked = request.finally(() => {
    if (skillLibraryLoad?.promise === tracked) skillLibraryLoad = null;
  });
  skillLibraryLoad = { projectId, promise: tracked };
  return tracked;
}

function renderSkillLibraryStatus() {
  const box = document.getElementById("skill-library-info");
  if (!box) return;
  if (!skillLibraryInfo || skillLibraryInfo.project_id !== currentProject) {
    box.innerHTML = `<span class="muted">正在读取项目 Skill 目录…</span>`;
    return;
  }
  const issues = skillLibraryInfo.issues || [];
  box.innerHTML = `
    <span><strong>直接投放目录</strong> <code>${esc(skillLibraryInfo.path)}</code>
      <span class="muted">复制含 SKILL.md 的 Skill 目录到此处，平台自动发现。</span></span>
    ${skillFolderImportOpen ? `<div class="skill-folder-import form">
      <label>包含一个或多个 Skill 的本地目录</label>
      <div class="row">
        <div><input type="text" id="skill-import-path" placeholder="~/skills 或 /path/to/skills"></div>
        <div style="flex:0 0 90px"><button class="ghost" style="width:100%"
          onclick="openDirPicker(document.getElementById('skill-import-path').value,'skill-import-path')">浏览…</button></div>
      </div>
      <div class="muted">平台会递归检测有效 SKILL.md，并完整复制其所在目录。</div>
      <div class="form-actions"><button class="action" onclick="importSkillFolder(false)">导入</button>
        <button class="ghost" onclick="closeSkillFolderImport()">取消</button></div>
    </div>` : ""}
    ${issues.length ? `<details class="skill-scan-issues"><summary>${issues.length} 个目录未载入</summary>
      <ul>${issues.map(issue => `<li>${esc(issue)}</li>`).join("")}</ul></details>` : ""}`;
}

function selectedSkillPackage() {
  return skillLibraryInfo?.project_id === currentProject
    ? (skillLibraryInfo.skills || []).find(item => item.id === selectedSkillId) : null;
}

function renderSkillEditor(skill = undefined, packageInfo = undefined, signature = undefined) {
  if (skill === undefined)
    skill = (projObj()?.skills || []).find(item => item.id === selectedSkillId);
  if (packageInfo === undefined) packageInfo = skill ? selectedSkillPackage() : null;
  if (!skill) skillMarkdownMode = "edit";
  // 已存在的 Skill 必须等库信息带回 SKILL.md 原文再进入编辑，
  // 避免用字段重建的草稿覆盖 frontmatter 附加属性。
  const loading = Boolean(skill) && !packageInfo;
  const root = document.getElementById("skill-editor");
  const itemKey = `${currentProject}:${selectedSkillId ?? "new"}`;
  const scrollState = root.dataset.itemKey === itemKey
    ? captureScrollPositions([
      "#skills-view", "#sf-content", "#skill-markdown-preview",
      ".skill-file-tree-scroll", ".skill-file-viewer-body",
    ]) : [];
  const viewerScrollState = scrollState.filter(
    position => position.selector === ".skill-file-viewer-body");
  root.innerHTML = `
    <div class="guideline-toolbar">
      ${skill
        ? `<div class="guideline-current-name"><span>id</span>
            <code>${esc(skill.id)}</code></div>`
        : `<label class="guideline-toolbar-field"><span>id（保存后不可修改）</span>
            <input id="sf-id" value="" placeholder="例如 local-ci"
              oninput="markConfigDirty('skills')"></label>`}
      <label class="guideline-enabled"><span>启用</span>
        <span class="switch ${skill?.enabled === false ? "" : "on"}" id="sf-enabled"
          role="switch" tabindex="0"
          onclick="this.classList.toggle('on');markConfigDirty('skills')"></span></label>
      <span class="guideline-toolbar-spacer"></span>
      ${skill ? `<button class="ghost compact ${skillFileTreeCollapsed ? "" : "active"}"
        id="skill-tree-toggle" type="button" onclick="toggleSkillFileTree()">文件树</button>` : ""}
      <div class="guideline-view-toggle" aria-label="Markdown 显示方式">
        <button class="ghost compact" id="skill-edit-button" type="button"
          onclick="setSkillMarkdownMode('edit')">编辑</button>
        <button class="ghost compact" id="skill-preview-button" type="button"
          onclick="setSkillMarkdownMode('preview')">预览</button>
      </div>
      <button class="action" type="button" ${loading ? "disabled" : ""}
        onclick="saveSkill()">保存</button>
      ${skill ? `<button class="danger" type="button" data-id="${esc(skill.id)}"
        onclick="deleteSkill(this.dataset.id)">删除</button>` : ""}
    </div>
    <div class="skill-main">
      ${skill ? `<aside class="skill-file-tree ${skillFileTreeCollapsed ? "collapsed" : ""}"
        id="skill-file-tree" style="width:${skillTreeWidth}px">
        <div class="skill-file-tree-scroll">${skillFileTreeHtml(packageInfo)}</div>
        <div class="skill-tree-resize" onpointerdown="startSkillTreeResize(event)"
          title="拖动调整文件树宽度"></div>
      </aside>` : ""}
      <div class="guideline-markdown-surface">
        ${loading ? `<div class="empty" style="padding:18px 20px">正在读取 SKILL.md…</div>` : `
        <textarea id="sf-content" class="guideline-markdown-editor" aria-label="完整 SKILL.md 文件"
          spellcheck="false"
          oninput="markConfigDirty('skills');updateSkillMarkdownPreview()">${esc(skill ? packageInfo.markdown : SKILL_MARKDOWN_PLACEHOLDER)}</textarea>
        <article class="skill-markdown-preview markdown-body" id="skill-markdown-preview"></article>`}
      </div>
      <div class="skill-file-viewer" id="skill-file-viewer" hidden></div>
    </div>`;
  root.dataset.itemKey = itemKey;
  skillEditorSignature = signature ?? skillEditorStateSignature(skill, packageInfo);
  updateSkillMarkdownPreview();
  setSkillMarkdownMode(skillMarkdownMode);
  restoreScrollPositions(scrollState.filter(
    position => position.selector !== ".skill-file-viewer-body"));
  if (skill && skillOpenFile) openSkillFile(skillOpenFile, viewerScrollState);
}

function skillFileTreeHtml(packageInfo) {
  const head = `<div class="skill-file-tree-head"><strong>文件树</strong>
    <span class="muted">${packageInfo ? `${(packageInfo.files || []).length} 个文件` : ""}</span></div>`;
  if (!packageInfo) return head + `<div class="muted">正在读取文件清单…</div>`;
  const pathLine = `<code class="skill-file-tree-path">${esc(packageInfo.path)}</code>`;
  const files = packageInfo.files || [];
  if (!files.length) return head + pathLine + `<div class="muted">目录为空。</div>`;
  const root = { dirs: new Map(), files: [] };
  for (const relative of files) {
    const parts = relative.split("/");
    let node = root;
    for (const part of parts.slice(0, -1)) {
      if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
      node = node.dirs.get(part);
    }
    node.files.push(parts[parts.length - 1]);
  }
  const renderNode = (node, prefix = "") => [
    ...[...node.dirs.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, child]) =>
      `<details open><summary title="${esc(prefix + name + "/")}">${esc(name)}/</summary>
        <div class="skill-tree-children">${renderNode(child, prefix + name + "/")}</div></details>`),
    ...node.files.slice().sort().map(name =>
      `<div class="skill-tree-file ${name.toLowerCase() === "skill.md" ? "main" : ""}"
        data-path="${esc(prefix + name)}" title="${esc(prefix + name)}"
        onclick="openSkillFile(this.dataset.path)">${esc(name)}</div>`),
  ].join("");
  return head + pathLine + `<div class="skill-tree-body">${renderNode(root)}</div>`;
}

const SKILL_TREE_WIDTH_KEY = "mc.skillTreeWidth";
let skillTreeWidth = Math.max(160, Math.min(
  Number(localStorage.getItem(SKILL_TREE_WIDTH_KEY)) || 240, 520));

function startSkillTreeResize(event) {
  if (event.button !== 0) return;
  event.preventDefault();
  const tree = document.getElementById("skill-file-tree");
  if (!tree) return;
  const startX = event.clientX;
  const startWidth = tree.getBoundingClientRect().width;
  document.body.classList.add("resizing-skill-tree");
  const move = moveEvent => {
    skillTreeWidth = Math.max(160, Math.min(startWidth + moveEvent.clientX - startX, 520));
    tree.style.width = `${skillTreeWidth}px`;
  };
  const stop = () => {
    document.body.classList.remove("resizing-skill-tree");
    localStorage.setItem(SKILL_TREE_WIDTH_KEY, String(Math.round(skillTreeWidth)));
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", stop);
    window.removeEventListener("pointercancel", stop);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", stop);
  window.addEventListener("pointercancel", stop);
}

function toggleSkillFileTree() {
  skillFileTreeCollapsed = !skillFileTreeCollapsed;
  document.getElementById("skill-file-tree")
    ?.classList.toggle("collapsed", skillFileTreeCollapsed);
  document.getElementById("skill-tree-toggle")
    ?.classList.toggle("active", !skillFileTreeCollapsed);
}

function updateSkillMarkdownPreview() {
  const preview = document.getElementById("skill-markdown-preview");
  if (!preview) return;
  const markdown = valueOf("sf-content");
  preview.innerHTML = markdown.trim() ? markdownPreviewHtml(markdown)
    : `<div class="empty">正文为空。切换到“编辑”输入 Markdown。</div>`;
}

function setSkillMarkdownMode(mode) {
  skillMarkdownMode = mode === "edit" ? "edit" : "preview";
  const editor = document.getElementById("sf-content");
  const preview = document.getElementById("skill-markdown-preview");
  if (!editor || !preview) return;
  const editing = skillMarkdownMode === "edit";
  editor.hidden = !editing;
  preview.hidden = editing;
  document.getElementById("skill-edit-button")?.classList.toggle("active", editing);
  document.getElementById("skill-preview-button")?.classList.toggle("active", !editing);
  if (!editing) updateSkillMarkdownPreview();
}

let skillOpenFile = null;

function isSkillMarkdownFile(path) {
  return /\.(?:md|markdown)$/i.test(path);
}

async function openSkillFile(path, restoreState = null) {
  if (!selectedSkillId) return;
  if (path.toLowerCase() === "skill.md") { closeSkillFile(); return; }
  const projectId = currentProject;
  const skillId = selectedSkillId;
  skillOpenFile = path;
  const data = await api("GET",
    `/api/projects/${encodeURIComponent(projectId)}/skills/` +
    `${encodeURIComponent(skillId)}/file?path=${encodeURIComponent(path)}`);
  if (projectId !== currentProject || skillId !== selectedSkillId || skillOpenFile !== path) return;
  const viewer = document.getElementById("skill-file-viewer");
  if (!viewer) return;
  const body = isSkillMarkdownFile(path)
    ? `<article class="skill-file-viewer-body markdown-body">${
        markdownPreviewHtml(data.content)}</article>`
    : `<pre class="skill-file-viewer-body">${esc(data.content)}</pre>`;
  viewer.innerHTML = `
    <div class="skill-file-viewer-head">
      <code>${esc(path)}</code>
      <span class="guideline-toolbar-spacer"></span>
      ${data.truncated ? `<span class="muted">文件过大，仅显示前 512 KB</span>` : ""}
      <button class="ghost compact" type="button" onclick="closeSkillFile()">返回 SKILL.md</button>
    </div>
    ${body}`;
  viewer.hidden = false;
  document.querySelector("#skill-editor .guideline-markdown-surface")
    ?.setAttribute("hidden", "");
  document.querySelectorAll("#skill-file-tree .skill-tree-file").forEach(row =>
    row.classList.toggle("active", row.dataset.path === path));
  restoreScrollPositions(restoreState);
  syncUrl();
}

function closeSkillFile() {
  skillOpenFile = null;
  const viewer = document.getElementById("skill-file-viewer");
  if (viewer) { viewer.hidden = true; viewer.innerHTML = ""; }
  document.querySelector("#skill-editor .guideline-markdown-surface")
    ?.removeAttribute("hidden");
  document.querySelectorAll("#skill-file-tree .skill-tree-file.active")
    .forEach(row => row.classList.remove("active"));
  syncUrl();
}

function editSkill(id) {
  selectedSkillId = id;
  skillOpenFile = null;
  skillMarkdownMode = id ? "preview" : "edit";
  configChatSelection = null;
  configEditorDirty.skills = false;
  if (currentTab !== "skills") switchTab("skills");
  else { renderSkillsPage(true); syncUrl(); }
}

async function saveSkill() {
  const skill = (projObj()?.skills || []).find(item => item.id === selectedSkillId);
  const id = skill ? skill.id : document.getElementById("sf-id").value.trim();
  if (!id) { uiAlert("请输入 Skill id"); return; }
  const markdown = document.getElementById("sf-content")?.value;
  if (markdown == null) { uiAlert("SKILL.md 尚未加载完成，请稍候"); return; }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/skills`, {
    id,
    markdown,
    enabled: document.getElementById("sf-enabled").classList.contains("on"),
  });
  selectedSkillId = id;
  skillMarkdownMode = "preview";
  configEditorDirty.skills = false;
  await loadOverview();
  skillLibraryInfo = null;
  await loadSkillLibraryInfo();
  renderSkillsPage(true);
  toast("Skill 已保存", "success");
}

async function deleteSkill(id) {
  if (!await uiConfirm(`将 Skill「${id}」移入项目回收站？`)) return;
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/skills/${encodeURIComponent(id)}`);
  selectedSkillId = undefined;
  skillOpenFile = null;
  configEditorDirty.skills = false;
  await loadOverview();
  skillLibraryInfo = null;
  await loadSkillLibraryInfo();
  renderSkillsPage(true);
  toast("Skill 已移入回收站", "success");
}

async function finishSkillImport(result) {
  if (result.needs_confirmation) return false;
  selectedSkillId = result.imported?.[0] || selectedSkillId;
  configEditorDirty.skills = false;
  skillLibraryInfo = null;
  await loadOverview();
  await loadSkillLibraryInfo();
  renderSkillsPage(true);
  const issueText = result.issues?.length ? `；${result.issues.length} 个条目未载入` : "";
  toast(`已导入 ${result.imported?.length || 0} 个 Skill${issueText}`, "success", 5000);
  return true;
}

async function importSkillZip(input) {
  const file = input.files?.[0];
  if (!file || !currentProject) return;
  const run = async overwrite => {
    const url = `/api/projects/${encodeURIComponent(currentProject)}/skills/import-zip?overwrite=${overwrite}`;
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/zip", "X-MissionCrew-Filename": file.name },
      body: file,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      toast(error.detail || `ZIP 导入失败 (${response.status})`, "error", 6000);
      return null;
    }
    return response.json();
  };
  try {
    let result = await run(false);
    if (result?.needs_confirmation) {
      const confirmed = await uiConfirm(
        `以下 Skill 已存在：${result.conflicts.join("、")}。覆盖时旧目录会移入项目回收站，是否继续？`,
        "覆盖已有 Skill");
      if (confirmed) result = await run(true);
    }
    if (result) await finishSkillImport(result);
  } catch (error) {
    toast(`ZIP 导入失败：${error.message || error}`, "error", 6000);
  } finally {
    input.value = "";
  }
}

function openSkillFolderImport() {
  skillFolderImportOpen = true;
  renderSkillLibraryStatus();
  setTimeout(() => document.getElementById("skill-import-path")?.focus(), 60);
}

function closeSkillFolderImport() {
  skillFolderImportOpen = false;
  renderSkillLibraryStatus();
}

async function importSkillFolder(overwrite) {
  const path = document.getElementById("skill-import-path")?.value.trim();
  if (!path) { uiAlert("请选择本地 Skill 目录"); return; }
  let result = await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/skills/import-folder`, {
    path, overwrite,
  });
  if (result.needs_confirmation) {
    const confirmed = await uiConfirm(
      `以下 Skill 已存在：${result.conflicts.join("、")}。覆盖时旧目录会移入项目回收站，是否继续？`,
      "覆盖已有 Skill");
    if (!confirmed) return;
    result = await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/skills/import-folder`, {
      path, overwrite: true,
    });
  }
  skillFolderImportOpen = false;
  await finishSkillImport(result);
}

async function rescanSkillLibrary() {
  const info = await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/skills/rescan`);
  skillLibraryInfo = { ...info, project_id: currentProject };
  await loadOverview();
  renderSkillsPage(true);
  toast(`已扫描 ${info.skills?.length || 0} 个 Skill`, "success");
}
