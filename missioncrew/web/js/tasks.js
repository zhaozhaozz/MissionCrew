/* ---------------- Issue 化 Task 看板 ---------------- */
/* 状态即标签(status: 文本);内置四态是锁定的前四列,样式按文本映射 */
const STATUS_META = {
  "待处理": { color: "var(--muted)", cls: "st-open" },
  "处理中": { color: "var(--accent)", cls: "st-in_progress" },
  "已阻塞": { color: "var(--bad)", cls: "st-blocked" },
  "已完成": { color: "var(--ok)", cls: "st-done" },
};
const STATUS_VALUES = Object.keys(STATUS_META);
const BUILTIN_COLS = STATUS_VALUES.map(value => ({
  title: value, color: STATUS_META[value].color,
  query: `status: ${value}`, locked: true,
}));

/* 标签工具:与服务端 label_query 同一套归一化(首个冒号切分高级标签) */
function splitLabel(text) {
  const raw = String(text || "").trim();
  const idx = raw.indexOf(":");
  if (idx > 0) {
    const prop = raw.slice(0, idx).trim();
    const value = raw.slice(idx + 1).trim();
    if (prop && value) return [prop, value];
  }
  return ["", raw];
}
function normalizeLabel(text) {
  const [prop, value] = splitLabel(text);
  return prop ? `${prop}: ${value}` : value;
}
function taskStatus(task) {
  for (const label of task.labels || []) {
    const [prop, value] = splitLabel(label);
    if (prop.toLowerCase() === "status") return value;
  }
  return "";
}
function taskDisplayLabels(task) {
  return (task.labels || []).filter(label =>
    splitLabel(label)[0].toLowerCase() !== "status");
}
function statusPillHtml(status) {
  if (!status) return "";
  const meta = STATUS_META[status];
  return `<span class="pill ${meta ? meta.cls : ""}">${esc(status)}</span>`;
}
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

// 内置任务看板只显示 built-in 源;外部同步任务在各自的自定义看板里
function builtinProjTasks() {
  return projTasks().filter(task =>
    (task.source_id || "built-in") === "built-in");
}

function visibleProjTasks() {
  return builtinProjTasks().filter(task =>
    taskFilter === "all"
      || (taskFilter === "archived" ? task.archived : !task.archived));
}

function renderTaskFilter() {
  // 列表只含当前筛选范围,计数用服务端的全量口径;拉取前回退本地计算
  const tasks = builtinProjTasks();
  const counts = taskCounts || {
    active: tasks.filter(task => !task.archived).length,
    archived: tasks.filter(task => task.archived).length,
  };
  const select = document.getElementById("task-filter");
  if (select) select.value = taskFilter;
  const summary = document.getElementById("task-filter-summary");
  if (summary) {
    const shown = visibleProjTasks().length;
    summary.textContent = `显示 ${shown} 个 · 活跃 ${counts.active} · 已归档 ${counts.archived}`;
  }
}

function setTaskFilter(value) {
  if (!TASK_FILTERS.has(value)) return;
  taskFilter = value;
  localStorage.setItem("mc.taskFilter", value);
  renderBoard();
  void refreshTasks().then(() => renderBoard());   // 列表按新筛选重拉
}

function taskCardHtml(task, { showStatus = false } = {}) {
  return `<div class="card ${task.archived ? "task-archived" : ""}"
      data-task-id="${esc(task.id)}" onclick="openTask(this.dataset.taskId)">
    <div class="title">${esc(task.title)}${showStatus
      ? ` ${statusPillHtml(taskStatus(task))}` : ""}${task.archived
      ? '<span class="badge task-archived-badge">已归档</span>' : ""}${task.url
      ? ` <a class="badge" href="${esc(task.url)}" target="_blank" rel="noopener"
           onclick="event.stopPropagation()" title="打开外部链接">↗</a>` : ""}</div>
    ${task.summary ? `<div class="task-card-summary">${esc(task.summary)}</div>` : ""}
    <div class="meta">更新 ${new Date(task.updated_at * 1000).toLocaleString()} ·
      ${esc(task.id)} · ${task.channel_ids.map(id => esc(taskChannelLabel(id))).join(" · ")}</div>
    <div class="meta">${taskDisplayLabels(task).map(label => `<span class="badge">${esc(label)}</span>`).join("")}</div>
  </div>`;
}

function taskMatchesQuery(query, task) {
  try { return matchLabelQuery(query, task.labels || []); }
  catch (_) { return false; }
}

function renderBoard() {
  // 看板横向滚动、每列内部纵向滚动:分别按列 key 保持重渲染前的滚动位置
  const board = document.getElementById("board");
  const outerScroll = captureScrollPositions(["#board"]);
  const columnScroll = captureKeyedScrollPositions(board);
  renderTaskFilter();
  // 内置看板与自定义任务看板同构:四个锁定状态列 + 项目自定义筛选列,
  // 每列一个标签表达式,⚡ 为该表达式配置自动处理规则
  const columns = BUILTIN_COLS.concat(projTaskBoardFilters().map(item => ({
    title: item.title, color: item.color || "var(--muted)", query: item.query,
  })));
  board.innerHTML = columns.map(col => {
    const items = visibleProjTasks().filter(task => taskMatchesQuery(col.query, task));
    const cards = items.map(task => taskCardHtml(task, { showStatus: !col.locked })).join("")
      || `<div class="empty" style="padding:6px 4px">暂无 Task</div>`;
    const rule = ruleForQuery(col.query);
    const ruleState = rule ? (rule.enabled ? "rule-on" : "rule-off") : "";
    const ruleTitle = rule
      ? (rule.enabled ? "自动处理规则已启用,点击修改" : "自动处理规则已停用,点击修改")
      : "为该列的标签表达式设置自动处理规则";
    const removeBtn = col.locked ? "" : `
      <button class="col-tool" title="移除该筛选列(不影响 Task)"
        data-query="${esc(col.query)}"
        onclick="removeTaskBoardFilter(this.dataset.query)">✕</button>`;
    return `<section class="col ${col.locked ? "" : "col-label"}"><h2>
      <span class="col-dot" style="background:${col.color}"></span>
      ${esc(col.title)}<span class="col-count">${items.length}</span>
      <button class="col-tool col-rule ${ruleState}" title="${esc(ruleTitle)}"
        data-query="${esc(col.query)}"
        onclick="openColumnRule(this.dataset.query)">⚡</button>${removeBtn}</h2>
      <div class="col-list" data-scroll-key="col:${esc(col.query)}">${cards}</div></section>`;
  }).join("");
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
      ${statusPillHtml(brief.status) || '<span class="pill"></span>'}
      <span>${new Date(brief.created_at * 1000).toLocaleString()}</span>
      <span>${brief.author_type === "agent" ? "@" : ""}${esc(brief.author)}</span>
    </div>
    <div class="markdown-body">${miniMarkdown(brief.content)}</div>
  </article>`).join("");
  document.getElementById("dlg-body").innerHTML = `
    <div class="task-detail-meta">
      ${statusPillHtml(taskStatus(task))}
      ${task.archived ? '<span class="badge task-archived-badge">已归档</span>' : ""}
      ${task.url ? `<a class="badge" href="${esc(task.url)}" target="_blank"
        rel="noopener">外部链接 ↗</a>` : ""}
      <span>${channels || "未绑定 Channel"}</span>
      ${taskDisplayLabels(task).map(label => `<span class="badge">${esc(label)}</span>`).join("")}
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
  if (!task.archived && taskStatus(task) !== "已完成")
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
  document.getElementById("nt-status").value = "待处理";
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
  const select = document.getElementById("nt-status");
  const status = taskStatus(task);
  if (status && !STATUS_VALUES.includes(status))   // 自由状态文本也可编辑
    select.insertAdjacentHTML("beforeend",
      `<option value="${esc(status)}">${esc(status)}</option>`);
  select.value = status || "待处理";
  document.getElementById("nt-labels").value = taskDisplayLabels(task).join(", ");
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
  // 频道绑定可选:不勾选即创建无绑定 Task,派发时按 general 回退
  const channel_ids = [...document.querySelectorAll("#nt-channels input:checked")]
    .map(input => input.value);
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
  const current = taskStatus(task);
  const options = [...new Set([current, ...STATUS_VALUES])].filter(Boolean);
  openFormDialog("添加状态简报", `
    <label>状态</label><select id="task-brief-status">
      ${options.map(value =>
        `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(value)}</option>`).join("")}
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
  const peer = peerModeProject();
  openFormDialog(`${peer ? "派发给角色" : "交给主控处理"} · ${task.id}`, `
    <p class="muted" style="margin-top:0">将在所有绑定 Channel 中发送这条 Task。
      ${peer ? "本项目没有主控:输入 @ 从列表选择处理角色(必填),多个角色各自启动。"
             : "可补充处理要求;输入 @ 从列表选择角色可指定处理人:单个角色直接执行" +
               "(不经主控),多个角色由主控协调。不 @ 任何角色则交给项目主控。"}</p>
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
  if (peerModeProject() && !mentions.length) {
    uiAlert("本项目没有主控，请 @ 指定处理角色。");
    return;
  }
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

/* ---------------- 筛选列与自动处理规则 ----------------
   内置看板 = 四个锁定状态列 + 项目自定义筛选列(task_board_filters,每列
   一个标签表达式);规则(task_auto_rules)按表达式配置,从列头的 ⚡ 图标
   进入(自定义面板的 taskboard 组件同入口)。 */

function projTaskRules() {
  return projObj()?.task_auto_rules || [];
}

function projTaskBoardFilters() {
  return projObj()?.task_board_filters || [];
}

function taskHasLabel(task, label) {
  return (task.labels || []).some(item =>
    normalizeLabel(item).toLowerCase() === normalizeLabel(label).toLowerCase());
}

function ruleForQuery(query) {
  return projTaskRules().find(rule =>
    (rule.query || "").toLowerCase() === String(query || "").toLowerCase())
    || null;
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

function openTaskBoardFilterAdder() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  const candidates = [...new Set(projTasks()
    .flatMap(task => task.labels || []).map(normalizeLabel))];
  openFormDialog("添加筛选列", `
    <p class="muted" style="margin-top:0">筛选列按标签表达式聚合 Task,
      支持 & | ! 与括号、「属性: 值」高级标签与「属性: *」存在性匹配,
      例如 <code>bug & !status: 已完成</code>。列头 ⚡ 图标可为该表达式
      配置自动处理规则。</p>
    <label>标签表达式</label>
    <input type="text" id="tbf-query" list="tbf-label-options"
      placeholder="输入表达式或从已有标签中选择">
    <datalist id="tbf-label-options">${candidates.map(label =>
      `<option value="${esc(label)}"></option>`).join("")}</datalist>
    <label>列标题(可选,缺省用表达式)</label>
    <input type="text" id="tbf-title" placeholder="例如:未完成的缺陷">`,
    `<button class="action" onclick="addTaskBoardFilter()">添加</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
  setTimeout(() => document.getElementById("tbf-query")?.focus(), 60);
}

async function addTaskBoardFilter() {
  const query = document.getElementById("tbf-query").value.trim();
  if (!query) { uiAlert("表达式不能为空"); return; }
  try { compileLabelQuery(query); }
  catch (error) { uiAlert(`标签表达式不合法:${error.message}`); return; }
  const exists = BUILTIN_COLS.concat(projTaskBoardFilters())
    .some(item => item.query.toLowerCase() === query.toLowerCase());
  if (exists) { uiAlert("该表达式的列已经存在"); return; }
  const title = document.getElementById("tbf-title").value.trim();
  await persistTaskBoardConfig({
    task_board_filters: [...projTaskBoardFilters(),
      { title: title || query, query, color: "" }],
  });
  fdlg.close();
  toast(`已添加筛选列「${title || query}」`, "success");
}

async function removeTaskBoardFilter(query) {
  if (ruleForQuery(query)) {
    uiAlert("该列的表达式绑定了自动处理规则;请先在 ⚡ 设置中删除规则,再移除列。");
    return;
  }
  await persistTaskBoardConfig({
    task_board_filters: projTaskBoardFilters()
      .filter(item => item.query.toLowerCase() !== query.toLowerCase()),
  });
  toast("已移除筛选列", "success");
}

function openColumnRule(query) {
  const rule = ruleForQuery(query);
  openFormDialog(`自动处理规则 · ${query}`, `
    <p class="muted" style="margin-top:0">新建 Task(含脚本同步的 Task)的标签
      命中表达式「${esc(query)}」时,按下面的处理要求自动派发。输入 @ 从列表选择角色:
      ${peerModeProject() ? "本项目没有主控,必须 @ 至少一个角色,多个角色各自启动。"
                          : "单个角色直接执行(不经主控),多个角色由主控协调;不 @ 任何角色则交给项目主控。"}</p>
    <div class="composer-wrap task-dispatch-wrap">
      <div id="task-rule-input" class="task-dispatch-input" contenteditable="true"
        role="textbox" aria-multiline="true" aria-label="处理要求"
        data-placeholder="处理要求,可 @指定角色…(Enter 保存,Shift+Enter 换行)"></div>
      <div id="task-rule-picker" class="task-dispatch-picker" role="listbox" hidden></div>
    </div>
    <label><input type="checkbox" id="tr-enabled"
      ${rule ? (rule.enabled ? "checked" : "") : "checked"}> 启用本规则</label>`,
    `<button class="action" data-query="${esc(query)}"
       onclick="saveColumnRule(this.dataset.query)">保存规则</button>
     ${rule ? `<button class="danger" data-query="${esc(query)}"
       onclick="deleteColumnRule(this.dataset.query)">删除规则</button>` : ""}
     <button class="ghost" onclick="cancelColumnRule()">取消</button>`);
  bindComposerEvents("task-rule-input", "task-rule-picker",
    () => saveColumnRule(query));
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

function cancelColumnRule() {
  activateComposer("input", "mention-picker");
  fdlg.close();
}

async function saveColumnRule(query) {
  const box = document.getElementById("task-rule-input");
  const { content, mentions } = composerPayload(box);
  const rules = projTaskRules().map(rule => ({ ...rule }));
  const index = rules.findIndex(rule =>
    (rule.query || "").toLowerCase() === query.toLowerCase());
  const rule = {
    query,
    prompt: content,
    mentions,
    role_ids: [...new Set(mentions.map(item => item.role_id))],
    enabled: document.getElementById("tr-enabled").checked,
  };
  if (index >= 0) rules[index] = rule; else rules.push(rule);
  await persistTaskBoardConfig({ task_auto_rules: rules });
  activateComposer("input", "mention-picker");
  fdlg.close();
  toast(`表达式「${query}」的自动处理规则已保存`, "success");
}

async function deleteColumnRule(query) {
  if (!await uiConfirm(`删除表达式「${query}」的自动处理规则?`)) return;
  await persistTaskBoardConfig({
    task_auto_rules: projTaskRules()
      .filter(rule => (rule.query || "").toLowerCase() !== query.toLowerCase())
      .map(rule => ({ ...rule })),
  });
  activateComposer("input", "mention-picker");
  fdlg.close();
  toast("自动处理规则已删除", "success");
}
