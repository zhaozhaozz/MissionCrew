/* ---------------- Issue 化 Task 看板 ---------------- */
/* 状态即标签(status: 文本);取值与颜色由任务所属数据源声明,
   内置源是下面四态(也是内置看板锁定的前四列) */
const BUILTIN_STATUS_VALUES = [
  { value: "待处理", color: "var(--muted)" },
  { value: "处理中", color: "var(--accent)" },
  { value: "已阻塞", color: "var(--bad)" },
  { value: "已完成", color: "var(--ok)" },
];
const STATUS_VALUES = BUILTIN_STATUS_VALUES.map(item => item.value);

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
// 数据源声明的状态取值 [{value,color}](随项目总览下发);清单未到时内置源用本地四态
function sourceStatusValues(source) {
  const id = source || "built-in";
  const hit = projBoardSources().find(item => item.id === id);
  if (hit?.status_values?.length) return hit.status_values;
  return id === "built-in" ? BUILTIN_STATUS_VALUES : [];
}

// 状态药丸按所属数据源声明的颜色着色,内置与自定义状态同一规则;
// 声明了但没给颜色的与看板列一样用灰色,未声明的自由状态文本不着色
function statusPillHtml(status, source = "built-in") {
  if (!status) return "";
  const hit = sourceStatusValues(source).find(item =>
    String(item.value).toLowerCase() === status.toLowerCase());
  return hit
    ? `<span class="pill status-pill" style="--status-color:${esc(hit.color || "var(--muted)")}">${esc(status)}</span>`
    : `<span class="pill">${esc(status)}</span>`;
}
const TASK_FILTERS = new Set(["all", "active", "archived"]);
let taskFilter = localStorage.getItem("mc.taskFilter") || "active";
if (!TASK_FILTERS.has(taskFilter)) taskFilter = "active";
let currentTaskId = null;
let currentTaskDetail = null;
let editingTaskId = null;
let editingTaskSnapshot = null;
let editingTaskSource = "built-in";   // 新建 Task 归属的数据源(从哪块看板点的新建)

function closeTaskDialog(updateRoute = true) {
  currentTaskId = null;
  currentTaskDetail = null;
  if (dlg.open) dlg.close();
  if (updateRoute) syncUrl();
}

dlg.addEventListener("close", () => {
  // 详情底部的派发输入框随弹窗失效,提及选择器交还聊天主输入框
  activateComposer("input", "mention-picker");
  if (!currentTaskId) return;
  currentTaskId = null;
  currentTaskDetail = null;
  syncUrl();
});

// 显示范围(活跃/全部/已归档)是两块任务看板共用的视图状态
function setTaskFilter(value) {
  if (!TASK_FILTERS.has(value)) return;
  taskFilter = value;
  localStorage.setItem("mc.taskFilter", value);
  renderBoard();
  renderCustomBoards(true);
  void refreshTasks();   // 任务列表(直链打开详情等用)按新范围重拉
}

// 内置任务看板与自定义任务看板共用 boards.js 的渲染器,只差绑定(数据端点与配置保存)
function renderBoard() {
  renderTaskboard(builtinTaskboardBinding());
}

// 两种更新时间分开展示:外部系统的时间由同步或 Agent 设置;系统更新时间由 MissionCrew
// 在每次写入时自动记录,只读,也是编辑时的乐观锁版本。
function taskTimesHtml(task) {
  const time = seconds => new Date(seconds * 1000).toLocaleString();
  const external = Number(task.external_updated_at) > 0;
  return `<div class="task-detail-times">
    <span title="外部系统里的更新时间，由数据源同步或 Agent 设置；看板按它排序">更新时间
      <b>${external ? time(task.external_updated_at) : "未提供"}</b></span>
    <span title="MissionCrew 自动记录：任务有任何修改或新简报都会刷新，不能手动设置">系统更新时间
      <b>${time(task.updated_at)}</b></span>
    <span title="任务在 MissionCrew 中创建的时间">创建时间 <b>${time(task.created_at)}</b></span>
  </div>`;
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
      ${statusPillHtml(brief.status, task.source_id) || '<span class="pill"></span>'}
      <span>${new Date(brief.created_at * 1000).toLocaleString()}</span>
      <span>${brief.author_type === "agent" ? "@" : ""}${esc(brief.author)}</span>
    </div>
    <div class="markdown-body">${miniMarkdown(brief.content)}</div>
  </article>`).join("");
  document.getElementById("dlg-body").innerHTML = `
    <div class="task-detail-meta">
      ${statusPillHtml(taskStatus(task), task.source_id)}
      ${task.archived ? '<span class="badge task-archived-badge">已归档</span>' : ""}
      ${task.url ? `<a class="badge" href="${esc(task.url)}" target="_blank"
        rel="noopener">外部链接 ↗</a>` : ""}
      <span>${channels || "未绑定 Channel"}</span>
      ${taskDisplayLabels(task).map(label => `<span class="badge">${esc(label)}</span>`).join("")}
    </div>
    ${taskTimesHtml(task)}
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
  document.getElementById("dlg-actions").innerHTML = actions.join("");
  renderTaskDispatchPanel(task);
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

// 表单的状态选项随任务所属数据源而定;源已不在时退回内置四态
function taskStatusOptions(source) {
  const values = sourceStatusValues(source).map(item => item.value).filter(Boolean);
  return values.length ? values : STATUS_VALUES;
}

function renderTaskStatusOptions(values, selected) {
  // 不在取值里的自由状态文本也列为选项,编辑时不会被悄悄改掉
  const options = [...new Set([...values, selected].filter(Boolean))];
  const select = document.getElementById("nt-status");
  select.innerHTML = options.map(value =>
    `<option value="${esc(value)}">${esc(value)}</option>`).join("");
  select.value = selected || values[0];
}

function openNewTask(source = "built-in") {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  editingTaskId = null;
  editingTaskSnapshot = null;
  editingTaskSource = source || "built-in";
  const values = taskStatusOptions(editingTaskSource);
  document.getElementById("tdlg-title").textContent = editingTaskSource === "built-in"
    ? "新建 Task" : `新建 Task · ${boardSourceName(editingTaskSource)}`;
  document.getElementById("task-save-btn").textContent = "创建 Task";
  document.getElementById("nt-title").value = "";
  document.getElementById("nt-summary").value = "";
  document.getElementById("nt-body").value = "";
  renderTaskStatusOptions(values, values[0]);
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
  const values = taskStatusOptions(task.source_id);
  document.getElementById("tdlg-title").textContent = `编辑 ${id}`;
  document.getElementById("task-save-btn").textContent = "保存修改";
  document.getElementById("nt-title").value = task.title;
  document.getElementById("nt-summary").value = task.summary;
  document.getElementById("nt-body").value = task.body;
  renderTaskStatusOptions(values, taskStatus(task) || values[0]);
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
    payload.source = editingTaskSource;
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
  const values = taskStatusOptions(task.source_id);
  const options = [...new Set([current, ...values])].filter(Boolean);
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

// 派发只发到一个频道:可选项为项目全部活跃频道,默认首个绑定频道,无绑定时回退 general
function taskDispatchChannelOptions(task) {
  const active = projChannels().filter(channel => !channel.archived);
  const bound = task.channel_ids.filter(id => active.some(channel => channel.id === id));
  const selected = bound[0] || (active.find(channelIsGeneral) || active[0])?.id;
  // 已绑定的排在前面并标注,选未绑定频道时后端会顺带绑定
  const ordered = [...active].sort((a, b) =>
    Number(!bound.includes(a.id)) - Number(!bound.includes(b.id)));
  return ordered.map(channel => `<option value="${esc(channel.id)}"
    ${channel.id === selected ? "selected" : ""}>#${esc(channel.name || channel.id)}${
      bound.includes(channel.id) ? " · 已绑定" : ""}</option>`).join("");
}

// 「交给角色处理」直接放在详情弹窗底部(频道选择 + 输入框),不另开弹窗;
// 已归档或已完成的 Task 不显示
function renderTaskDispatchPanel(task) {
  const panel = document.getElementById("dlg-dispatch");
  // 输入框每次随详情重建,先把提及选择器交还主输入框,丢掉指向旧节点的选区
  activateComposer("input", "mention-picker");
  if (task.archived || taskStatus(task) === "已完成") {
    panel.hidden = true;
    panel.replaceChildren();
    return;
  }
  const peer = peerModeProject();
  panel.innerHTML = `
    <div class="task-dispatch-head">
      <strong>交给角色处理</strong>
      <span class="muted">${peer ? "本项目没有主控：@ 选择处理角色（必填），多个角色各自启动"
        : "不 @ 角色时交给主控；@ 单个角色直接执行，@ 多个由主控协调"}</span>
      <label class="task-dispatch-target">发到
        <select id="task-dispatch-channel"
          title="只在这一个 Channel 中发送；选未绑定的 Channel 会同时绑定它">${taskDispatchChannelOptions(task)}</select>
      </label>
    </div>
    <div class="task-dispatch-compose">
      <div class="composer-wrap">
        <div id="task-dispatch-input" class="task-dispatch-input" contenteditable="true"
          role="textbox" aria-multiline="true" aria-label="处理要求"
          data-placeholder="${peer ? "输入 @ 选择处理角色，可补充处理要求…"
            : "补充处理要求，或输入 @ 指定角色…（可留空）"}（Enter 发送，Shift+Enter 换行）"></div>
        <div id="task-dispatch-picker" class="task-dispatch-picker" role="listbox" hidden></div>
      </div>
      <button class="send" id="task-dispatch-send" onclick="submitProcessTask('${task.id}')">发送</button>
    </div>`;
  panel.hidden = false;
  bindComposerEvents("task-dispatch-input", "task-dispatch-picker",
    () => submitProcessTask(task.id));
}

async function submitProcessTask(id) {
  const box = document.getElementById("task-dispatch-input");
  const button = document.getElementById("task-dispatch-send");
  if (!box || button.disabled) return;   // 请求未返回时再按 Enter 不重复派发
  const { content, mentions } = composerPayload(box);
  if (peerModeProject() && !mentions.length) {
    uiAlert("本项目没有主控，请 @ 指定处理角色。");
    return;
  }
  const channel_id = document.getElementById("task-dispatch-channel").value;
  if (!channel_id) { uiAlert("请先选择接收 Channel"); return; }
  button.disabled = true;
  try {
    await api("POST", `/api/tasks/${id}/process`, { message: content, mentions, channel_id });
  } finally {
    button.disabled = false;
  }
  box.replaceChildren();
  await loadOverview();
  // 派发后刷新详情(可能新增了频道绑定);期间用户已关掉弹窗就不再弹出
  if (currentTaskId === id) await openTask(id, false);
  const where = `#${projChannels().find(channel => channel.id === channel_id)?.name || channel_id}`;
  const named = [...new Set(mentions.map(item => `@${item.role_id}`))];
  toast(named.length ? `Task 已在 ${where} 派发给 ${named.join("、")}`
    : `Task 已发送给 ${where} 的主控`, "success");
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

/* ---------------- 看板配置与自动处理规则 ----------------
   内置看板 = 四个锁定状态列 + 项目自定义筛选列(task_board_filters,每列
   一个标签表达式)+ 默认分列属性(task_board_group_by),共享列在看板分列选
   「自定义表达式列」后从旁边的 ✎ 打开编辑对话框修改;日常筛选和显示偏好由 boards.js 独立管理。规则(task_auto_rules)的
   列表页与编辑器在 task-rules.js,列头 ⚡ 与面板组件打开同一个编辑器。 */

function projTaskRules() {
  return projObj()?.task_auto_rules || [];
}

function projTaskBoardFilters() {
  return projObj()?.task_board_filters || [];
}

// 看板列与规则按条件对应:比较规范写法,空格、&& 等书写差异不影响
function ruleForQuery(query) {
  return projTaskRules().find(rule => sameRuleQuery(rule.query, query)) || null;
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
