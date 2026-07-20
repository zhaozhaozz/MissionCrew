/* ---------------- 新建项目(侧栏 + 号) ---------------- */
function openNewProject() {
  document.getElementById("np-id").value = "";
  document.getElementById("np-name").value = "";
  document.getElementById("np-desc").value = "";
  const templates = globalRoleTemplates();
  document.getElementById("np-role-summary").textContent = templates.length
    ? `将复制 ${templates.length} 个全局角色模板，默认主控为 @${templates[0].id}；并创建 general 频道。`
    : "当前没有全局角色模板，请先到全局设置中配置。";
  pdlg.showModal();
}

function openNewTask() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  document.getElementById("nt-title").value = "";
  document.getElementById("nt-desc").value = "";
  document.getElementById("nt-labels").value = "";
  tdlg.showModal();
}

async function createTask() {
  const title = document.getElementById("nt-title").value.trim();
  if (!title) { uiAlert("标题不能为空"); return; }
  await api("POST", "/api/tasks", {
    project_id: currentProject, title,
    description: document.getElementById("nt-desc").value,
    task_type: document.getElementById("nt-type").value,
    risk: document.getElementById("nt-risk").value,
    labels: document.getElementById("nt-labels").value
      .split(",").map(s => s.trim()).filter(Boolean),
  });
  tdlg.close();
  await loadOverview(); renderBoard();
  toast("任务已创建", "success");
}

async function createProject() {
  const id = document.getElementById("np-id").value.trim();
  if (!id) { uiAlert("项目 id 不能为空"); return; }
  await api("POST", "/api/projects", {
    id,
    name: document.getElementById("np-name").value.trim() || id,
    description: document.getElementById("np-desc").value.trim(),
  });
  pdlg.close();
  await loadOverview();
  setProject(id);
  switchTab("proj");   // 引导补充章程与准则
  toast("项目已创建,请补充章程与准则", "success");
}
