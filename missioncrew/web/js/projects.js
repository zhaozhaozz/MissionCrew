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
  if (await setProject(id) === false) return;
  switchTab("proj");   // 引导补充章程与准则
  toast("项目已创建,请补充章程与准则", "success");
}
