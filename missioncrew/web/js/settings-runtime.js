/* ---------------- 全局设置:运行时(仿 Multica Runtime 页) ----------------
   只列支持的工具 + 安装状态/版本/路径 + 启停;成本/能力/档位在角色层配置 */
async function renderGlobalSettings() {
  await ensureTraits();
  await renderBackendTable();
  // 能查到最新版就直接展示:首次进入自动静默查询,不用等用户点按钮
  if (!Object.keys(updateHints).length) checkUpdates(true);
}

let updateHints = {};   // 检查更新的结果:{id: {latest, update_available, installed}}
let updatingIds = new Set();

function closeRuntimeRolePopovers() {
  document.querySelectorAll(".runtime-role-popover").forEach(pop => { pop.hidden = true; });
  document.querySelectorAll(".role-count-btn").forEach(btn => btn.setAttribute("aria-expanded", "false"));
}

function toggleRuntimeRolePopover(event, rowIndex) {
  event.stopPropagation();
  const pop = document.getElementById(`runtime-role-popover-${rowIndex}`);
  const btn = event.currentTarget;
  const shouldOpen = pop.hidden;
  closeRuntimeRolePopovers();
  if (shouldOpen) {
    pop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
  }
}

function roleUsageCell(t, rowIndex) {
  const roles = t.role_users || [];
  if (!roles.length) return `<span class="muted">0</span>`;
  const items = roles.map(r => {
    const project = r.project_name || r.project_id || "未归属项目";
    const model = r.model ? ` · 模型 ${esc(r.model)}` : "";
    return `<div class="runtime-role-item">
      <div><b>@${esc(r.id)}</b>${r.name ? ` · ${esc(r.name)}` : ""}</div>
      <div class="runtime-role-meta">${esc(project)}${model}</div>
    </div>`;
  }).join("");
  return `<span class="runtime-role-usage">
    <button type="button" class="role-count-btn" aria-expanded="false"
      onclick="toggleRuntimeRolePopover(event, ${rowIndex})">${roles.length}</button>
    <div id="runtime-role-popover-${rowIndex}" class="runtime-role-popover" hidden
      onclick="event.stopPropagation()">
      <div class="runtime-role-popover-title">固定使用此运行时的角色</div>${items}
    </div>
  </span>`;
}

document.addEventListener("click", closeRuntimeRolePopovers);
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeRuntimeRolePopovers();
});

function updateCell(t) {
  if (!t.registered || !t.installed) return `<span class="muted">—</span>`;
  const hint = updateHints[t.id];
  if (updatingIds.has(t.id)) return `<span class="muted">更新中…</span>`;
  const btn = label =>
    `<button class="ghost" onclick="runUpdate('${esc(t.id)}')">${label}</button>`;
  if (hint && hint.update_available)
    return `<span class="pill" style="color:var(--warn);border-color:var(--warn)">
        可更新 → ${esc(hint.latest)}</span> ${t.updatable ? btn("更新") : ""}`;
  if (hint && hint.latest && !hint.update_available)   // 能查到最新版就展示出来
    return hint.installed
      ? `<span class="muted">已是最新 (${esc(hint.latest)})</span>`
      : `<span class="muted" title="已装版本未知,无法比对">最新版 ${esc(hint.latest)}</span>`;
  if (t.updatable)   // 没有可靠的最新版来源(如 kimi/trae),仍提供手动更新
    return btn("更新");
  return `<span class="muted" title="安装方式未知或由宿主程序托管">不支持自动更新</span>`;
}

async function renderBackendTable() {
  const tools = await (await fetch("/api/backends/tools")).json();
  // 已安装工具优先；稳定排序会保留每组在支持矩阵中的原始顺序。
  tools.sort((a, b) => Number(b.installed) - Number(a.installed));
  closeRuntimeRolePopovers();
  const rows = tools.map((t, rowIndex) => {
    const status = !t.installed
      ? `<span class="muted">未安装</span>`
      : `<span style="color:var(--ok)">✓ 已安装</span>` +
        (t.version ? ` <span class="pill">${esc(t.version)}</span>` : "");
    const toggle = t.registered
      ? `<span class="switch ${t.enabled ? "on" : ""}" role="switch"
           aria-checked="${t.enabled}" title="${t.enabled ? "已启用,点击停用" : "已停用,点击启用"}"
           onclick="toggleBackend('${esc(t.id)}', ${!t.enabled})"></span>`
      : "";
    return `<tr>
      <td><b>${esc(t.binary)}</b><br><span class="muted">${esc(t.adapter)}</span></td>
      <td>${status}</td>
      <td class="muted">${esc(t.path || "—")}</td>
      <td>${roleUsageCell(t, rowIndex)}</td>
      <td>${updateCell(t)}</td>
      <td>${toggle}</td></tr>`;
  }).join("");
  document.getElementById("backend-table").innerHTML =
    `<tr><th>工具</th><th>状态</th><th>路径</th>` +
    `<th title="仅统计固定到此运行时的角色">使用角色</th><th>更新</th><th></th></tr>` + rows;
}

async function checkUpdates(silent = false) {
  const btn = document.getElementById("btn-check-updates");
  btn.disabled = true; btn.textContent = "检查中…";
  try {
    const results = await api("POST", "/api/backends/check_updates");
    updateHints = Object.fromEntries(results.map(r => [r.id, r]));
    await renderBackendTable();
    if (!silent && !results.some(r => r.update_available)) {
      // "查询不到最新版"不等于"已是最新":断网/无 npm 源时如实说明
      const unknown = results.filter(r => !r.latest).map(r => r.id);
      if (unknown.length === results.length && results.length)
        uiAlert("无法从 npm 源获取任何最新版本信息(可能离线),无法确认是否有更新。");
      else
        uiAlert("可检查的工具都已是最新版本。" +
              (unknown.length ? `\n无法确认(无版本源): ${unknown.join(", ")}` : ""));
    }
  } finally {
    btn.disabled = false; btn.textContent = "检查更新";
  }
}

async function runUpdate(id) {
  const hint = updateHints[id];
  const target = hint && hint.latest ? `到 ${hint.latest}` : "到最新版本";
  if (!await uiConfirm(`更新 ${id} ${target}?更新期间该工具不会被派发新的执行。`)) return;
  updatingIds.add(id);
  await renderBackendTable();
  try {
    const r = await api("POST", `/api/backends/${encodeURIComponent(id)}/update`);
    uiAlert(r.ok
      ? `更新完成:${r.old_version || "?"} → ${r.version || "?"}`
      : `更新失败:\n${r.log || "(无输出)"}`);
    if (r.ok) delete updateHints[id];   // 失败时版本未变,"可更新"提示仍有效
  } finally {
    updatingIds.delete(id);
    await renderBackendTable();          // 先清"更新中…",再刷新全局数据
    try { await loadOverview(); } catch (e) { /* 服务重启间隙,忽略 */ }
    await renderBackendTable();
  }
}

async function toggleBackend(id, enabled) {
  await api("POST", "/api/backends", { id, enabled });
  await loadOverview(); renderBackendTable();
  toast(enabled ? `已启用 ${id}` : `已停用 ${id}`, "success");
}

async function detectBackends() {
  const d = await api("POST", "/api/backends/detect");
  uiAlert(`检测到: ${d.found.join(", ") || "无"}\n新注册: ${d.added.join(", ") || "无"}\n已刷新: ${d.updated.join(", ") || "无"}`);
  updateHints = {};        // 已装版本可能已变,旧比对结果作废
  await loadOverview();
  await renderBackendTable();
  checkUpdates(true);      // 用新版本静默重新比对
}
