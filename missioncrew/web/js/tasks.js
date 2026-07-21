/* ---------------- 看板 ---------------- */
const COLS = [
  { title: "进行中 / 待推进", color: "var(--accent)", match: t => t.status === "open" },
  { title: "等待审批", color: "var(--warn)", match: t => t.status === "awaiting_approval" },
  { title: "阻塞 / 失败", color: "var(--bad)", match: t => ["blocked", "failed"].includes(t.status) },
  { title: "已完成", color: "var(--ok)", match: t => t.status === "done" },
];

function renderBoard() {
  const scrollState = captureScrollPositions(["#board-view"]);
  document.getElementById("board").innerHTML = COLS.map(col => {
    const items = projTasks().filter(col.match);
    const cards = items.map(t => {
      const stage = t.stages[t.stage_index]?.name ?? "—";
      return `<div class="card" onclick="openTask('${t.id}')">
        <div class="title">${esc(t.title)}</div>
        <div class="meta">${t.id} · 阶段 ${esc(stage)}</div>
        <div class="meta">
          <span class="badge">${esc(t.task_type)}</span>
          <span class="badge ${t.risk === "high" ? "hi" : ""}">风险 ${esc(t.risk)}</span>
          ${t.labels.map(l => `<span class="badge">${esc(l)}</span>`).join("")}
        </div></div>`;
    }).join("") || `<div class="empty" style="padding:6px 4px">暂无任务</div>`;
    return `<section class="col"><h2><span class="col-dot" style="background:${col.color}"></span>
      ${col.title}<span class="col-count">${items.length}</span></h2>${cards}</section>`;
  }).join("");
  restoreScrollPositions(scrollState);
}

async function openTask(id) {
  const d = await (await fetch(`/api/tasks/${id}`)).json();
  const t = d.task;
  document.getElementById("dlg-title").textContent = `${t.id} · ${t.title}`;
  const stages = t.stages.map((s, i) => `<tr>
      <td>${i === t.stage_index ? "▶" : ""}</td><td>${esc(s.name)}</td><td>${esc(s.kind)}</td>
      <td><span class="pill st-${esc(s.status)}">${esc(s.status)}</span></td>
      <td>${esc(s.backend_id ?? "")}</td><td>${s.attempts || ""}</td>
      <td>${[s.independent ? "独立" : "", s.human_gate ? "人工审批" : ""].filter(Boolean).join(" · ")}</td>
    </tr>`).join("");
  const evidence = d.evidence.map(e => `<tr><td>${esc(e.type)}</td><td>${esc(e.stage)}</td>
      <td>${esc(e.path)}</td><td>${esc(e.summary)}</td></tr>`).join("");
  const runs = d.runs.map(r2 => `<tr><td>${r2.success ? "✓" : "✗"}</td><td>${esc(r2.stage)}</td>
      <td>${esc(r2.backend_id)}</td><td>${esc(r2.tier)}</td><td>${r2.cost}</td>
      <td>${esc(r2.summary)}</td></tr>`).join("");
  const audit = d.audit.slice(0, 30).map(a => `<tr>
      <td>${new Date(a.ts * 1000).toLocaleTimeString()}</td><td>${esc(a.actor)}</td>
      <td>${esc(a.action)}</td><td>${esc(a.detail)}</td></tr>`).join("");
  document.getElementById("dlg-body").innerHTML = `
    <h3>阶段计划</h3><table><tr><th></th><th>阶段</th><th>类型</th><th>状态</th>
      <th>执行者</th><th>重试</th><th>门禁</th></tr>${stages}</table>
    <h3>证据 (${d.evidence.length})</h3>
    <table><tr><th>类型</th><th>阶段</th><th>路径</th><th>摘要</th></tr>
      ${evidence || "<tr><td colspan=4 class=empty>暂无</td></tr>"}</table>
    <h3>执行记录</h3><table><tr><th></th><th>阶段</th><th>后端</th><th>档位</th><th>成本</th><th>摘要</th></tr>
      ${runs || "<tr><td colspan=6 class=empty>暂无</td></tr>"}</table>
    <h3>审计</h3><table><tr><th>时间</th><th>主体</th><th>动作</th><th>详情</th></tr>${audit}</table>`;
  const actions = [];
  if (!["done", "failed"].includes(t.status))
    actions.push(`<button class="action" onclick="advanceTask('${t.id}')">推进</button>`);
  if (t.status === "awaiting_approval") {
    actions.push(`<button class="action" onclick="approveTask('${t.id}', 'approved')">批准</button>`);
    actions.push(`<button class="ghost" onclick="approveTask('${t.id}', 'rejected')">拒绝</button>`);
  }
  document.getElementById("dlg-actions").innerHTML = actions.join("");
  dlg.showModal();
}

async function advanceTask(id) {
  await fetch(`/api/tasks/${id}/advance`, { method: "POST" });
  await loadOverview(); await openTask(id);
}
async function approveTask(id, decision) {
  const approver = await uiPrompt("审批人:", { value: "human" });
  if (!approver) return;
  await fetch(`/api/tasks/${id}/approve`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approver, decision }),
  });
  await loadOverview(); await openTask(id);
  toast(decision === "approved" ? "已批准" : "已拒绝", decision === "approved" ? "success" : "info");
}
