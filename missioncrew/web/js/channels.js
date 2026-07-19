/* ---- 频道(当前项目) ---- */
function renderChanTable() {
  const rows = projChannels().map(c => `<tr>
    <td><b># ${esc(c.name || c.id)}</b> <span class="muted">${esc(c.id)}</span></td>
    <td>${esc(c.purpose || "(未说明)")}</td>
    <td class="muted">${esc(c.workdir || "(平台内置工作区)")}</td>
    <td class="muted">${esc(c.created_by_role_id ? "@" + c.created_by_role_id : "human/platform")}</td>
    <td><button class="danger" onclick="deleteChannel('${c.id}')">删除</button></td>
  </tr>`).join("");
  document.getElementById("chan-table").innerHTML =
    `<tr><th>频道</th><th>用途</th><th>工作目录</th><th>创建者</th><th></th></tr>` + rows;
}

function openChannelDialog() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  const repoOpts = (projObj()?.repos || []).filter(r => r.path).map(r =>
    `<option value="${esc(r.path)}">${esc(r.name || r.id)} — ${esc(r.path)}</option>`).join("");
  openFormDialog("新建频道", `
    <label>频道名(字母、数字、下划线、连字符)</label>
    <input type="text" id="nc-id" placeholder="例如 backend-repo">
    <label>频道用途/任务边界</label>
    <input type="text" id="nc-purpose" placeholder="例如：结算模块需求澄清与实现讨论">
    <label>工作目录(可选;默认平台内置工作区)</label>
    ${repoOpts ? `<select id="nc-workdir-sel" onchange="document.getElementById('nc-workdir').value=this.value">
      <option value="">(平台内置工作区)</option>${repoOpts}</select>` : ""}
    <input type="text" id="nc-workdir" placeholder="或直接输入路径 ~/code/myrepo" style="margin-top:6px">`,
    `<button class="action" onclick="createChannel()">创建频道</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
  setTimeout(() => document.getElementById("nc-id")?.focus(), 60);
}

async function createChannel() {
  const id = document.getElementById("nc-id").value.trim();
  if (!id) { uiAlert("频道名不能为空"); return; }
  await api("POST", "/api/chat/channels", {
    id, name: id,
    project_id: currentProject,
    purpose: document.getElementById("nc-purpose").value.trim(),
    workdir: document.getElementById("nc-workdir").value.trim() || null,
  });
  fdlg.close();
  await loadOverview(); renderChanTable(); renderSidebar();
  toast("频道已创建", "success");
}

async function deleteChannel(id) {
  if (!await uiConfirm(`删除频道 #${id}?(消息记录保留在数据库)`)) return;
  await api("DELETE", `/api/chat/channels/${id}`);
  if (currentChan === id) currentChan = null;
  await loadOverview(); renderChanTable(); renderSidebar();
  toast("频道已删除", "success");
}

