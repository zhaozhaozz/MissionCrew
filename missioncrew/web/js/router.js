/* ---- URL 路由:/<项目>/<视图>[/<频道>](History API,干净 URL),
   刷新与前进后退都能还原;服务端对非 API 路径统一返回本页面 ---- */
const TABS = ["chat", "board", "custom", "docs", "guidelines", "skills",
              "proj", "runtime-status", "settings"];
let routeApplying = false;

function emptyRoute(project = null, tab = "chat") {
  return { project, tab, chan: null, doc: null, task: null, dashboard: null,
           guideline: undefined, skill: undefined, skillFile: null };
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
  if (currentTab === "docs" && docMode === "view" && !docViewingRevision)
    path = missionCrewDocumentUrl(currentProject, docSelected || "");
  if (currentTab === "guidelines")
    path = missionCrewResourceUrl(currentProject, "guidelines",
      ...(selectedGuidelineName ? [selectedGuidelineName] : []));
  if (currentTab === "skills")
    path = missionCrewResourceUrl(currentProject, "skills",
      ...(selectedSkillId ? [selectedSkillId] : []),
      ...(selectedSkillId && skillOpenFile ? skillOpenFile.split("/") : []));
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
    if (r.project !== currentProject) setProject(r.project, false);

    if (r.tab === "chat") {
      const channel = projChannels().find(item =>
        item.id === r.chan || item.id === `${r.project}:${r.chan}`)
        || (!r.chan ? projChannels()[0] : null);
      if (channel && channel.id !== currentChan) selectChannel(channel.id, false);
      if (channel?.archived && channelFilter === "active") {
        channelFilter = "all";
        localStorage.setItem("mc.channelFilter", channelFilter);
      }
    }

    const documentChanged = r.tab === "docs" && r.doc !== docSelected;
    if (r.tab === "docs") {
      docSelected = r.doc;
      docMode = "view";
      docViewingRevision = null;
      docHistoryOpen = false;
      resetDocumentVersionCompare();
    }
    if (r.tab === "custom") {
      const board = projBoards().find(item =>
        item.id === r.dashboard || item.id === `${r.project}:${r.dashboard}`);
      currentCustomBoard = board?.id || null;
      customBoardEditing = false;
      boardEditorVisible = false;
    }
    if (r.tab === "guidelines")
      selectedGuidelineName = (projObj()?.guidelines || [])
        .some(item => item.name === r.guideline) ? r.guideline : undefined;
    if (r.tab === "skills") {
      selectedSkillId = (projObj()?.skills || [])
        .some(item => item.id === r.skill) ? r.skill : undefined;
      skillOpenFile = selectedSkillId ? r.skillFile : null;
    }

    if (r.tab !== "board" || !r.task) closeTaskDialog(false);
    if (r.tab !== currentTab) switchTab(r.tab);
    else if (documentChanged) await renderDocuments();
    else if (r.tab === "custom") renderCustomBoards(true);
    else if (r.tab === "guidelines") renderGuidelinesPage(true);
    else if (r.tab === "skills") renderSkillsPage(true);

    if (r.tab === "board" && r.task) {
      const task = projTasks().find(item => item.id === r.task);
      if (task) await openTask(task.id, false);
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

function setProject(id, updateRoute = true) {
  currentProject = id;
  localStorage.setItem("mc.project", id);
  currentChan = null; lastMsgId = 0; lastMsgDate = "";
  currentCustomBoard = null; customBoardEditing = false;
  selectedGuidelineName = undefined;
  guidelineMarkdownMode = "preview";
  guidelineHistoryOpen = false;
  guidelineViewingVersion = null;
  selectedSkillId = undefined;
  skillMarkdownMode = "preview";
  skillOpenFile = null;
  skillLibraryInfo = null;
  skillFolderImportOpen = false;
  configChatSelection = null;
  configEditorDirty.guidelines = false;
  configEditorDirty.skills = false;
  document.getElementById("msgs").innerHTML = "";
  roleColor = Object.fromEntries(projRoles().map(r => [r.id, r.color || "#888"]));
  docFiles = []; docFilesMeta = []; docSelected = null;
  docMode = "view"; docViewingRevision = null; docHistoryOpen = false;
  resetDocumentVersionCompare();
  docCollapsed.clear();
  closeTaskDialog(false);
  renderSidebar(); renderBoard(); renderCustomBoards();
  if (currentTab === "proj") renderProjSettings();
  if (currentTab === "docs") renderDocuments();
  else loadDocFiles().then(changed => { if (changed) renderSidebar(); });
  renderProjectConfigPage(currentTab, true);
  const chans = projChannels();
  if (chans.length) selectChannel(chans[0].id, false);
  if (updateRoute) syncUrl();
}

function switchTab(tab) {
  const previousTab = currentTab;
  currentTab = tab;
  if (previousTab !== tab) configChatSelection = null;
  document.getElementById("chat-view").style.display = tab === "chat" ? "flex" : "none";
  document.getElementById("board-view").style.display = tab === "board" ? "block" : "none";
  document.getElementById("custom-view").style.display = tab === "custom" ? "block" : "none";
  document.getElementById("docs-view").style.display = tab === "docs" ? "block" : "none";
  document.getElementById("guidelines-view").style.display = tab === "guidelines" ? "block" : "none";
  document.getElementById("skills-view").style.display = tab === "skills" ? "block" : "none";
  document.getElementById("proj-view").style.display = tab === "proj" ? "block" : "none";
  document.getElementById("runtime-status-view").style.display =
    tab === "runtime-status" ? "block" : "none";
  document.getElementById("settings-view").style.display = tab === "settings" ? "block" : "none";
  // 全局设置是独立导航项；项目设置是 ⚙；项目内容位于可折叠分区。
  // 任务看板是面板分区中的内置项，因此 board/custom 都激活面板标题。
  document.getElementById("nav-settings").classList.toggle("active", tab === "settings");
  document.getElementById("nav-runtime-status").classList.toggle(
    "active", tab === "runtime-status");
  document.getElementById("proj-cfg").classList.toggle("active", tab === "proj");
  document.getElementById("sec-channels").classList.toggle("active", tab === "chat");
  document.getElementById("sec-boards").classList.toggle(
    "active", tab === "board" || tab === "custom");
  document.getElementById("sec-docs").classList.toggle("active", tab === "docs");
  document.getElementById("sec-guides").classList.toggle("active", tab === "guidelines");
  document.getElementById("sec-skills").classList.toggle("active", tab === "skills");
  if (tab === "proj") renderProjSettings();
  if (tab === "custom") renderCustomBoards(true);
  if (tab === "docs") renderDocuments();
  renderProjectConfigPage(tab);
  renderSidebar();
  if (tab === "settings") renderGlobalSettings();
  if (tab === "runtime-status") renderRuntimeStatus(true);
  updateConfigChatContext();
  syncUrl();
}

async function loadOverview() {
  overview = await (await fetch("/api/overview")).json();
  const sel = document.getElementById("proj-sel");
  if (!overview.projects.length) {
    sel.innerHTML = `<option>(无项目)</option>`;
    currentProject = null;
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
  sel.innerHTML = overview.projects.map(p =>
    `<option value="${esc(p.id)}" ${p.id === currentProject ? "selected" : ""}>${esc(p.name || p.id)}</option>`).join("");
  roleColor = Object.fromEntries(projRoles().map(r => [r.id, r.color || "#888"]));
  renderSidebar(); renderBoard(); renderCustomBoards();
  if (currentTab === "docs" && docMode === "view") renderDocuments(true);
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
              title="${esc(panel.description)}">▦ ${esc(panel.name)}<span class="builtin-badge">内置</span></div>`
      : `<div class="side-item panel-item ${panel.id === currentCustomBoard && currentTab === "custom" ? "selected" : ""}"
              data-kind="custom" data-id="${esc(panel.id)}"
              onclick="openPanelFromSidebar(this.dataset.kind,this.dataset.id)"
              title="${esc(panel.description || "")}">▦ ${esc(panel.name || panel.id)}</div>`
    ).join("");
    const emptyAction = !currentProject
      ? `<div class="empty" style="padding-left:20px">暂无面板</div>`
      : projBoards().length ? ""
      : `<div class="side-item" onclick="requestBoardFocus()">＋ 向主控提一个面板需求…</div>`;
    document.getElementById("board-list").innerHTML = panelItems + emptyAction;
  }
  // 文档 -> 文档库视图
  if (!_secState("docs", "doc-list", "cnt-docs", docFiles.length))
    document.getElementById("doc-list").innerHTML = documentSidebarHtml();
  // 资源 -> 项目设置资源管理;本地 git 仓自动带远程标记
  const res = projObj()?.repos || [];
  if (!_secState("resources", "resource-list", "cnt-resources", res.length))
    document.getElementById("resource-list").innerHTML = res.map(r =>
      `<div class="side-item" onclick="gotoProjSection('res-table')"
            title="${esc(r.path || "")}${r.remote ? "\n远程: " + esc(r.remote) : ""}">
         ${r.kind === "git" ? "🔗" : "📁"} ${esc(r.name || r.id)}</div>`).join("")
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
  // 角色 -> 聊天 @(主控带标记)
  const orch = projObj()?.orchestrator_role_id;
  if (!_secState("roles", "role-list", "cnt-roles", projRoles().length))
    document.getElementById("role-list").innerHTML = projRoles().map(r =>
      `<div class="role-chip" style="padding-left:20px" data-role-id="${esc(r.id)}"
            onclick="insertMention(this.dataset.roleId)" title="${esc(r.description)}">
         <span class="role-dot" style="background:${esc(r.color || "#888")}"></span>
         <span>@${esc(r.id)}</span><small>${esc(r.name)}</small>
         ${r.id === orch ? `<span class="pill" style="color:var(--warn);border-color:var(--warn)">主控</span>` : ""}</div>`).join("");
  document.getElementById("role-bar").innerHTML = projRoles().map(r =>
    `<button data-role-id="${esc(r.id)}" onclick="insertMention(this.dataset.roleId)"
       title="选择后会创建可触发执行的提及。${esc(r.description || "")}">
       <span class="role-dot" style="background:${esc(r.color || "#888")}"></span>@${esc(r.id)} ${esc(r.name)}</button>`).join("");
  renderChannelState();
  restoreScrollPositions(scrollState);
}
