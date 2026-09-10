/* ---- 项目准则 / Skills 全页管理与底部页面对话 ---- */
let selectedGuidelineName;
let selectedSkillId;
const GUIDELINE_MARKDOWN_PLACEHOLDER = "---\nname: \ndescription: \n---\n\n";
const configEditorDirty = { guidelines: false, skills: false };
let skillLibraryInfo = null;
let skillFolderImportOpen = false;
const CONFIG_CHAT_TABS = new Set(["guidelines", "skills", "docs", "custom"]);
// kind 与后端 CONTENT_KIND_LABELS 一致,用于专注频道的名称
const CONFIG_CHAT_TARGETS = {
  guidelines: { label: "准则文档", kind: "准则", action: "guideline.save" },
  skills: { label: "Skill", kind: "Skill", action: "skill.save" },
  docs: { label: "版本化文档", kind: "文档", action: "document.publish" },
  custom: { label: "自定义面板", kind: "面板", action: "dashboard.save" },
};
const CONFIG_FIELD_LABELS = {
  "gf-content": "准则 Markdown 文件",
  "sf-id": "Skill id", "sf-content": "SKILL.md 文件",
  "doc-new-path": "文档路径", "doc-content": "文档正文",
};
let configChatSelection = null;
let configChatPolling = false;
/* 页面对话的三层状态:
   configChatBindings  contextKey → 当前条目专注频道的查找结果(是否已存在、id、归档)
   configChatChoices   contextKey → 用户在频道选择器里的选择(CONFIG_CHAT_FOCUSED 或频道 id)
   configChatThreads   channelId  → 该频道在页面对话里的消息缓存与运行卡片 */
const CONFIG_CHAT_FOCUSED = "focused";
const CONFIG_CHAT_TARGET_KEY = "mc.configChatTarget";   // 每个项目最近一次明确选择的目标
const configChatBindings = new Map();
const configChatChoices = new Map();
const configChatThreads = new Map();
const configChatResolving = new Map();
let configChatUploads = [];   // 待随下一条页面消息上传的本地文件(发送时才上传到目标频道)
let configChatSending = false;   // 发送进行中:轮询不要覆盖"正在发送…"状态
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
  toggle.setAttribute("aria-label", configChatCollapsed ? "展开页面对话" : "收起页面对话");
  toggle.title = configChatCollapsed ? "展开页面对话" : "收起页面对话";
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
  let contentKey = null;
  if (currentTab === "guidelines") {
    const guideline = project.guidelines?.find(value => value.name === selectedGuidelineName);
    const draftName = guidelineFrontmatterValue(valueOf("gf-content"), "name");
    item = guideline ? `${guideline.name}（Markdown 文件${guidelineViewer.viewingRevision
      ? `，历史版本 ${guidelineViewer.viewingRevision.slice(0, 10)}` : ""}）`
                     : `新建准则（${draftName || "name 未填写"}，未保存）`;
    itemKey = guideline?.name || "new";
    contentKey = guideline?.name || null;
  } else if (currentTab === "skills") {
    const skill = project.skills?.find(value => value.id === selectedSkillId);
    const draftName = guidelineFrontmatterValue(valueOf("sf-content"), "name");
    const draftId = skill ? skill.id : valueOf("sf-id").trim();
    item = skill ? `${draftName || skill.name || skill.id}（id: ${skill.id}${
      skillViewingRevision ? `，历史版本 ${skillViewingRevision.slice(0, 10)}` : ""}）`
                 : `新建 Skill（${draftName || draftId || "未命名"}，未保存）`;
    itemKey = skill?.id || "new";
    contentKey = skill?.id || null;
  } else if (currentTab === "docs") {
    const draftPath = document.getElementById("doc-new-path")?.value.trim();
    item = docMode === "new" ? `新建文档（${draftPath || "路径未填写"}）`
                             : (docSelected || "未选择文档");
    itemKey = docMode === "new" ? "new" : (docSelected || "none");
    contentKey = docMode === "new" ? null : docSelected;
  } else if (currentTab === "custom") {
    // 面板对话只在编辑模式出现;taskboard 面板直接改表达式,不经主控
    const board = projBoards().find(value => value.id === currentCustomBoard);
    if (!boardEditMode || !board || board.kind === "taskboard") return null;
    const shortId = board.id.replace(`${project.id}:`, "");
    item = `${board.name || shortId}（id: ${shortId}）`;
    itemKey = shortId;
    contentKey = shortId;
  }
  return {
    ...target, tab: currentTab, item, contentKey,
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
  if (context.tab === "custom") {
    const board = projBoards().find(value => value.id === currentCustomBoard);
    if (!board) return null;
    return {
      board_id: board.id.replace(`${currentProject}:`, ""),
      name: board.name,
      description: board.description || "",
      layout: board.layout || [],
      unsaved_changes: customBoardEditing,
    };
  }
  if (context.tab === "skills") return {
    id: selectedSkillId ?? valueOf("sf-id").trim(),
    markdown: clippedConfigText(valueOf("sf-content")).text,
    frontmatter_contract: {
      required_attributes: ["name", "description"],
      source_of_truth: "后端直接从这份 SKILL.md frontmatter 读取 name、description；其他附加属性原样保留",
    },
    enabled: document.getElementById("sf-enabled")?.classList.contains("on") ?? true,
    unsaved_changes: configEditorDirty.skills,
    viewing_revision: skillViewingRevision,
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

// 正文快照写进目标频道的共享目录,频道内任何角色都能按路径读取
async function stageConfigPage(channel, context) {
  const snapshot = configPageSnapshot(context);
  if (!snapshot) throw new Error("当前页面还没有可提供给 Agent 的文件信息");
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

function openConfigChatChannel() {
  const context = configChatContext();
  const channelId = context ? configChatTargetChannelId(context) : null;
  if (channelId) selectChannel(channelId);
}

async function restoreConfigChatChannel() {
  const context = configChatContext();
  const channelId = context ? configChatTargetChannelId(context) : null;
  if (!channelId) return;
  await api("POST", `/api/chat/channels/${encodeURIComponent(channelId)}/restore`);
  setConfigChatChannelArchived(channelId, false);
  await loadOverview();
  updateConfigChatContext();
  toast("频道已恢复", "success");
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

const CONFIG_CHAT_RELOOKUP_MS = 10000;   // 专注频道尚不存在时的复查间隔

function configChatBinding(context, create = false) {
  let binding = configChatBindings.get(context.key);
  if (!binding && create) {
    binding = { channelId: null, resolved: false, missing: false, archived: false,
                checkedAt: 0 };
    configChatBindings.set(context.key, binding);
  }
  return binding;
}

function configChatThread(channelId, create = false) {
  let thread = configChatThreads.get(channelId);
  if (!thread && create) {
    thread = {
      cursor: 0, entries: [], runs: [], activeRuns: [],
      runCards: new Map(), lastMsgDate: "", lastRenderedId: 0,
      running: false, loaded: false, archived: false,
      refreshedAfterReply: true, notice: "",
    };
    configChatThreads.set(channelId, thread);
  }
  return thread;
}

// 条目被删除时忘掉它的专注频道绑定与缓存(频道本身由后端一并清空)
function forgetConfigChatBinding(context) {
  const binding = configChatBindings.get(context.key);
  configChatBindings.delete(context.key);
  configChatChoices.delete(context.key);
  if (binding?.channelId) configChatThreads.delete(binding.channelId);
}

function upsertOverviewChannel(channel) {
  const index = overview.channels.findIndex(item => item.id === channel.id);
  if (index >= 0) overview.channels[index] = { ...overview.channels[index], ...channel };
  else overview.channels.push(channel);
  renderSidebar();
}

function setConfigChatChannelArchived(channelId, archived) {
  const thread = configChatThreads.get(channelId);
  if (thread) thread.archived = archived;
  for (const binding of configChatBindings.values()) {
    if (binding.channelId === channelId) binding.archived = archived;
  }
  if (configChatContext()) updateConfigChatContext();
}

function resetConfigChatChannel(channelId) {
  configChatThreads.delete(channelId);
  for (const [key, binding] of configChatBindings.entries()) {
    if (binding.channelId === channelId) configChatBindings.delete(key);
  }
  if (configChatContext()) updateConfigChatContext();
}

// 只读查找当前条目的专注频道;refresh=true 时即使上次没找到也隔一段时间再查,
// 使其他页面首次发起的对话能同步回来,又不至于每轮轮询都打一次 404
async function resolveConfigChatChannel(context, refresh = false) {
  if (!context?.contentKey) return null;
  const binding = configChatBinding(context, true);
  if (binding.channelId) return binding;
  if (binding.resolved
      && (!refresh || Date.now() - binding.checkedAt < CONFIG_CHAT_RELOOKUP_MS))
    return binding;
  const resolvingKey = `${context.key}:lookup`;
  if (configChatResolving.has(resolvingKey))
    return configChatResolving.get(resolvingKey);
  const request = (async () => {
    const query = new URLSearchParams({
      content_kind: context.tab, content_key: context.contentKey,
    });
    const response = await fetch(
      `/api/projects/${encodeURIComponent(currentProject)}/content-channel?${query}`);
    binding.checkedAt = Date.now();
    if (response.status === 404) {
      binding.resolved = true;
      binding.missing = true;
      return binding;
    }
    if (!response.ok) {
      let detail = "专注频道连接失败";
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    const channel = await response.json();
    binding.resolved = true;
    binding.channelId = channel.id;
    binding.missing = false;
    binding.archived = Boolean(channel.archived);
    upsertOverviewChannel(channel);
    return binding;
  })();
  configChatResolving.set(resolvingKey, request);
  try { return await request; }
  finally {
    if (configChatResolving.get(resolvingKey) === request)
      configChatResolving.delete(resolvingKey);
  }
}

async function ensureConfigChatChannel(context) {
  const binding = await resolveConfigChatChannel(context);
  if (!binding || binding.channelId) return binding;
  const resolvingKey = `${context.key}:create`;
  if (configChatResolving.has(resolvingKey))
    return configChatResolving.get(resolvingKey);
  const request = (async () => {
    const channel = await api(
      "POST", `/api/projects/${encodeURIComponent(currentProject)}/content-channel`, {
        content_kind: context.tab,
        content_key: context.contentKey,
        label: context.item.replace(/（.*$/, ""),
      });
    binding.channelId = channel.id;
    binding.resolved = true;
    binding.missing = false;
    binding.archived = Boolean(channel.archived);
    upsertOverviewChannel(channel);
    return binding;
  })();
  configChatResolving.set(resolvingKey, request);
  try { return await request; }
  finally {
    if (configChatResolving.get(resolvingKey) === request)
      configChatResolving.delete(resolvingKey);
  }
}

/* ---- 频道选择器 ----
   第一项始终是当前条目的专注频道(已存在则沿用,不存在则发送时新建),其后是项目内
   所有活跃频道;projChannels() 只含当前侧栏筛选范围,筛选"已归档"时只剩专注频道。 */
function configChatChannelChoices(context) {
  const binding = configChatBinding(context);
  const focusedId = binding?.channelId || null;
  const focusedChannel = focusedId
    ? overview.channels.find(channel => channel.id === focusedId) : null;
  const title = context.item.replace(/（.*$/, "");
  const archived = Boolean(focusedChannel ? focusedChannel.archived : binding?.archived);
  let label;
  if (!context.contentKey) label = "新建专注频道（请先保存当前条目）";
  else if (focusedId) label = `专注频道 · ${focusedChannel?.name || `${context.kind} · ${title}`}${
    archived ? "（已归档）" : ""}`;
  else label = `＋ 新建专注频道（${context.kind} · ${title}）`;
  const focused = { value: CONFIG_CHAT_FOCUSED, label, disabled: !context.contentKey };
  const others = projChannels()
    .filter(channel => !channel.archived && channel.id !== focusedId)
    .map(channel => ({ value: channel.id, label: `# ${channel.name || channel.id}`,
                       disabled: false }));
  return [focused, ...others];
}

/* 目标优先级:本次会话对该条目的明确选择 > 已存在的专注频道 > 本项目最近一次明确
   选择 > 聊天页当前所在频道 > general > 新建专注频道。 */
function configChatTarget(context, choices = configChatChannelChoices(context)) {
  const usable = value => Boolean(value)
    && choices.some(choice => choice.value === value && !choice.disabled);
  const chosen = configChatChoices.get(context.key);
  if (usable(chosen)) return chosen;
  if (configChatBinding(context)?.channelId && usable(CONFIG_CHAT_FOCUSED))
    return CONFIG_CHAT_FOCUSED;
  let preferred = null;
  try { preferred = localStorage.getItem(`${CONFIG_CHAT_TARGET_KEY}:${currentProject}`); }
  catch (_) { /* 无本地存储时忽略 */ }
  if (usable(preferred)) return preferred;
  if (usable(currentChan)) return currentChan;
  const general = projChannels().find(channel => channelIsGeneral(channel) && !channel.archived);
  if (general && usable(general.id)) return general.id;
  return choices.find(choice => !choice.disabled)?.value || null;
}

// 目标对应的频道 id;专注频道尚未创建时为 null(发送时才创建)
function configChatTargetChannelId(context, target = configChatTarget(context)) {
  if (!target) return null;
  if (target === CONFIG_CHAT_FOCUSED) return configChatBinding(context)?.channelId || null;
  return target;
}

function chooseConfigChatChannel(value) {
  const context = configChatContext();
  if (!context || !value) return;
  configChatChoices.set(context.key, value);
  try { localStorage.setItem(`${CONFIG_CHAT_TARGET_KEY}:${currentProject}`, value); }
  catch (_) { /* 无本地存储时忽略 */ }
  updateConfigChatContext();
  void pollConfigChat();
}

function syncConfigChatChannelSelect(context) {
  const select = document.getElementById("config-chat-channel");
  const choices = configChatChannelChoices(context);
  const target = configChatTarget(context, choices);
  if (!select) return target;
  // 轮询频繁重绘会打断展开中的下拉框,内容不变就不动 DOM
  const signature = JSON.stringify([choices, target]);
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.innerHTML = choices.map(choice =>
      `<option value="${esc(choice.value)}"${choice.disabled ? " disabled" : ""}${
        choice.value === target ? " selected" : ""}>${esc(choice.label)}</option>`).join("");
    select.value = target || "";
    select.disabled = !target;
  }
  return target;
}

function findConfigChatRunCard(runId) {
  for (const thread of configChatThreads.values()) {
    const card = thread.runCards.get(runId);
    if (card) return card;
  }
  return null;
}

function renderConfigChatThread(context, channelId, target) {
  const root = document.getElementById("config-chat-thread");
  if (!root) return;
  const threadKey = channelId || `${context.key}:${target || "none"}`;
  const thread = channelId ? configChatThread(channelId, true) : null;
  if (root.dataset.threadKey !== threadKey) {
    root.replaceChildren();
    root.dataset.threadKey = threadKey;
    if (thread) {
      thread.lastMsgDate = "";
      thread.lastRenderedId = 0;
      thread.runCards.clear();
    }
  }
  if (!thread) {
    const binding = configChatBinding(context);
    root.classList.remove("has-messages");
    root.innerHTML = `<div class="chat-empty empty">${
      !context.contentKey ? "请先保存当前条目；发送第一条消息时会创建专注频道。"
      : binding?.resolved ? "发送第一条消息时会创建专注频道。"
      : "正在查找专注频道…"}</div>`;
    return;
  }
  const pending = thread.entries.filter(entry => entry.id > thread.lastRenderedId);
  if (pending.length) {
    const surface = {
      pane: root, channelId,
      lastMsgDate: thread.lastMsgDate, lastMsgId: thread.lastRenderedId,
    };
    appendMessagesToSurface(pending, surface);
    thread.lastMsgDate = surface.lastMsgDate;
    thread.lastRenderedId = surface.lastMsgId;
  }
  syncRuns(thread.runs, { pane: root, runCards: thread.runCards });
  root.classList.toggle("has-messages", Boolean(thread.entries.length));
  if (!thread.entries.length) {
    const hint = thread.loaded ? "此频道还没有消息。" : "正在加载频道记录…";
    const existing = root.querySelector(".chat-empty");
    if (!existing) root.innerHTML = `<div class="chat-empty empty">${hint}</div>`;
    else if (existing.textContent !== hint) existing.textContent = hint;
  }
}

/* ---- 附件:与频道输入框同一套附件条,但目标频道要到发送时才确定,
   所以这里只暂存本地文件,发送时再上传到所选频道 ---- */
function configChatOnFiles(files) {
  if (!configChatContext() || !files.length) return false;
  for (const file of files) {
    const isImage = String(file.type || "").startsWith("image/");
    configChatUploads.push({
      file, name: file.name || "pasted-image.png", display: file.name || "粘贴的图片",
      is_image: isImage, url: isImage ? URL.createObjectURL(file) : "",
    });
  }
  renderConfigChatUploads();
  return true;
}

function handleConfigChatFileInput(input) {
  configChatOnFiles([...input.files]);
  input.value = "";
}

function removeConfigChatUpload(index) {
  const [item] = configChatUploads.splice(index, 1);
  if (item?.url) URL.revokeObjectURL(item.url);
  renderConfigChatUploads();
}

function clearConfigChatUploads() {
  for (const item of configChatUploads) if (item.url) URL.revokeObjectURL(item.url);
  configChatUploads = [];
  renderConfigChatUploads();
}

function renderConfigChatUploads() {
  const wrap = document.getElementById("config-chat-attachments");
  if (!wrap) return;
  wrap.hidden = !configChatUploads.length;
  wrap.innerHTML = attachmentChipsHtml(configChatUploads, "removeConfigChatUpload");
}

// "@" 按钮:不用手输 @ 也能从列表选角色,插入位置沿用输入框光标
function openConfigChatMentionPicker() {
  const box = document.getElementById("config-chat-input");
  if (!box || box.getAttribute("aria-disabled") === "true") return;
  box.focus();
  activateComposer("config-chat-input", "config-chat-picker");
  mentionPickerIndex = 0;
  renderMentionPicker("");
}

function updateConfigChatContext() {
  const panel = document.getElementById("config-chat");
  if (!panel) return;
  const context = configChatContext();
  panel.classList.toggle("visible", Boolean(context));
  applyConfigChatLayout();
  if (!context) return;
  if (configChatSelection?.context_key !== context.key) configChatSelection = null;
  const peer = peerModeProject();
  const target = syncConfigChatChannelSelect(context);
  const channelId = configChatTargetChannelId(context, target);
  const binding = configChatBinding(context);
  const thread = channelId ? configChatThread(channelId) : null;
  const channel = channelId
    ? overview.channels.find(item => item.id === channelId) : null;
  const archived = Boolean(channel ? channel.archived
    : (thread?.archived || (target === CONFIG_CHAT_FOCUSED && binding?.archived)));
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
    selection.textContent = `非文本文件；接收方将收到文件名与文档路径：${docSelected}`;
    selection.classList.remove("has-selection");
    clear.style.display = "none";
  } else {
    selection.textContent = "未选择文本；接收方仍会收到当前页面与当前对象。";
    selection.classList.remove("has-selection");
    clear.style.display = "none";
  }
  const input = document.getElementById("config-chat-input");
  input.dataset.placeholder = peer
    ? `询问或修改${context.label}「${context.item}」；本项目没有主控，输入 @ 至少选择一个角色…（Enter 发送，Shift+Enter 换行）`
    : `询问或修改${context.label}「${context.item}」；输入 @ 可指定角色，不指定则交给主控…（Enter 发送，Shift+Enter 换行）`;
  document.getElementById("config-chat-open-channel").style.display =
    channelId ? "inline-block" : "none";
  document.getElementById("config-chat-restore").hidden = !(channelId && archived);
  const disabled = !target || archived;
  input.contentEditable = String(!disabled);
  input.setAttribute("aria-disabled", String(disabled));
  document.querySelector("#config-chat .config-chat-compose .send").disabled = disabled;
  document.querySelectorAll("#config-chat .config-chat-tool")
    .forEach(button => { button.disabled = disabled; });
  const status = document.getElementById("config-chat-status");
  if (configChatSending) {
    /* 发送过程中的状态由 sendConfigChat 维护 */
  } else if (!target) {
    status.textContent = "请先保存当前条目；发送第一条消息时会创建专注频道。";
  } else if (archived) {
    status.textContent = "频道已归档；恢复后才能继续对话。";
  } else if (thread?.running) {
    status.textContent = "Agent 正在处理…";
  } else if (target === CONFIG_CHAT_FOCUSED && !channelId) {
    status.textContent = binding?.resolved ? "发送第一条消息时会创建专注频道。" : "正在查找专注频道…";
  } else if (thread) {
    status.textContent = thread.notice
      || (thread.entries.some(entry => entry.author_type === "agent") ? "Agent 已回复。" : "");
  }
  renderConfigChatThread(context, channelId, target);
  // 专注频道的存在与否决定选择器首项文案,所以无论当前目标是什么都先查一次
  if (context.contentKey && !binding?.resolved) {
    void resolveConfigChatChannel(context).then(resolved => {
      if (configChatContext()?.key !== context.key) return null;
      return resolved?.channelId ? pollConfigChat() : updateConfigChatContext();
    }).catch(error => {
      if (configChatContext()?.key === context.key)
        status.textContent = error.message || "专注频道连接失败。";
    });
  }
}

// 把选择器目标落实为可发送的频道:专注频道不存在时在此创建;归档频道拒绝发送
async function resolveConfigChatTargetChannel(context, target) {
  if (target === CONFIG_CHAT_FOCUSED) {
    const binding = await ensureConfigChatChannel(context);
    const channel = binding?.channelId
      ? overview.channels.find(item => item.id === binding.channelId) : null;
    if (!channel) throw new Error("专注频道尚未就绪");
    if (channel.archived || binding.archived) throw new Error("专注频道已归档；请先恢复后再继续对话。");
    return channel;
  }
  const channel = projChannels().find(item => item.id === target);
  if (!channel) throw new Error("所选频道不存在，请重新选择。");
  if (channel.archived) throw new Error("所选频道已归档，请先恢复后再发送。");
  return channel;
}

/* 发送与频道输入框同一套交付:结构化 @ 提及决定接收者(不 @ 则由后端交给主控,
   无主控项目不 @ 就不触发),附件路径追加在正文尾部;页面身份、正文快照与选区
   放在 context.page_collaboration 里随消息带给接收方。 */
async function sendConfigChat() {
  const project = projObj();
  const context = configChatContext();
  const box = document.getElementById("config-chat-input");
  const status = document.getElementById("config-chat-status");
  const payload = composerPayload(box);
  const { mentions } = payload;
  const files = configChatUploads.slice();
  if (!project || !context) {
    status.textContent = project ? "当前页面没有可对话的对象。" : "请先选择项目。";
    return;
  }
  if (!payload.content && !files.length) {
    status.textContent = "请输入要询问或修改的内容。";
    return;
  }
  const target = configChatTarget(context);
  if (!target) {
    status.textContent = "请先保存当前条目，再开始页面内对话。";
    return;
  }
  const selection = configChatSelection?.context_key === context.key
    ? configChatSelection : null;
  const peer = peerModeProject();
  configChatSending = true;
  status.textContent = "正在发送…";
  box.replaceChildren();
  configChatUploads = [];
  renderConfigChatUploads();
  savedComposerRange = null;
  hideMentionPicker();
  try {
    const channel = await resolveConfigChatTargetChannel(context, target);
    const attachments = [];
    for (const item of files) {
      const stored = await uploadChatFile(item.file, channel.id);
      if (!stored) throw new Error(`附件上传失败：${item.display}`);
      attachments.push(stored);
    }
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
    const pagePayload = {
      page_kind: context.tab, page_label: context.label, current_item: context.item,
      ...(fileBackedPage ? { current_page: currentPage }
                         : { current_draft: currentConfigDraft(context) }),
      selection: selectionPayload, user_message: payload.content,
    };
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
    const dashboardEditingTip = context.tab === "custom"
      ? `当前编辑对象是自定义面板，现有布局在 current_draft.layout（组件数组）。` +
        `修改时用 ${context.action} 传 id=current_draft.board_id、mode=update 和修改后的完整 layout；` +
        `不要新建面板或改动其他面板。`
      : "";
    const instructions =
      `这是围绕当前页面的对话：若用户只是提问、解释或讨论，只需回答，不要写入；` +
      `若用户明确要求创建或修改，则使用 ${context.action} 控制动作实际保存完整结果。` +
      `优先处理 selection 指定的字段和行；修改现有条目时沿用当前 name、id、match 或路径。` +
      guidelineEditingTip + documentEditingTip + skillEditingTip + dashboardEditingTip;
    // 附件路径追加在正文尾部,不影响前面提及范围的字符偏移
    let content = payload.content;
    if (attachments.length)
      content = (content ? content + "\n\n" : "") + attachmentBlock(attachments);
    const messageContext = { page_collaboration: { ...pagePayload, instructions } };
    if (attachments.length) messageContext.attachments = attachments;
    await api("POST", `/api/chat/${encodeURIComponent(channel.id)}/messages`, {
      author: "human", content, mentions, context: messageContext,
    });
    const thread = configChatThread(channel.id, true);
    thread.running = Boolean(mentions.length) || !peer;
    thread.refreshedAfterReply = false;
    const named = [...new Set(mentions.map(item => `@${item.role_id}`))];
    thread.notice = named.length
      ? `已发送给 ${named.join("、")}${named.length > 1 && !peer ? "（由主控协调）" : ""}；回复会显示在此处。`
      : peer ? "已发送；本项目没有主控，未 @ 角色的消息不会触发执行。"
             : "已发送给项目主控；回复会显示在此处。";
    status.textContent = thread.notice;
    configChatSending = false;
    if (currentChan === channel.id) pollMessages();
    await pollConfigChat();
  } catch (error) {
    // 发送失败:附件退回待发区,还原结构化提及而不降级成文本
    configChatSending = false;
    configChatUploads = files;
    renderConfigChatUploads();
    restoreComposerPayload(payload.content, mentions, box);
    status.textContent = error.message || "发送失败，请重试。";
  }
}

async function pollConfigChat() {
  if (configChatPolling) return;
  const context = configChatContext();
  if (!context) return;
  configChatPolling = true;
  try {
    // 专注频道尚未创建时也定期只读查找,使其他页面首次发起的对话能同步回来
    if (context.contentKey) await resolveConfigChatChannel(context, true);
    const target = configChatTarget(context);
    const channelId = configChatTargetChannelId(context, target);
    if (!channelId) {
      if (configChatContext()?.key === context.key) updateConfigChatContext();
      return;
    }
    const thread = configChatThread(channelId, true);
    // 首屏直接定位频道末尾一页(活跃频道历史可能很长),之后按 after_id 增量
    const query = thread.loaded ? `after_id=${thread.cursor}` : "tail=true";
    const response = await fetch(
      `/api/chat/${encodeURIComponent(channelId)}/messages?${query}`);
    if (!response.ok) return;
    const data = await response.json();
    thread.archived = Boolean(data.channel?.archived);
    const channel = overview.channels.find(item => item.id === channelId);
    if (channel && data.channel) {
      const lastMessageAt = channel.last_message_at;
      Object.assign(channel, data.channel);
      channel.last_message_at = Math.max(Number(lastMessageAt || 0),
                                         Number(data.channel.last_message_at || 0));
    }
    let agentReplied = false;
    for (const message of data.messages || []) {
      thread.cursor = Math.max(thread.cursor, message.id);
      if (thread.entries.some(entry => entry.id === message.id)) continue;
      thread.entries.push(message);
      if (message.author_type === "agent") agentReplied = true;
    }
    if (agentReplied) thread.notice = "";
    thread.runs = data.runs || [];
    thread.activeRuns = data.active_runs || [];
    thread.running = thread.activeRuns.some(run =>
      ["queued", "running", "waiting_user"].includes(run.status));
    thread.loaded = true;
    if (setChannelRunningCount(channelId, thread.activeRuns.length))
      refreshChannelRunningMarkers();
    if (configChatContext()?.key === context.key) updateConfigChatContext();
    // 本页发出的消息得到回复后刷新一次总览,让保存的准则/文档/Skill 立即出现
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
// 与频道输入框共用同一套提及选择器、Enter 发送、粘贴与拖入附件逻辑
bindComposerEvents("config-chat-input", "config-chat-picker", () => sendConfigChat(),
                   configChatOnFiles);
bindDropZone("config-chat-input-wrap", configChatOnFiles);
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

// 总览只带准则元信息与内容指纹,正文在进入查看/编辑时按需拉取。
// 404(已被他处删除)回退占位内容;其他失败抛错,避免用占位文本覆盖真实正文。
async function fetchGuidelineMarkdown() {
  if (!currentProject || !selectedGuidelineName) return null;
  const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}` +
    `/guidelines/${encodeURIComponent(selectedGuidelineName)}`);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`准则读取失败 (${r.status})`);
  return (await r.json()).markdown || null;
}

const guidelineViewer = createTextViewer({
  containerId: "guideline-editor",
  textareaId: "gf-content",
  editorClass: "guideline-markdown-editor",
  previewClass: "guideline-markdown-preview",
  surfaceClass: "guideline-markdown-surface",
  identity: () => JSON.stringify(
    [currentProject, selectedGuidelineName, guidelineViewer.viewingRevision]),
  // 总览里的内容指纹作版本标记:准则在别处被改写后按新指纹重新读取正文
  version: () => guidelineViewer.viewingRevision
    ? null : (selectedGuideline()?.markdown_fingerprint ?? null),
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
             content: (await fetchGuidelineMarkdown()) || GUIDELINE_MARKDOWN_PLACEHOLDER };
  },
  loadEdit: async () =>
    (await fetchGuidelineMarkdown()) || GUIDELINE_MARKDOWN_PLACEHOLDER,
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
  if (!await uiConfirm(
      `将准则文档「${name}」移入项目回收站，并永久清空它的页面对话？`)) return;
  const threadKey = configChatContext()?.key;
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/guidelines/${encodeURIComponent(name)}`);
  if (threadKey) configChatThreads.delete(threadKey);
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
let skillViewingRevision = null;
let skillHistoricalPackage = null;
let skillHistoryOpen = false;
let skillHistoryRows = null;
let skillCompareRevisions = [];
let skillCompareResult = null;

function skillMetadataSignature(skills) {
  // 总览与 skills/library 都带 instructions_fingerprint,以指纹代替全文比对
  return JSON.stringify((skills || []).map(skill => ({
    id: skill.id, name: skill.name, description: skill.description,
    instructions_fingerprint: skill.instructions_fingerprint, enabled: skill.enabled,
  })));
}

function skillEditorStateSignature(skill, packageInfo) {
  return JSON.stringify([
    currentProject, selectedSkillId, skill || null,
    packageInfo ? packageInfo.content_version : null, skillViewingRevision,
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
  if (skillViewingRevision) return skillHistoricalPackage;
  return skillLibraryInfo?.project_id === currentProject
    ? (skillLibraryInfo.skills || []).find(item => item.id === selectedSkillId) : null;
}

function resetSkillHistoryState() {
  skillViewingRevision = null;
  skillHistoricalPackage = null;
  skillHistoryOpen = false;
  skillHistoryRows = null;
  skillCompareRevisions = [];
  skillCompareResult = null;
}

function skillHistoryPanelHtml() {
  if (!skillHistoryOpen) return "";
  if (skillCompareResult) {
    if (skillCompareResult.loading)
      return `<section class="doc-compare-panel"><div class="empty">正在比较 Skill 包…</div></section>`;
    const result = skillCompareResult;
    const statusLabels = { added: "新增", deleted: "删除", modified: "修改" };
    const changes = result.file_changes || [];
    return `<section class="doc-compare-panel">
      <div class="doc-compare-head"><strong>Skill 包版本比较</strong>
        <code>A ${esc(result.from_revision.slice(0, 10))}</code><span>→</span>
        <code>B ${esc(result.to_revision.slice(0, 10))}</code>
        <span class="st-done">+${result.additions}</span>
        <span class="st-failed">−${result.deletions}</span>
        <span class="guideline-toolbar-spacer"></span>
        <button class="ghost compact" type="button" onclick="closeSkillCompare()">退出比较</button>
      </div>
      <div class="skill-version-file-changes">${changes.length
        ? changes.map(item => `<code class="skill-version-change ${esc(item.status)}">${
            esc(statusLabels[item.status] || item.status)} ${esc(item.path)}</code>`).join("")
        : `<span class="muted">文件内容和权限均相同。</span>`}</div>
      ${result.identical
        ? `<div class="empty">两个版本的 Skill 包完全相同。</div>`
        : viewerInlineDiffHtml(result.ops)}
    </section>`;
  }
  if (skillHistoryRows === null)
    return `<section class="doc-history-panel"><div class="empty">正在读取版本历史…</div></section>`;
  if (!skillHistoryRows.length)
    return `<section class="doc-history-panel"><div class="empty">暂无版本历史。</div></section>`;
  const full = skillCompareRevisions.length >= 2;
  return `<section class="doc-history-panel">
    <div class="doc-history-head"><h3>版本历史</h3>
      <span class="muted">每个版本包含 SKILL.md、scripts、references、assets 等完整目录内容</span>
      <span class="guideline-toolbar-spacer"></span>
      <button class="action compact" type="button" onclick="runSkillCompare()"
        ${skillCompareRevisions.length === 2 ? "" : "disabled"}>
        比较已选版本 (${skillCompareRevisions.length}/2)</button>
    </div>
    <div class="doc-history-table-wrap"><table>
      <tr><th>比较</th><th>版本</th><th>时间</th><th>作者</th><th>说明</th><th></th></tr>
      ${skillHistoryRows.map((row, index) => {
        const slot = skillCompareRevisions.indexOf(row.revision);
        const checked = slot >= 0;
        return `<tr class="${skillViewingRevision === row.revision ? "selected" : ""}">
          <td class="doc-compare-choice"><input type="checkbox"
            data-rev="${esc(row.revision)}" onchange="toggleSkillCompareRevision(this.dataset.rev)"
            aria-label="选择版本 ${esc(row.revision.slice(0, 10))} 进行比较"
            ${checked ? "checked" : ""} ${full && !checked ? "disabled" : ""}>
            ${checked ? `<span class="doc-compare-slot">${slot === 0 ? "A" : "B"}</span>` : ""}</td>
          <td><code>${esc(row.revision.slice(0, 10))}</code>
            ${index === 0 ? `<span class="pill st-done">最新</span>` : ""}</td>
          <td>${new Date(row.created_at * 1000).toLocaleString()}</td>
          <td>${esc(row.actor)}</td><td>${esc(row.message)}</td><td>
            <button class="ghost compact" type="button" data-rev="${esc(row.revision)}"
              onclick="viewSkillRevision(this.dataset.rev)">查看</button>
            <button class="ghost compact" type="button" data-rev="${esc(row.revision)}"
              onclick="restoreSkillRevision(this.dataset.rev)">恢复</button>
          </td></tr>`;
      }).join("")}
    </table></div>
  </section>`;
}

async function loadSkillHistory() {
  if (!currentProject || !selectedSkillId) return;
  const projectId = currentProject;
  const skillId = selectedSkillId;
  try {
    const rows = await api("GET", `/api/projects/${encodeURIComponent(projectId)}/skills/` +
      `${encodeURIComponent(skillId)}/history`);
    if (projectId !== currentProject || skillId !== selectedSkillId) return;
    skillHistoryRows = rows;
    renderSkillsPage(true);
  } catch (_) { /* api() 已显示错误 */ }
}

function toggleSkillHistory() {
  skillHistoryOpen = !skillHistoryOpen;
  skillCompareResult = null;
  renderSkillsPage(true);
  if (skillHistoryOpen && skillHistoryRows === null) void loadSkillHistory();
}

function toggleSkillCompareRevision(revision) {
  const index = skillCompareRevisions.indexOf(revision);
  if (index >= 0) skillCompareRevisions.splice(index, 1);
  else if (skillCompareRevisions.length < 2) skillCompareRevisions.push(revision);
  renderSkillsPage(true);
}

async function runSkillCompare() {
  if (skillCompareRevisions.length !== 2 || !currentProject || !selectedSkillId) return;
  const projectId = currentProject;
  const skillId = selectedSkillId;
  const [fromRevision, toRevision] = skillCompareRevisions;
  skillCompareResult = { loading: true };
  renderSkillsPage(true);
  try {
    const result = await api("POST", `/api/projects/${encodeURIComponent(projectId)}/skills/` +
      `${encodeURIComponent(skillId)}/compare`, {
        from_revision: fromRevision, to_revision: toRevision,
      });
    if (projectId !== currentProject || skillId !== selectedSkillId) return;
    skillCompareResult = result;
    renderSkillsPage(true);
  } catch (_) {
    if (projectId === currentProject && skillId === selectedSkillId) {
      skillCompareResult = null;
      renderSkillsPage(true);
    }
  }
}

function closeSkillCompare() {
  skillCompareResult = null;
  renderSkillsPage(true);
}

async function viewSkillRevision(revision) {
  if (!currentProject || !selectedSkillId) return;
  const projectId = currentProject;
  const skillId = selectedSkillId;
  const info = await api("GET", `/api/projects/${encodeURIComponent(projectId)}/skills/` +
    `${encodeURIComponent(skillId)}/history/${encodeURIComponent(revision)}`);
  if (projectId !== currentProject || skillId !== selectedSkillId) return;
  skillViewingRevision = info.revision;
  skillHistoricalPackage = info;
  skillOpenFile = null;
  skillMarkdownMode = "preview";
  configEditorDirty.skills = false;
  renderSkillsPage(true);
}

function closeSkillRevision() {
  skillViewingRevision = null;
  skillHistoricalPackage = null;
  skillOpenFile = null;
  renderSkillsPage(true);
}

async function restoreSkillRevision(revision) {
  if (!currentProject || !selectedSkillId) return;
  if (!await uiConfirm(
      `将 Skill「${selectedSkillId}」的完整目录恢复到版本 ${revision.slice(0, 10)}？`)) return;
  const projectId = currentProject;
  const skillId = selectedSkillId;
  await api("POST", `/api/projects/${encodeURIComponent(projectId)}/skills/` +
    `${encodeURIComponent(skillId)}/restore`, { revision });
  if (projectId !== currentProject || skillId !== selectedSkillId) return;
  resetSkillHistoryState();
  skillOpenFile = null;
  configEditorDirty.skills = false;
  await loadOverview();
  skillLibraryInfo = null;
  await loadSkillLibraryInfo();
  renderSkillsPage(true);
  toast("Skill 完整目录已恢复，并已生成新版本", "success");
}

function renderSkillEditor(skill = undefined, packageInfo = undefined, signature = undefined) {
  if (skill === undefined)
    skill = (projObj()?.skills || []).find(item => item.id === selectedSkillId);
  if (packageInfo === undefined) packageInfo = skill ? selectedSkillPackage() : null;
  const historical = Boolean(skillViewingRevision);
  if (!skill) skillMarkdownMode = "edit";
  if (historical) skillMarkdownMode = "preview";
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
          role="switch" tabindex="0" ${historical ? "aria-disabled=\"true\"" : ""}
          ${historical ? "" : `onclick="this.classList.toggle('on');markConfigDirty('skills')"`}></span></label>
      ${historical ? `<span class="guideline-history-badge">历史版本
        <code>${esc(skillViewingRevision.slice(0, 10))}</code></span>` : ""}
      <span class="guideline-toolbar-spacer"></span>
      ${skill ? `<button class="ghost compact ${skillFileTreeCollapsed ? "" : "active"}"
        id="skill-tree-toggle" type="button" onclick="toggleSkillFileTree()">文件树</button>` : ""}
      ${historical ? "" : `<div class="guideline-view-toggle" aria-label="Markdown 显示方式">
        <button class="ghost compact" id="skill-edit-button" type="button"
          onclick="setSkillMarkdownMode('edit')">编辑</button>
        <button class="ghost compact" id="skill-preview-button" type="button"
          onclick="setSkillMarkdownMode('preview')">预览</button>
      </div>`}
      ${historical ? `<button class="action compact" type="button"
          data-rev="${esc(skillViewingRevision)}"
          onclick="restoreSkillRevision(this.dataset.rev)">恢复此版本</button>
        <button class="ghost compact" type="button" onclick="closeSkillRevision()">返回最新</button>` : `
      <button class="action" type="button" ${loading ? "disabled" : ""}
        onclick="saveSkill()">保存</button>
      ${skill ? `<button class="danger" type="button" data-id="${esc(skill.id)}"
        onclick="deleteSkill(this.dataset.id)">删除</button>` : ""}`}
      ${skill ? `<button class="ghost compact" type="button" onclick="toggleSkillHistory()">
        ${skillHistoryOpen ? "收起历史" : "版本历史"}</button>` : ""}
    </div>
    ${skillHistoryPanelHtml()}
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
          spellcheck="false" ${historical ? "readonly" : ""}
          ${historical ? "" : `oninput="markConfigDirty('skills');updateSkillMarkdownPreview()"`}>${esc(skill ? packageInfo.markdown : SKILL_MARKDOWN_PLACEHOLDER)}</textarea>
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
  const pathLine = `<code class="skill-file-tree-path">${esc(
    packageInfo.path || `历史版本 ${packageInfo.revision?.slice(0, 10) || ""}`)}</code>`;
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
  if (skillViewingRevision && mode === "edit") mode = "preview";
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
  const revision = skillViewingRevision;
  skillOpenFile = path;
  const revisionQuery = revision ? `&revision=${encodeURIComponent(revision)}` : "";
  const data = await api("GET",
    `/api/projects/${encodeURIComponent(projectId)}/skills/` +
    `${encodeURIComponent(skillId)}/file?path=${encodeURIComponent(path)}${revisionQuery}`);
  if (projectId !== currentProject || skillId !== selectedSkillId
      || revision !== skillViewingRevision || skillOpenFile !== path) return;
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
  resetSkillHistoryState();
  skillOpenFile = null;
  skillMarkdownMode = id ? "preview" : "edit";
  configChatSelection = null;
  configEditorDirty.skills = false;
  if (currentTab !== "skills") switchTab("skills");
  else { renderSkillsPage(true); syncUrl(); }
}

async function saveSkill() {
  if (skillViewingRevision) return;
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
  resetSkillHistoryState();
  skillMarkdownMode = "preview";
  configEditorDirty.skills = false;
  await loadOverview();
  skillLibraryInfo = null;
  await loadSkillLibraryInfo();
  renderSkillsPage(true);
  toast("Skill 已保存", "success");
}

async function deleteSkill(id) {
  if (!await uiConfirm(
      `将 Skill「${id}」移入项目回收站，并永久清空它的页面对话？`)) return;
  const threadKey = configChatContext()?.key;
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/skills/${encodeURIComponent(id)}`);
  if (threadKey) configChatThreads.delete(threadKey);
  selectedSkillId = undefined;
  resetSkillHistoryState();
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
  resetSkillHistoryState();
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
  resetSkillHistoryState();
  skillLibraryInfo = { ...info, project_id: currentProject };
  await loadOverview();
  renderSkillsPage(true);
  toast(`已扫描 ${info.skills?.length || 0} 个 Skill`, "success");
}
