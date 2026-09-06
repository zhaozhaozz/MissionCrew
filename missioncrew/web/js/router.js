/* ---- URL 路由:/<项目>/<视图>[/<频道>](History API,干净 URL),
   刷新与前进后退都能还原;服务端对非 API 路径统一返回本页面 ---- */
const TABS = ["chat", "board", "custom", "docs", "guidelines", "skills",
              "automations", "recycle-bin", "proj", "runtime-status", "settings"];
let routeApplying = false;

function emptyRoute(project = null, tab = "chat") {
  return { project, tab, chan: null, doc: null, task: null, dashboard: null,
           guideline: undefined, skill: undefined, skillFile: null,
           automation: undefined };
}

function parsePath() {
  // 兼容旧的 hash 链接(/#/default/settings):路径为根时读 hash
  const raw = location.pathname !== "/" ? location.pathname
            : location.hash.replace(/^#/, "");
  const resource = missionCrewResourceReference(raw);
  if (resource) {
    const route = emptyRoute(resource.projectId);
    const [id, ...rest] = resource.segments;
    if (resource.type === "documents") {
      route.tab = "docs"; route.doc = resource.segments.join("/") || null;
    } else if (resource.type === "channels") {
      route.tab = "chat"; route.chan = id || null;
    } else if (resource.type === "tasks") {
      route.tab = "board"; route.task = id || null;
    } else if (resource.type === "dashboards") {
      route.tab = !id || id === "tasks" ? "board" : "custom";
      route.dashboard = id || "tasks";
    } else if (resource.type === "guidelines") {
      route.tab = "guidelines"; route.guideline = id || null;
    } else if (resource.type === "skills") {
      route.tab = "skills"; route.skill = id || null;
      route.skillFile = rest.join("/") || null;
    } else if (resource.type === "automations") {
      route.tab = "automations"; route.automation = id || null;
    } else if (resource.type === "recycle-bin") {
      route.tab = "recycle-bin";
    }
    return route;
  }
  const parts = raw.replace(/^\/+/, "").split("/").map(value => {
    try { return decodeURIComponent(value); } catch (_) { return value; }
  });
  return { ...emptyRoute(parts[0] || null,
             TABS.includes(parts[1]) ? parts[1] : "chat"),
           chan: parts.slice(2).join("/") || null };
}

function syncUrl(push = true) {
  // 首次路由还原完成前不写 URL:否则加载期的默认频道选择会先把
  // 原始地址(如 /default/settings)覆写成 chat,刷新就回不去了
  if (!routeRestored || routeApplying || !currentProject) return;
  let path = `/${encodeURIComponent(currentProject)}/${currentTab}`;
  if (currentTab === "chat" && currentChan)
    path = missionCrewResourceUrl(currentProject, "channels",
      currentChan.replace(`${currentProject}:`, ""));
  if (currentTab === "board")
    path = currentTaskId
      ? missionCrewResourceUrl(currentProject, "tasks", currentTaskId)
      : missionCrewResourceUrl(currentProject, "dashboards", "tasks");
  if (currentTab === "custom")
    path = missionCrewResourceUrl(currentProject, "dashboards",
      ...(currentCustomBoard ? [currentCustomBoard.replace(`${currentProject}:`, "")] : []));
  if (currentTab === "docs" && docMode !== "new" && !docViewer.viewingRevision)
    path = missionCrewDocumentUrl(currentProject, docSelected || "");
  if (currentTab === "guidelines")
    path = missionCrewResourceUrl(currentProject, "guidelines",
      ...(selectedGuidelineName ? [selectedGuidelineName] : []));
  if (currentTab === "skills")
    path = missionCrewResourceUrl(currentProject, "skills",
      ...(selectedSkillId ? [selectedSkillId] : []),
      ...(selectedSkillId && skillOpenFile ? skillOpenFile.split("/") : []));
  if (currentTab === "automations")
    path = missionCrewResourceUrl(currentProject, "automations",
      ...(selectedAutomationId
        ? [selectedAutomationId.replace(`${currentProject}:`, "")] : []));
  if (currentTab === "recycle-bin")
    path = missionCrewResourceUrl(currentProject, "recycle-bin");
  if (location.pathname === path && !location.hash) return;
  if (push) history.pushState(null, "", path);      // 用户操作:产生历史记录
  else history.replaceState(null, "", path);        // 规范化:不产生历史记录
}

async function applyRoute() {
  const r = parsePath();
  if (!r.project) { syncUrl(false); return; }
  if (!overview.projects.some(p => p.id === r.project)) {
    syncUrl(false);
    return;
  }
  routeApplying = true;
  try {
    // 任一步骤被未保存修改守卫取消时，跳过后续切换，最后统一把 URL 规范回当前状态
    let cancelled = false;
    if (r.project !== currentProject) {
      const switched = await setProject(r.project, false);
      if (switched === false) cancelled = true;   // 用户选择保留未保存修改，取消跳转
    }

    if (!cancelled && r.tab === "chat") {
      const findChannel = () => projChannels().find(item =>
        item.id === r.chan || item.id === `${r.project}:${r.chan}`)
        || (!r.chan ? projChannels()[0] : null);
      let channel = findChannel();
      if (!channel && r.chan && channelFilter !== "all") {
        // 直链归档频道:当前筛选的列表里取不到,切到全部并重拉后再试
        channelFilter = "all";
        localStorage.setItem("mc.channelFilter", channelFilter);
        await refreshChannels();
        channel = findChannel();
      }
      if (channel && channel.id !== currentChan) selectChannel(channel.id, false);
      if (channel?.archived && channelFilter === "active") {
        channelFilter = "all";
        localStorage.setItem("mc.channelFilter", channelFilter);
      }
    }

    const documentChanged = r.tab === "docs" && r.doc !== docSelected;
    if (!cancelled && documentChanged && !await confirmDocDiscard()) cancelled = true;
    const guidelineChanged = r.tab === "guidelines" && r.guideline
      && selectedGuidelineName !== undefined && r.guideline !== selectedGuidelineName;
    if (!cancelled && guidelineChanged && !await guidelineViewer.confirmDiscard())
      cancelled = true;
    const routeAutomation = r.tab === "automations" ? projAutomations().find(item =>
      item.id === r.automation || item.id === `${r.project}:${r.automation}`) : null;
    const automationChanged = r.tab === "automations" && automationEditingId !== undefined
      && (routeAutomation?.id ?? null) !== automationEditingId;
    if (!cancelled && automationChanged && !await confirmAutomationDiscard()) cancelled = true;
    if (!cancelled) {
    if (r.tab === "docs") {
      docSelected = r.doc;
      docMode = "view";
      docViewer.activate();
    }
    if (r.tab === "custom") {
      const board = projBoards().find(item =>
        item.id === r.dashboard || item.id === `${r.project}:${r.dashboard}`);
      currentCustomBoard = board?.id || null;
      customBoardEditing = false;
      boardEditorVisible = false;
    }
    if (r.tab === "guidelines") {
      const next = (projObj()?.guidelines || [])
        .some(item => item.name === r.guideline) ? r.guideline : undefined;
      if (next !== selectedGuidelineName) {
        selectedGuidelineName = next;
        guidelineViewer.activate();
      }
    }
    if (r.tab === "skills") {
      resetSkillHistoryState();
      selectedSkillId = (projObj()?.skills || [])
        .some(item => item.id === r.skill) ? r.skill : undefined;
      skillOpenFile = selectedSkillId ? r.skillFile : null;
    }
    if (r.tab === "automations") {
      if (automationChanged) clearAutomationEditState();
      selectedAutomationId = routeAutomation?.id;
    }

    if (r.tab !== "board" || !r.task) closeTaskDialog(false);
    if (r.tab !== currentTab) switchTab(r.tab);
    else if (documentChanged) await renderDocuments();
    else if (r.tab === "custom") renderCustomBoards(true);
    else if (r.tab === "guidelines") renderGuidelinesPage(true);
    else if (r.tab === "skills") renderSkillsPage(true);
    else if (r.tab === "automations") renderAutomationPage(true);
    else if (r.tab === "recycle-bin") renderRecycleBin(true);

    if (r.tab === "board" && r.task) {
      await ensureTasksLoaded();
      let task = projTasks().find(item => item.id === r.task);
      if (!task && taskFilter !== "all") {
        // 直链归档任务:当前筛选的列表里取不到,切到全部并重拉后再试
        taskFilter = "all";
        localStorage.setItem("mc.taskFilter", taskFilter);
        await refreshTasks();
        renderBoard();
        task = projTasks().find(item => item.id === r.task);
      }
      if (task) await openTask(task.id, false);
    }
    }
  } finally {
    routeApplying = false;
  }
  syncUrl(false);   // 规范化(清掉无效项目/资源段、旧 hash)
}

function openMissionCrewResourceLink(event, target) {
  event.preventDefault();
  const resource = missionCrewResourceReference(target);
  if (!resource || !overview.projects.some(project => project.id === resource.projectId)) {
    toast("找不到 MissionCrew 资源", "error");
    return false;
  }
  history.pushState(null, "", resource.url);
  void applyRoute();
  return false;
}

window.addEventListener("popstate", applyRoute);

async function setProject(id, updateRoute = true) {
  if (id !== currentProject && !await confirmDiscardUnsaved()) {
    // 用户取消：还原顶部项目选择框，保持当前项目
    const select = document.getElementById("proj-sel");
    if (select && currentProject) select.value = currentProject;
    return false;
  }
  currentProject = id;
  localStorage.setItem("mc.project", id);
  await loadProjectScope();   // 角色/频道/面板等项目内数据换成新项目的
  currentChan = null; lastMsgId = 0; lastMsgDate = "";
  firstMsgId = 0; chanHasEarlier = false;
  currentCustomBoard = null; customBoardEditing = false; boardEditMode = false;
  selectedGuidelineName = undefined;
  guidelineViewer.reset();
  selectedSkillId = undefined;
  selectedAutomationId = undefined;
  automationEditingId = undefined;
  automationInitialFormSnapshot = "";
  resetSkillHistoryState();
  skillMarkdownMode = "preview";
  skillOpenFile = null;
  skillLibraryInfo = null;
  skillFolderImportOpen = false;
  recycleBinItems = [];
  configChatSelection = null;
  clearConfigChatUploads();
  configEditorDirty.guidelines = false;
  configEditorDirty.skills = false;
  document.getElementById("msgs").innerHTML = "";
  roleColor = Object.fromEntries(projRoles().map(r => [r.id, r.color || "#888"]));
  docFiles = []; docFilesMeta = []; docSelected = null;
  docMode = "view";
  docViewer.reset();
  docExpanded.clear();
  closeTaskDialog(false);
  renderSidebar(); renderBoard(); renderCustomBoards();
  if (currentTab === "proj") renderProjSettings();
  if (currentTab === "docs") renderDocuments();
  else if (currentTab === "automations") renderAutomationPage(true);
  else if (currentTab === "recycle-bin") renderRecycleBin(true);
  else loadDocFiles().then(changed => { if (changed) renderSidebar(); });
  renderProjectConfigPage(currentTab, true);
  const chans = projChannels();
  if (chans.length) selectChannel(chans[0].id, false);
  if (updateRoute) syncUrl();
  return true;
}

function switchTab(tab) {
  const previousTab = currentTab;
  currentTab = tab;
  if (previousTab !== tab) configChatSelection = null;
  // 任务列表不参与其他页面的轮询,进入看板时拉一次最新数据
  if (tab === "board" && previousTab !== "board")
    void refreshTasks().then(() => renderBoard());
  document.getElementById("chat-view").style.display = tab === "chat" ? "flex" : "none";
  document.getElementById("board-view").style.display = tab === "board" ? "flex" : "none";
  document.getElementById("custom-view").style.display = tab === "custom" ? "block" : "none";
  document.getElementById("docs-view").style.display = tab === "docs" ? "block" : "none";
  document.getElementById("guidelines-view").style.display = tab === "guidelines" ? "block" : "none";
  document.getElementById("skills-view").style.display = tab === "skills" ? "block" : "none";
  document.getElementById("automations-view").style.display =
    tab === "automations" ? "block" : "none";
  document.getElementById("recycle-bin-view").style.display =
    tab === "recycle-bin" ? "block" : "none";
  document.getElementById("proj-view").style.display = tab === "proj" ? "block" : "none";
  document.getElementById("runtime-status-view").style.display =
    tab === "runtime-status" ? "block" : "none";
  document.getElementById("settings-view").style.display = tab === "settings" ? "block" : "none";
  // 全局设置是独立导航项；项目设置是 ⚙；项目内容位于可折叠分区。
  // 任务看板是面板分区中的内置项，因此 board/custom 都激活面板标题。
  document.getElementById("nav-settings").classList.toggle("active", tab === "settings");
  document.getElementById("nav-recycle-bin").classList.toggle(
    "active", tab === "recycle-bin");
  document.getElementById("nav-runtime-status").classList.toggle(
    "active", tab === "runtime-status");
  document.getElementById("proj-cfg").classList.toggle("active", tab === "proj");
  document.getElementById("sec-channels").classList.toggle("active", tab === "chat");
  document.getElementById("sec-boards").classList.toggle(
    "active", tab === "board" || tab === "custom");
  document.getElementById("sec-docs").classList.toggle("active", tab === "docs");
  document.getElementById("sec-guides").classList.toggle("active", tab === "guidelines");
  document.getElementById("sec-skills").classList.toggle("active", tab === "skills");
  document.getElementById("sec-automations").classList.toggle("active", tab === "automations");
  if (tab === "proj") renderProjSettings();
  if (tab === "custom") renderCustomBoards(true);
  if (tab === "docs") renderDocuments();
  if (tab === "automations") renderAutomationPage(true);
  if (tab === "recycle-bin") renderRecycleBin();
  renderProjectConfigPage(tab);
  renderSidebar();
  if (tab === "settings") renderGlobalSettings();
  if (tab === "runtime-status") renderRuntimeStatus(true);
  updateConfigChatContext();
  syncUrl();
}

/* ---- 分层加载 ----
   /api/overview 只含全局数据(projects/backends/role_templates);
   角色/面板/自动化走项目内总览,频道/任务按当前筛选状态另取。
   每个加载器在响应落地时校验项目与筛选未变,避免竞态覆盖。 */
async function loadProjectScope() {
  if (!currentProject) {
    overview.roles = []; overview.channels = []; overview.boards = [];
    overview.automations = []; overview.tasks = [];
    channelCounts = null; taskCounts = null; tasksScopeKey = null;
    return;
  }
  const project = currentProject;
  const [scope] = await Promise.all([
    fetch(`/api/projects/${encodeURIComponent(project)}/overview`)
      .then(r => r.ok ? r.json() : null),
    refreshChannels(),
    // 任务列表只在任务看板打开时参与轮询,其余页面不拉取
    currentTab === "board" ? refreshTasks() : null,
  ]);
  if (!scope || project !== currentProject) return;
  overview.roles = scope.roles;
  overview.boards = scope.boards;
  overview.automations = scope.automations;
}

async function refreshChannels() {
  if (!currentProject) return;
  const project = currentProject, filter = channelFilter;
  const r = await fetch(`/api/projects/${encodeURIComponent(project)}` +
    `/channels?scope=${encodeURIComponent(filter)}`);
  if (!r.ok) return;
  const data = await r.json();
  if (project !== currentProject || filter !== channelFilter) return;
  overview.channels = data.channels;
  channelCounts = data.counts;
}

async function refreshTasks() {
  if (!currentProject) return;
  const project = currentProject, filter = taskFilter;
  const r = await fetch(`/api/projects/${encodeURIComponent(project)}` +
    `/tasks?scope=${encodeURIComponent(filter)}`);
  if (!r.ok) return;
  const data = await r.json();
  if (project !== currentProject || filter !== taskFilter) return;
  overview.tasks = data.tasks;
  taskCounts = data.counts;
  tasksScopeKey = `${project}:${filter}`;
}

async function ensureTasksLoaded() {
  if (tasksScopeKey !== `${currentProject}:${taskFilter}`) await refreshTasks();
}

async function loadOverview() {
  const global = await (await fetch("/api/overview")).json();
  overview.projects = global.projects;
  overview.backends = global.backends;
  overview.role_templates = global.role_templates;
  const sel = document.getElementById("proj-sel");
  if (!overview.projects.length) {
    sel.innerHTML = `<option>(无项目)</option>`;
    currentProject = null;
    await loadProjectScope();
    renderSidebar(); renderBoard(); renderCustomBoards();
    updateConfigChatContext();
    return;
  }
  // 项目优先级:URL hash > localStorage > 第一个项目
  const want = parsePath();
  if (want.project && overview.projects.some(p => p.id === want.project))
    currentProject = want.project;
  if (!overview.projects.some(p => p.id === currentProject))
    currentProject = overview.projects[0].id;
  await loadProjectScope();
  sel.innerHTML = overview.projects.map(p =>
    `<option value="${esc(p.id)}" ${p.id === currentProject ? "selected" : ""}>${esc(p.name || p.id)}</option>`).join("");
  roleColor = Object.fromEntries(projRoles().map(r => [r.id, r.color || "#888"]));
  renderSidebar(); renderBoard(); renderCustomBoards();
  if (currentTab === "docs" && docMode !== "new") renderDocuments(true);
  else if (currentTab === "automations") renderAutomationPage(true);
  else loadDocFiles().then(changed => { if (changed) renderSidebar(); });
  renderProjectConfigPage(currentTab);
  updateConfigChatContext();
  const chans = projChannels();
  if ((!currentChan || !chans.some(c => c.id === currentChan)) && chans.length)
    selectChannel(chans[0].id, false);
  if (!routeRestored) {         // 刷新后还原到 URL 指定的视图/频道
    routeRestored = true;
    applyRoute();
  }
}

// 侧栏各可折叠分区的状态跨会话记忆。
const sideCollapsed = new Set(JSON.parse(localStorage.getItem("mc.sideCollapsed") || "[]"));

function toggleSection(sec) {
  sideCollapsed.has(sec) ? sideCollapsed.delete(sec) : sideCollapsed.add(sec);
  localStorage.setItem("mc.sideCollapsed", JSON.stringify([...sideCollapsed]));
  renderSidebar();
}

function _secState(sec, listId, countId, count) {
  const closed = sideCollapsed.has(sec);
  const head = document.getElementById(`sec-${sec}`);
  if (head) head.querySelector(".caret").textContent = closed ? "▸" : "▾";
  const cnt = document.getElementById(countId);
  if (cnt) cnt.textContent = count ? String(count) : "";
  const list = document.getElementById(listId);
  if (list) list.style.display = closed ? "none" : "block";
  return closed;
}

function renderSidebar() {
  const scrollState = captureScrollPositions(["#side-scroll"]);
  // 频道 -> 聊天
  const chans = visibleProjChannels();
  if (!_secState("channels", "chan-list", "cnt-channels", chans.length))
    document.getElementById("chan-list").innerHTML = chans.map(channelSidebarItem).join("")
      || `<div class="empty" style="padding-left:20px">该筛选下暂无频道</div>`;
  renderChannelFilter();
  // 面板 -> 内置任务看板 + 项目自定义面板。
  const panels = projPanels();
  if (!_secState("boards", "board-list", "cnt-boards", panels.length)) {
    const panelItems = panels.map(panel => panel.builtin
      ? `<div class="side-item panel-item ${currentTab === "board" ? "selected" : ""}"
              data-kind="tasks" data-id="${esc(panel.id)}"
              onclick="openPanelFromSidebar(this.dataset.kind,this.dataset.id)"
              title="${esc(panel.description)}"><span class="panel-name">▦ ${esc(panel.name)}</span><span class="builtin-badge">内置</span></div>`
      : `<div class="side-item panel-item ${panel.id === currentCustomBoard && currentTab === "custom" ? "selected" : ""}"
              data-kind="custom" data-id="${esc(panel.id)}"
              onclick="openPanelFromSidebar(this.dataset.kind,this.dataset.id)"
              title="${esc(panel.description || "")}"><span class="panel-name">${panel.kind === "taskboard" ? "▤" : "▦"} ${esc(panel.name || panel.id)}</span>
          <span class="channel-item-actions">
            <button class="channel-more" type="button" aria-label="面板操作" title="面板操作"
              onclick="togglePanelActions(event,this)">•••</button>
            <span class="channel-actions-menu" hidden onclick="event.stopPropagation()">
              <button type="button" data-id="${esc(panel.id)}"
                onclick="editPanelFromSidebar(this.dataset.id)">编辑面板</button>
              <button class="danger-text" type="button" data-id="${esc(panel.id)}"
                onclick="deletePanelFromSidebar(this.dataset.id)">删除面板</button>
            </span>
          </span></div>`
    ).join("");
    const emptyAction = !currentProject
      ? `<div class="empty" style="padding-left:20px">暂无面板</div>`
      : projBoards().length ? ""
      : `<div class="side-item" onclick="openNewBoardDialog()">＋ 新建面板…</div>`;
    document.getElementById("board-list").innerHTML = panelItems + emptyAction;
  }
  // 文档 -> 文档库视图
  if (!_secState("docs", "doc-list", "cnt-docs", docFiles.length))
    document.getElementById("doc-list").innerHTML = documentSidebarHtml();
  // 本地代码仓 -> 项目设置的代码仓管理;git 管理的仓库带 git 图标与远程地址提示
  const res = projObj()?.repos || [];
  if (!_secState("resources", "resource-list", "cnt-resources", res.length))
    document.getElementById("resource-list").innerHTML = res.map(r =>
      `<div class="side-item" onclick="gotoProjSection('res-table')"
            title="${esc(repoKindLabel(r))}\n${esc(r.path || "")}${r.remote ? "\n远程: " + esc(r.remote) : ""}">
         ${repoIcon(r)} ${esc(r.name || r.id)}</div>`).join("")
      || `<div class="side-item" onclick="quickAddResource()">＋ 添加本地路径或 git 仓…</div>`;
  // 准则 -> 全页逐篇编辑
  const guides = projObj()?.guidelines || [];
  if (!_secState("guides", "guide-list", "cnt-guides", guides.length))
    document.getElementById("guide-list").innerHTML = guides.map(g =>
      `<div class="side-item ${g.name === selectedGuidelineName && currentTab === "guidelines" ? "selected" : ""}" data-name="${esc(g.name)}"
            onclick="openGuidelineFromSidebar(this.dataset.name)" title="${esc(g.description || g.name)}">
         📜 ${esc(g.name)}${g.enabled === false ? " (停用)" : ""}</div>`).join("")
      || `<div class="side-item" onclick="quickNewGuideline()">＋ 写第一篇项目准则…</div>`;
  // Skill -> 全页逐个编辑
  const skills = projObj()?.skills || [];
  if (!_secState("skills", "skill-list", "cnt-skills", skills.length))
    document.getElementById("skill-list").innerHTML = skills.map(s =>
      `<div class="side-item ${s.id === selectedSkillId && currentTab === "skills" ? "selected" : ""}" data-id="${esc(s.id)}"
            onclick="openSkillFromSidebar(this.dataset.id)" title="${esc(s.description || s.id)}">
         ⚡ ${esc(s.name || s.id)}${s.enabled === false ? " (停用)" : ""}</div>`).join("")
      || `<div class="side-item" onclick="quickNewSkill()">＋ 添加第一个 Skill…</div>`;
  // 自动化 -> 独立详情页。
  const automations = projAutomations();
  if (!_secState("automations", "automation-list", "cnt-automations", automations.length))
    document.getElementById("automation-list").innerHTML = automations.map(automationSidebarItem).join("")
      || `<div class="side-item" onclick="openAutomationEditor(null)">＋ 新建自动化脚本…</div>`;
  // 角色 -> 聊天 @(主控带标记);无主控项目改提示文字
  const orch = projObj()?.orchestrator_role_id;
  const roleHelp = document.getElementById("role-help-text");
  if (roleHelp) roleHelp.textContent = peerModeProject()
    ? "输入 @ 后从列表选择角色；本项目没有主控，必须 @ 至少一个角色，多个角色各自启动"
    : "输入 @ 后从列表选择角色；单个角色直接执行，多个角色交给主控协调";
  if (!_secState("roles", "role-list", "cnt-roles", projRoles().length))
    document.getElementById("role-list").innerHTML = projRoles().map(r => {
      const disabled = r.enabled === false;
      return `<div class="role-chip" style="padding-left:20px" data-role-id="${esc(r.id)}"
            ${disabled ? `aria-disabled="true"` :
              `onclick="insertMention(this.dataset.roleId)"`}
            title="${esc(disabled ? roleDisabledReason(r) : r.description)}">
         <span class="role-dot" style="background:${esc(r.color || "#888")}"></span>
         <span>@${esc(r.id)}</span><small>${esc(r.name)}</small>
         ${r.id === orch ? `<span class="pill" style="color:var(--warn);border-color:var(--warn)">主控</span>` : ""}
         ${disabled ? `<span class="pill">${r.usage_auto_disabled ? "用量停用" : "停用"}</span>` : ""}</div>`;
    }).join("");
  document.getElementById("role-bar").innerHTML = activeProjRoles().map(r =>
    `<button data-role-id="${esc(r.id)}" onclick="insertMention(this.dataset.roleId)"
       title="${esc(r.description || `选择 @${r.id}`)}">
       <span class="role-dot" style="background:${esc(r.color || "#888")}"></span>@${esc(r.id)} ${esc(r.name)}
       <span class="role-running-marker" data-runtime-project="${esc(currentProject || "")}"
         data-runtime-role="${esc(r.id)}" hidden><i></i>运行中</span></button>`).join("");
  refreshRuntimeIndicators();
  renderChannelState();
  restoreScrollPositions(scrollState);
}
