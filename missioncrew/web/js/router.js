/* ---- URL 路由:/<项目>/<视图>[/<频道>](History API,干净 URL),
   刷新与前进后退都能还原;服务端对非 API 路径统一返回本页面 ---- */
const TABS = ["chat", "board", "custom", "docs", "guidelines", "skills", "rules",
              "proj", "settings"];

function parsePath() {
  // 兼容旧的 hash 链接(/#/default/settings):路径为根时读 hash
  const raw = location.pathname !== "/" ? location.pathname
            : location.hash.replace(/^#/, "");
  const parts = raw.replace(/^\/+/, "").split("/").map(decodeURIComponent);
  return { project: parts[0] || null,
           tab: TABS.includes(parts[1]) ? parts[1] : "chat",
           chan: parts.slice(2).join("/") || null };
}

function syncUrl(push = true) {
  // 首次路由还原完成前不写 URL:否则加载期的默认频道选择会先把
  // 原始地址(如 /default/settings)覆写成 chat,刷新就回不去了
  if (!routeRestored || !currentProject) return;
  let path = `/${encodeURIComponent(currentProject)}/${currentTab}`;
  if (currentTab === "chat" && currentChan) path += `/${encodeURIComponent(currentChan)}`;
  if (location.pathname === path && !location.hash) return;
  if (push) history.pushState(null, "", path);      // 用户操作:产生历史记录
  else history.replaceState(null, "", path);        // 规范化:不产生历史记录
}

function applyRoute() {
  const r = parsePath();
  if (!r.project) { syncUrl(false); return; }
  if (r.project !== currentProject && overview.projects.some(p => p.id === r.project))
    setProject(r.project);
  if (r.chan && r.chan !== currentChan && projChannels().some(c => c.id === r.chan))
    selectChannel(r.chan, false);
  if (r.tab !== currentTab) switchTab(r.tab);
  syncUrl(false);   // 规范化(清掉无效项目/频道段、旧 hash)
}

window.addEventListener("popstate", applyRoute);

function setProject(id) {
  currentProject = id;
  localStorage.setItem("mc.project", id);
  currentChan = null; lastMsgId = 0; lastMsgDate = "";
  currentCustomBoard = null; customBoardEditing = false;
  selectedGuidelineId = undefined;
  selectedSkillId = undefined;
  selectedRuleIndex = undefined;
  configChatSelection = null;
  configEditorDirty.guidelines = false;
  configEditorDirty.skills = false;
  configEditorDirty.rules = false;
  document.getElementById("msgs").innerHTML = "";
  roleColor = Object.fromEntries(projRoles().map(r => [r.id, r.color || "#888"]));
  docFiles = []; docFilesMeta = []; docSelected = null;
  docMode = "view"; docViewingRevision = null; docHistoryOpen = false;
  docCollapsed.clear();
  renderSidebar(); renderBoard(); renderCustomBoards();
  loadDocFiles().then(renderSidebar);
  if (currentTab === "proj") renderProjSettings();
  if (currentTab === "docs") renderDocuments();
  renderProjectConfigPage(currentTab, true);
  const chans = projChannels();
  if (chans.length) selectChannel(chans[0].id, false);
  syncUrl();
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
  document.getElementById("rules-view").style.display = tab === "rules" ? "block" : "none";
  document.getElementById("proj-view").style.display = tab === "proj" ? "block" : "none";
  document.getElementById("settings-view").style.display = tab === "settings" ? "block" : "none";
  // 侧栏导航:任务看板/全局设置是导航项,项目设置是 ⚙;
  // 频道/面板/文档/角色是并列可折叠分区,激活态落在分区标题上
  document.getElementById("nav-board").classList.toggle("active", tab === "board");
  document.getElementById("nav-settings").classList.toggle("active", tab === "settings");
  document.getElementById("proj-cfg").classList.toggle("active", tab === "proj");
  document.getElementById("sec-channels").classList.toggle("active", tab === "chat");
  document.getElementById("sec-boards").classList.toggle("active", tab === "custom");
  document.getElementById("sec-docs").classList.toggle("active", tab === "docs");
  document.getElementById("sec-guides").classList.toggle("active", tab === "guidelines");
  document.getElementById("sec-skills").classList.toggle("active", tab === "skills");
  document.getElementById("sec-rules").classList.toggle("active", tab === "rules");
  if (tab === "proj") renderProjSettings();
  if (tab === "custom") renderCustomBoards(true);
  if (tab === "docs") renderDocuments();
  renderProjectConfigPage(tab);
  renderSidebar();
  if (tab === "settings") renderGlobalSettings();
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
  loadDocFiles().then(renderSidebar);   // 文档分区的文件清单异步补齐
  renderProjectConfigPage(currentTab);
  if (currentTab === "docs" && docMode === "view") renderDocuments();
  updateConfigChatContext();
  const chans = projChannels();
  if ((!currentChan || !chans.some(c => c.id === currentChan)) && chans.length)
    selectChannel(chans[0].id, false);
  if (!routeRestored) {         // 刷新后还原到 URL 指定的视图/频道
    routeRestored = true;
    applyRoute();
  }
}

// 侧栏四个并列分区(频道/面板/文档/角色)的折叠状态,跨会话记忆
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
  // 频道 -> 聊天
  const chans = projChannels();
  if (!_secState("channels", "chan-list", "cnt-channels", chans.length))
    document.getElementById("chan-list").innerHTML = chans.map(c =>
      `<div class="side-item ${c.id === currentChan && currentTab === "chat" ? "selected" : ""}"
            onclick="selectChannel('${c.id}')" title="${esc(c.purpose || "")}"># ${esc(c.name || c.id)}</div>`).join("")
      || `<div class="empty" style="padding-left:20px">暂无频道</div>`;
  // 面板 -> 自定义面板视图
  const boards = projBoards();
  if (!_secState("boards", "board-list", "cnt-boards", boards.length))
    document.getElementById("board-list").innerHTML = boards.map(b =>
      `<div class="side-item ${b.id === currentCustomBoard && currentTab === "custom" ? "selected" : ""}"
            data-id="${esc(b.id)}" onclick="openBoardFromSidebar(this.dataset.id)"
            title="${esc(b.description || "")}">▦ ${esc(b.name || b.id)}</div>`).join("")
      || `<div class="side-item" onclick="switchTab('custom')">＋ 向主控提一个面板需求…</div>`;
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
      `<div class="side-item ${g.id === selectedGuidelineId && currentTab === "guidelines" ? "selected" : ""}" data-id="${esc(g.id)}"
            onclick="openGuidelineFromSidebar(this.dataset.id)" title="${esc(g.id)}">
         📜 ${esc(g.title || g.id)}${g.enabled === false ? " (停用)" : ""}</div>`).join("")
      || `<div class="side-item" onclick="quickNewGuideline()">＋ 写第一篇项目准则…</div>`;
  // Skill -> 全页逐个编辑
  const skills = projObj()?.skills || [];
  if (!_secState("skills", "skill-list", "cnt-skills", skills.length))
    document.getElementById("skill-list").innerHTML = skills.map(s =>
      `<div class="side-item ${s.id === selectedSkillId && currentTab === "skills" ? "selected" : ""}" data-id="${esc(s.id)}"
            onclick="openSkillFromSidebar(this.dataset.id)" title="${esc(s.description || s.id)}">
         ⚡ ${esc(s.name || s.id)}${s.enabled === false ? " (停用)" : ""}</div>`).join("")
      || `<div class="side-item" onclick="quickNewSkill()">＋ 添加第一个 Skill…</div>`;
  // 验证规则 -> 全页逐条编辑
  const rules = projObj()?.rules || [];
  if (!_secState("rules", "rule-list", "cnt-rules", rules.length))
    document.getElementById("rule-list").innerHTML = rules.map((rule, index) =>
      `<div class="side-item ${index === selectedRuleIndex && currentTab === "rules" ? "selected" : ""}"
            onclick="openRuleFromSidebar(${index})" title="${esc(JSON.stringify(rule.match))}">
         ✓ ${esc(rule.note || JSON.stringify(rule.match))}</div>`).join("")
      || `<div class="side-item" onclick="quickNewRule()">＋ 添加第一条验证规则…</div>`;
  // 角色 -> 聊天 @(主控带标记)
  const orch = projObj()?.orchestrator_role_id;
  if (!_secState("roles", "role-list", "cnt-roles", projRoles().length))
    document.getElementById("role-list").innerHTML = projRoles().map(r =>
      `<div class="role-chip" style="padding-left:20px" onclick="insertMention('${r.id}')" title="${esc(r.description)}">
         <span class="role-dot" style="background:${esc(r.color || "#888")}"></span>
         <span>@${esc(r.id)}</span><small>${esc(r.name)}</small>
         ${r.id === orch ? `<span class="pill" style="color:var(--warn);border-color:var(--warn)">主控</span>` : ""}</div>`).join("");
  document.getElementById("role-bar").innerHTML = projRoles().map(r =>
    `<button onclick="insertMention('${r.id}')" title="${esc(r.description || "")}">
       <span class="role-dot" style="background:${esc(r.color || "#888")}"></span>@${esc(r.id)} ${esc(r.name)}</button>`).join("");
}
