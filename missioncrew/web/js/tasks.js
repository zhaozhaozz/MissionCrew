/* ---------------- Issue 化 Task 看板 ---------------- */
const TASK_STATUS = {
  open: "待处理", in_progress: "处理中", blocked: "已阻塞", done: "已完成",
};
const COLS = [
  { title: "待处理", color: "var(--muted)", match: task => task.status === "open" },
  { title: "处理中", color: "var(--accent)", match: task => task.status === "in_progress" },
  { title: "已阻塞", color: "var(--bad)", match: task => task.status === "blocked" },
  { title: "已完成", color: "var(--ok)", match: task => task.status === "done" },
];
const TASK_FILTERS = new Set(["all", "active", "archived"]);
let taskFilter = localStorage.getItem("mc.taskFilter") || "active";
if (!TASK_FILTERS.has(taskFilter)) taskFilter = "active";
let currentTaskId = null;
let currentTaskDetail = null;
let editingTaskId = null;
let editingTaskSnapshot = null;

function closeTaskDialog(updateRoute = true) {
  currentTaskId = null;
  currentTaskDetail = null;
  if (dlg.open) dlg.close();
  if (updateRoute) syncUrl();
}

dlg.addEventListener("close", () => {
  if (!currentTaskId) return;
  currentTaskId = null;
  currentTaskDetail = null;
  syncUrl();
});

function taskChannelLabel(channelId) {
  const channel = overview.channels.find(item => item.id === channelId);
  return channel ? `#${channel.name || channel.id}` : channelId;
}

function visibleProjTasks() {
  return projTasks().filter(task =>
    taskFilter === "all"
      || (taskFilter === "archived" ? task.archived : !task.archived));
}

function renderTaskFilter() {
  const tasks = projTasks();
  const archived = tasks.filter(task => task.archived).length;
  const select = document.getElementById("task-filter");
  if (select) select.value = taskFilter;
  const summary = document.getElementById("task-filter-summary");
  if (summary) {
    const shown = visibleProjTasks().length;
    summary.textContent = `显示 ${shown} 个 · 活跃 ${tasks.length - archived} · 已归档 ${archived}`;
  }
}

function setTaskFilter(value) {
  if (!TASK_FILTERS.has(value)) return;
  taskFilter = value;
  localStorage.setItem("mc.taskFilter", value);
  renderBoard();
}

function taskCardHtml(task, { showStatus = false } = {}) {
  return `<div class="card ${task.archived ? "task-archived" : ""}"
      data-task-id="${esc(task.id)}" onclick="openTask(this.dataset.taskId)">
    <div class="title">${esc(task.title)}${showStatus
      ? ` <span class="pill st-${esc(task.status)}">${esc(TASK_STATUS[task.status] || task.status)}</span>` : ""}${task.archived
      ? '<span class="badge task-archived-badge">已归档</span>' : ""}</div>
    ${task.summary ? `<div class="task-card-summary">${esc(task.summary)}</div>` : ""}
    <div class="meta">更新 ${new Date(task.updated_at * 1000).toLocaleString()} ·
      ${esc(task.id)} · ${task.channel_ids.map(id => esc(taskChannelLabel(id))).join(" · ")}</div>
    <div class="meta">${task.labels.map(label => `<span class="badge">${esc(label)}</span>`).join("")}</div>
  </div>`;
}

function renderBoard() {
  // 看板横向滚动、每列内部纵向滚动:分别按列 key 保持重渲染前的滚动位置
  const board = document.getElementById("board");
  const outerScroll = captureScrollPositions(["#board"]);
  const columnScroll = captureKeyedScrollPositions(board);
  renderTaskFilter();
  const statusCols = COLS.map(col => {
    const items = visibleProjTasks().filter(col.match);
    const cards = items.map(task => taskCardHtml(task)).join("")
      || `<div class="empty" style="padding:6px 4px">暂无 Task</div>`;
    return `<section class="col"><h2><span class="col-dot" style="background:${col.color}"></span>
      ${col.title}<span class="col-count">${items.length}</span></h2>
      <div class="col-list" data-scroll-key="col:${esc(col.title)}">${cards}</div></section>`;
  });
  const labelCols = labelBoardEntries().map(entry => {
    const items = visibleProjTasks().filter(task => taskHasLabel(task, entry.label));
    const cards = items.map(task => taskCardHtml(task, { showStatus: true })).join("")
      || `<div class="empty" style="padding:6px 4px">暂无带此标签的 Task</div>`;
    const ruleState = entry.rule
      ? (entry.rule.enabled ? "rule-on" : "rule-off") : "";
    const ruleTitle = entry.rule
      ? (entry.rule.enabled ? "自动处理规则已启用,点击修改" : "自动处理规则已停用,点击修改")
      : "为该标签设置自动处理规则";
    const removeBtn = entry.rule ? "" : `
      <button class="col-tool" title="移除该标签看板(不影响 Task)"
        data-label="${esc(entry.label)}"
        onclick="removeLabelBoard(this.dataset.label)">✕</button>`;
    return `<section class="col col-label"><h2>
      <span class="badge">${esc(entry.label)}</span><span class="col-count">${items.length}</span>
      <button class="col-tool col-rule ${ruleState}" title="${esc(ruleTitle)}"
        data-label="${esc(entry.label)}"
        onclick="openLabelRule(this.dataset.label)">⚡</button>${removeBtn}</h2>
      <div class="col-list" data-scroll-key="col-label:${esc(entry.label)}">${cards}</div></section>`;
  });
  board.innerHTML = statusCols.concat(labelCols).join("");
  restoreScrollPositions(outerScroll);
  restoreKeyedScrollPositions(board, columnScroll);
}

async function openTask(id, updateRoute = true) {
  const detail = await api("GET", `/api/tasks/${id}`);
  const task = detail.task;
  if (!task || task.project_id !== currentProject) return;
  currentTaskId = task.id;
  currentTaskDetail = detail;
  document.getElementById("dlg-title").textContent = `${task.id} · ${task.title}`;
  const channels = detail.channels.map(channel =>
    `<a href="${esc(channel.resource_url || missionCrewResourceUrl(task.project_id, "channels", channel.id.replace(`${task.project_id}:`, "")))}"
        data-resource-link="${esc(channel.resource_url || missionCrewResourceUrl(task.project_id, "channels", channel.id.replace(`${task.project_id}:`, "")))}"
        onclick="return openMissionCrewResourceLink(event,this.dataset.resourceLink)">#${esc(channel.name || channel.id)}</a>`
  ).join(" · ");
  const briefs = detail.briefs.map(brief => `<article class="task-brief">
    <div class="task-brief-meta">
      <span class="pill st-${esc(brief.status)}">${esc(TASK_STATUS[brief.status] || brief.status)}</span>
      <span>${new Date(brief.created_at * 1000).toLocaleString()}</span>
      <span>${brief.author_type === "agent" ? "@" : ""}${esc(brief.author)}</span>
    </div>
    <div class="markdown-body">${miniMarkdown(brief.content)}</div>
  </article>`).join("");
  document.getElementById("dlg-body").innerHTML = `
    <div class="task-detail-meta">
      <span class="pill st-${esc(task.status)}">${esc(TASK_STATUS[task.status] || task.status)}</span>
      ${task.archived ? '<span class="badge task-archived-badge">已归档</span>' : ""}
      <span>${channels || "未绑定 Channel"}</span>
      ${task.labels.map(label => `<span class="badge">${esc(label)}</span>`).join("")}
    </div>
    <h3>简介</h3><div class="task-summary">${esc(task.summary) || "暂无简介"}</div>
    <h3>正文</h3>
    <article class="task-body markdown-body">${task.body ? markdownPreviewHtml(task.body, { showFrontmatter: false }) : '<span class="empty">暂无正文</span>'}</article>
    <h3>状态简报 (${detail.briefs.length})</h3>
    <div class="task-briefs">${briefs || '<div class="empty">暂无状态简报</div>'}</div>`;
  const actions = task.archived ? [
    `<button class="ghost" onclick="restoreTask('${task.id}')">恢复 Task</button>`,
    `<button class="danger" onclick="deleteTask('${task.id}')">删除</button>`,
  ] : [
    `<button class="ghost" onclick="openTaskEditor('${task.id}')">编辑</button>`,
    `<button class="ghost" onclick="openTaskBriefForm('${task.id}')">添加简报</button>`,
    `<button class="ghost" onclick="archiveTask('${task.id}')">归档</button>`,
    `<button class="danger" onclick="deleteTask('${task.id}')">删除</button>`,
  ];
  if (!task.archived && task.status !== "done")
    actions.push(
      `<button class="action" onclick="processTask('${task.id}')">交给主控处理</button>`);
  document.getElementById("dlg-actions").innerHTML = actions.join("");
  if (!dlg.open) dlg.showModal();
  if (updateRoute) syncUrl();
}

function renderTaskChannelOptions(selected = []) {
  const active = projChannels().filter(channel => !channel.archived);
  document.getElementById("nt-channels").innerHTML = active.map(channel => `
    <label><input type="checkbox" value="${esc(channel.id)}"
      ${selected.includes(channel.id) ? "checked" : ""}> #${esc(channel.name || channel.id)}</label>`
  ).join("") || '<span class="empty">当前项目没有可用 Channel</span>';
}

function openNewTask() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  editingTaskId = null;
  editingTaskSnapshot = null;
  document.getElementById("tdlg-title").textContent = "新建 Task";
  document.getElementById("task-save-btn").textContent = "创建 Task";
  document.getElementById("nt-title").value = "";
  document.getElementById("nt-summary").value = "";
  document.getElementById("nt-body").value = "";
  document.getElementById("nt-status").value = "open";
  document.getElementById("nt-labels").value = "";
  const active = projChannels().filter(channel => !channel.archived);
  const initial = active.some(channel => channel.id === currentChan)
    ? [currentChan]
    : [active.find(channelIsGeneral)?.id || active[0]?.id].filter(Boolean);
  renderTaskChannelOptions(initial);
  tdlg.showModal();
}

function openTaskEditor(id) {
  const task = currentTaskDetail?.task;
  if (!task || task.id !== id) return;
  editingTaskId = id;
  editingTaskSnapshot = task.updated_at;
  closeTaskDialog(false);
  document.getElementById("tdlg-title").textContent = `编辑 ${id}`;
  document.getElementById("task-save-btn").textContent = "保存修改";
  document.getElementById("nt-title").value = task.title;
  document.getElementById("nt-summary").value = task.summary;
  document.getElementById("nt-body").value = task.body;
  document.getElementById("nt-status").value = task.status;
  document.getElementById("nt-labels").value = task.labels.join(", ");
  renderTaskChannelOptions(task.channel_ids);
  tdlg.showModal();
}

function cancelTaskForm() {
  const id = editingTaskId;
  editingTaskId = null;
  editingTaskSnapshot = null;
  tdlg.close();
  if (id) openTask(id);
}

async function saveTask() {
  const title = document.getElementById("nt-title").value.trim();
  if (!title) { uiAlert("标题不能为空"); return; }
  const channel_ids = [...document.querySelectorAll("#nt-channels input:checked")]
    .map(input => input.value);
  if (!channel_ids.length) { uiAlert("请至少绑定一个 Channel"); return; }
  const payload = {
    title,
    summary: document.getElementById("nt-summary").value,
    body: document.getElementById("nt-body").value,
    status: document.getElementById("nt-status").value,
    labels: document.getElementById("nt-labels").value
      .split(",").map(value => value.trim()).filter(Boolean),
    channel_ids,
  };
  const id = editingTaskId;
  if (id) {
    payload.snapshot_updated_at = editingTaskSnapshot;
    await api("PATCH", `/api/tasks/${id}`, payload);
  } else {
    payload.project_id = currentProject;
    await api("POST", "/api/tasks", payload);
  }
  editingTaskId = null;
  editingTaskSnapshot = null;
  tdlg.close();
  await loadOverview();
  if (id) await openTask(id);
  toast(id ? "Task 已更新" : "Task 已创建", "success");
}

function openTaskBriefForm(id) {
  const task = currentTaskDetail?.task;
  if (!task || task.id !== id) return;
  closeTaskDialog(false);
  openFormDialog("添加状态简报", `
    <label>状态</label><select id="task-brief-status">
      ${Object.entries(TASK_STATUS).map(([value, label]) =>
        `<option value="${value}" ${value === task.status ? "selected" : ""}>${label}</option>`).join("")}
    </select>
    <label>简报</label><textarea id="task-brief-content" rows="7" style="height:auto"
      placeholder="说明已完成的工作、当前阻塞或下一步……"></textarea>`,
    `<button class="action" onclick="saveTaskBrief('${id}')">添加简报</button>
     <button class="ghost" onclick="cancelTaskBrief('${id}')">取消</button>`);
}

function cancelTaskBrief(id) {
  fdlg.close();
  openTask(id);
}

async function saveTaskBrief(id) {
  const content = document.getElementById("task-brief-content").value.trim();
  if (!content) { uiAlert("简报不能为空"); return; }
  await api("POST", `/api/tasks/${id}/briefs`, {
    content, status: document.getElementById("task-brief-status").value,
  });
  fdlg.close();
  await loadOverview();
  await openTask(id);
  toast("状态简报已添加", "success");
}

function processTask(id) {
  const task = currentTaskDetail?.task;
  if (!task || task.id !== id) return;
  closeTaskDialog(false);
  openFormDialog(`交给主控处理 · ${task.id}`, `
    <p class="muted" style="margin-top:0">将在所有绑定 Channel 中发送这条 Task。
      可补充处理要求;输入 @ 从列表选择角色可指定处理人:单个角色直接执行
      (不经主控),多个角色由主控协调。不 @ 任何角色则交给项目主控。</p>
    <div class="composer-wrap task-dispatch-wrap">
      <div id="task-dispatch-input" class="task-dispatch-input" contenteditable="true"
        role="textbox" aria-multiline="true" aria-label="处理要求"
        data-placeholder="补充要求或 @指定角色…(可留空;Enter 发送,Shift+Enter 换行)"></div>
      <div id="task-dispatch-picker" class="task-dispatch-picker" role="listbox" hidden></div>
    </div>`,
    `<button class="action" onclick="submitProcessTask('${id}')">发送</button>
     <button class="ghost" onclick="cancelProcessTask('${id}')">取消</button>`);
  bindComposerEvents("task-dispatch-input", "task-dispatch-picker",
    () => submitProcessTask(id));
  setTimeout(() => document.getElementById("task-dispatch-input")?.focus(), 60);
}

function cancelProcessTask(id) {
  activateComposer("input", "mention-picker");
  fdlg.close();
  openTask(id);
}

async function submitProcessTask(id) {
  const box = document.getElementById("task-dispatch-input");
  const { content, mentions } = composerPayload(box);
  await api("POST", `/api/tasks/${id}/process`, { message: content, mentions });
  activateComposer("input", "mention-picker");
  fdlg.close();
  await loadOverview();
  await openTask(id, false);
  const named = [...new Set(mentions.map(item => `@${item.role_id}`))];
  toast(named.length ? `Task 已派发给 ${named.join("、")}`
    : "Task 已发送给绑定 Channel 的主控", "success");
}

async function archiveTask(id) {
  const task = currentTaskDetail?.task;
  if (!task || task.id !== id || task.archived || !await uiConfirm(
    `归档 Task「${task.title}」？归档后会从活跃看板和 Agent Task 快照中隐藏。`,
    "归档 Task")) return;
  await api("POST", `/api/tasks/${encodeURIComponent(id)}/archive`);
  closeTaskDialog();
  await loadOverview();
  toast("Task 已归档", "success");
}

async function restoreTask(id) {
  const task = currentTaskDetail?.task;
  if (!task || task.id !== id || !task.archived) return;
  await api("POST", `/api/tasks/${encodeURIComponent(id)}/restore`);
  closeTaskDialog();
  await loadOverview();
  toast("Task 已恢复并移到最新活动位置", "success");
}

async function deleteTask(id) {
  const task = currentTaskDetail?.task;
  if (!task || task.id !== id || !await uiConfirm(
    `将 Task「${task.title}」及其状态简报移入项目回收站？`,
    "删除 Task")) return;
  await api("DELETE", `/api/tasks/${encodeURIComponent(id)}`);
  closeTaskDialog();
  await loadOverview();
  toast("Task 已移入项目回收站", "success");
}

/* ---------------- 标签看板与自动处理规则 ----------------
   标签看板 = 看板页上按 label 聚合的任务列;绑定了自动处理规则的 label
   必然显示为一列,规则从列头的 ⚡ 图标配置(自定义面板的 taskboard 组件同入口)。 */

function projTaskRules() {
  return projObj()?.task_auto_rules || [];
}

function taskHasLabel(task, label) {
  return task.labels.some(item => item.toLowerCase() === label.toLowerCase());
}

function ruleForLabel(label) {
  return projTaskRules().find(rule =>
    rule.label.toLowerCase() === label.toLowerCase()) || null;
}

// 可见标签列 = 用户固定的标签 ∪ 已配置规则的标签(规则标签必须可见)
function labelBoardEntries() {
  const entries = [];
  const seen = new Set();
  const push = label => {
    if (seen.has(label.toLowerCase())) return;
    seen.add(label.toLowerCase());
    entries.push({ label, rule: ruleForLabel(label) });
  };
  (projObj()?.task_label_boards || []).forEach(push);
  projTaskRules().forEach(rule => { if (rule.label) push(rule.label); });
  return entries;
}

// 看板/规则配置都持久化在项目对象上,复用项目保存端点(未传字段保留现值)
async function persistTaskBoardConfig(patch) {
  const project = projObj();
  await api("POST", "/api/projects", {
    id: currentProject,
    name: project.name,
    description: project.description,
    orchestrator_role_id: project.orchestrator_role_id,
    max_chain_runs: project.max_chain_runs,
    charter: project.charter,
    ...patch,
  });
  await loadOverview();
  renderBoard();
}

function openLabelBoardAdder() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  const shown = new Set(labelBoardEntries().map(entry => entry.label.toLowerCase()));
  const candidates = [...new Set(projTasks().flatMap(task => task.labels))]
    .filter(label => !shown.has(label.toLowerCase()));
  openFormDialog("添加标签看板", `
    <p class="muted" style="margin-top:0">标签看板汇总带某个 label 的全部 Task,
      并可从列头 ⚡ 图标为该 label 配置自动处理规则。</p>
    <label>标签</label>
    <input type="text" id="lb-label" list="lb-label-options"
      placeholder="输入或从已有标签中选择">
    <datalist id="lb-label-options">${candidates.map(label =>
      `<option value="${esc(label)}"></option>`).join("")}</datalist>`,
    `<button class="action" onclick="addLabelBoard()">添加</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
  setTimeout(() => document.getElementById("lb-label")?.focus(), 60);
}

async function addLabelBoard(label) {
  const value = (label ?? document.getElementById("lb-label").value).trim();
  if (!value) { uiAlert("标签不能为空"); return; }
  if (labelBoardEntries().some(entry =>
      entry.label.toLowerCase() === value.toLowerCase())) {
    uiAlert("该标签的看板已经存在"); return;
  }
  await persistTaskBoardConfig({
    task_label_boards: [...(projObj()?.task_label_boards || []), value],
  });
  fdlg.close();
  toast(`已添加标签看板「${value}」`, "success");
}

async function removeLabelBoard(label) {
  if (ruleForLabel(label)) {
    uiAlert("该标签绑定了自动处理规则;请先在 ⚡ 设置中删除规则,再移除看板。");
    return;
  }
  await persistTaskBoardConfig({
    task_label_boards: (projObj()?.task_label_boards || [])
      .filter(item => item.toLowerCase() !== label.toLowerCase()),
  });
  toast(`已移除标签看板「${label}」`, "success");
}

function openLabelRule(label) {
  const rule = ruleForLabel(label);
  openFormDialog(`自动处理规则 · ${label}`, `
    <p class="muted" style="margin-top:0">新建 Task(含脚本同步的 Task)带有
      label「${esc(label)}」时,按下面的处理要求自动派发。输入 @ 从列表选择角色:
      单个角色直接执行(不经主控),多个角色由主控协调;不 @ 任何角色则交给项目主控。</p>
    <div class="composer-wrap task-dispatch-wrap">
      <div id="task-rule-input" class="task-dispatch-input" contenteditable="true"
        role="textbox" aria-multiline="true" aria-label="处理要求"
        data-placeholder="处理要求,可 @指定角色…(Enter 保存,Shift+Enter 换行)"></div>
      <div id="task-rule-picker" class="task-dispatch-picker" role="listbox" hidden></div>
    </div>
    <label><input type="checkbox" id="tr-enabled"
      ${rule ? (rule.enabled ? "checked" : "") : "checked"}> 启用本规则</label>`,
    `<button class="action" data-label="${esc(label)}"
       onclick="saveLabelRule(this.dataset.label)">保存规则</button>
     ${rule ? `<button class="danger" data-label="${esc(label)}"
       onclick="deleteLabelRule(this.dataset.label)">删除规则</button>` : ""}
     <button class="ghost" onclick="cancelLabelRule()">取消</button>`);
  bindComposerEvents("task-rule-input", "task-rule-picker",
    () => saveLabelRule(label));
  const box = document.getElementById("task-rule-input");
  if (rule) {
    // 旧规则只有 role_ids:合成 "@角色 " 前缀提及,保存后自然升级成结构化提及
    let content = rule.prompt || "";
    let mentions = rule.mentions || [];
    if (!mentions.length && rule.role_ids?.length) {
      let prefix = "";
      mentions = rule.role_ids.map(id => {
        const start = Array.from(prefix).length;
        prefix += `@${id} `;
        return { role_id: id, start, end: start + id.length + 1 };
      });
      content = prefix + content;
    }
    restoreComposerPayload(content, mentions, box);
  } else {
    setTimeout(() => box?.focus(), 60);
  }
}

function cancelLabelRule() {
  activateComposer("input", "mention-picker");
  fdlg.close();
}

async function saveLabelRule(label) {
  const box = document.getElementById("task-rule-input");
  const { content, mentions } = composerPayload(box);
  const rules = projTaskRules().map(rule => ({ ...rule }));
  const index = rules.findIndex(rule =>
    rule.label.toLowerCase() === label.toLowerCase());
  const rule = {
    label,
    prompt: content,
    mentions,
    role_ids: [...new Set(mentions.map(item => item.role_id))],
    enabled: document.getElementById("tr-enabled").checked,
  };
  if (index >= 0) rules[index] = rule; else rules.push(rule);
  await persistTaskBoardConfig({ task_auto_rules: rules });
  activateComposer("input", "mention-picker");
  fdlg.close();
  toast(`label「${label}」的自动处理规则已保存`, "success");
}

async function deleteLabelRule(label) {
  if (!await uiConfirm(`删除 label「${label}」的自动处理规则?`)) return;
  await persistTaskBoardConfig({
    task_auto_rules: projTaskRules()
      .filter(rule => rule.label.toLowerCase() !== label.toLowerCase())
      .map(rule => ({ ...rule })),
  });
  activateComposer("input", "mention-picker");
  fdlg.close();
  toast("自动处理规则已删除", "success");
}
