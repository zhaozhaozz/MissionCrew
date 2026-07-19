/* ---------------- 新建项目(侧栏 + 号) ---------------- */
function openNewProject() {
  document.getElementById("np-id").value = "";
  document.getElementById("np-name").value = "";
  document.getElementById("np-desc").value = "";
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

