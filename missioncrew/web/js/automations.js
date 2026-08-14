/* ---------------- 自动化脚本 + Task 自动处理规则(项目设置页) ---------------- */

const AUTOMATION_STATUS = {
  running: "运行中", succeeded: "成功", failed: "失败", timeout: "超时",
};

const projAutomations = () =>
  (overview.automations || []).filter(item => item.project_id === currentProject);

function automationShortId(automation) {
  return automation.id.replace(`${automation.project_id}:`, "");
}

function fmtTs(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "—";
}

async function renderAutomationTable() {
  let list = [];
  try {
    list = (await api("GET",
      `/api/projects/${encodeURIComponent(currentProject)}/automations`)).automations;
  } catch (e) { /* 项目不存在或服务重启间隙 */ }
  const rows = list.map(a => {
    const status = a.running ? "running" : a.last_status;
    const schedule = a.cron
      ? `<code>${esc(a.cron)}</code>${a.enabled ? "" : ' <span class="badge">已停用</span>'}`
      : '<span class="muted">仅手动</span>';
    return `<tr>
      <td><b>${esc(a.name)}</b> <span class="muted">${esc(automationShortId(a))}</span><br>
        <span class="muted">${esc(a.description || "")}</span></td>
      <td>${schedule}<br><span class="muted">下次:${esc(fmtTs(a.next_run_at))}</span></td>
      <td>${status ? `<span class="pill automation-st-${esc(status)}">${esc(AUTOMATION_STATUS[status] || status)}</span>` : '<span class="muted">未运行</span>'}<br>
        <span class="muted">${esc(fmtTs(a.last_run_at))}</span></td>
      <td>
        <button class="ghost" data-id="${esc(a.id)}" onclick="openAutomationEditor(this.dataset.id)">查看/编辑</button>
        <button class="ghost" data-id="${esc(a.id)}" onclick="openAutomationRuns(this.dataset.id)">运行记录</button>
        <button class="action" data-id="${esc(a.id)}" ${a.running ? "disabled" : ""}
          onclick="runAutomationNow(this.dataset.id)">立即运行</button>
        <button class="danger" data-id="${esc(a.id)}" onclick="deleteAutomation(this.dataset.id)">删除</button>
      </td></tr>`;
  }).join("");
  const table = document.getElementById("automation-table");
  if (table) table.innerHTML =
    `<tr><th>脚本</th><th>定时(crontab)</th><th>最近运行</th><th></th></tr>` +
    (rows || `<tr><td colspan="4" class="empty">暂无自动化脚本;可手动新建,或在频道里让主控用 automation.save 编写。</td></tr>`);
}

function openAutomationEditor(id) {
  const automation = id
    ? projAutomations().find(item => item.id === id) : null;
  if (id && !automation) { uiAlert("脚本不存在,请刷新后重试"); return; }
  const actions = automation?.actions
    || traitMeta.automation_default_actions || [];
  const allActions = traitMeta.automation_actions || [];
  const actionBoxes = allActions.map(name => `
    <label class="automation-action-option"><input type="checkbox" value="${esc(name)}"
      ${actions.includes(name) ? "checked" : ""}> ${esc(name)}</label>`).join("");
  openFormDialog(automation ? `编辑脚本 · ${automation.name}` : "新建自动化脚本", `
    <div class="row">
      <div><label>脚本 id(创建后不可改)</label>
        <input type="text" id="af-id" placeholder="例如 daily-report"
          value="${automation ? esc(automationShortId(automation)) : ""}"
          ${automation ? "disabled" : ""}></div>
      <div><label>名称</label><input type="text" id="af-name"
        value="${esc(automation?.name || "")}"></div>
    </div>
    <label>用途说明</label>
    <input type="text" id="af-desc" value="${esc(automation?.description || "")}">
    <div class="row">
      <div><label>crontab(五段;留空 = 仅手动触发)</label>
        <input type="text" id="af-cron" placeholder="例如 0 9 * * 1-5"
          value="${esc(automation?.cron || "")}"></div>
      <div><label>超时(秒)</label><input type="number" id="af-timeout" min="1"
        value="${esc(automation?.timeout_seconds ?? 600)}"></div>
    </div>
    <label><input type="checkbox" id="af-enabled"
      ${automation ? (automation.enabled ? "checked" : "") : "checked"}> 启用定时触发</label>
    <label>允许调用的平台动作(脚本 token 白名单)</label>
    <div class="automation-actions-grid">${actionBoxes}</div>
    <label>脚本内容(有 shebang 按可执行文件运行,否则用 bash;可用
      <code>missioncrew-tool</code> 环境变量调用平台动作)</label>
    <textarea id="af-script" rows="14" class="automation-script" spellcheck="false"
      placeholder="#!/bin/bash&#10;# 例:向 general 频道发布巡检报告&#10;&quot;$MISSIONCREW_AGENT_TOOL_PYTHON&quot; -m missioncrew.agent_tool publish-message --channel general --content &quot;巡检正常&quot;">${esc(automation?.script || "")}</textarea>`,
    `<button class="action" onclick="saveAutomationForm(${automation ? `'${esc(automation.id)}'` : "null"})">${automation ? "保存修改" : "创建脚本"}</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
}

async function saveAutomationForm(existingId) {
  const shortId = existingId
    ? existingId.replace(`${currentProject}:`, "")
    : document.getElementById("af-id").value.trim();
  if (!shortId) { uiAlert("脚本 id 不能为空"); return; }
  const script = document.getElementById("af-script").value;
  if (!script.trim()) { uiAlert("脚本内容不能为空"); return; }
  const actions = [...document.querySelectorAll(
    ".automation-actions-grid input:checked")].map(input => input.value);
  if (!actions.length) { uiAlert("至少勾选一个允许的动作"); return; }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/automations`, {
    id: shortId,
    name: document.getElementById("af-name").value.trim(),
    description: document.getElementById("af-desc").value.trim(),
    script,
    cron: document.getElementById("af-cron").value.trim(),
    enabled: document.getElementById("af-enabled").checked,
    actions,
    timeout_seconds: Number(document.getElementById("af-timeout").value) || 600,
  });
  fdlg.close();
  await loadOverview();
  renderAutomationTable();
  toast(existingId ? "脚本已更新" : "脚本已创建", "success");
}

async function deleteAutomation(id) {
  const automation = projAutomations().find(item => item.id === id);
  if (!automation || !await uiConfirm(
    `删除自动化脚本「${automation.name}」及其全部运行记录?此操作不可恢复。`,
    "删除脚本")) return;
  await api("DELETE",
    `/api/projects/${encodeURIComponent(currentProject)}/automations/${encodeURIComponent(automationShortId(automation))}`);
  await loadOverview();
  renderAutomationTable();
  toast("脚本已删除", "success");
}

async function runAutomationNow(id) {
  const automation = projAutomations().find(item => item.id === id);
  if (!automation) return;
  const result = await api("POST",
    `/api/projects/${encodeURIComponent(currentProject)}/automations/${encodeURIComponent(automationShortId(automation))}/run`);
  toast(`已触发运行(记录 #${result.run_id});稍后在运行记录中查看输出`, "success");
  setTimeout(renderAutomationTable, 800);
}

async function openAutomationRuns(id) {
  const automation = projAutomations().find(item => item.id === id);
  if (!automation) return;
  const shortId = automationShortId(automation);
  const data = await api("GET",
    `/api/projects/${encodeURIComponent(currentProject)}/automations/${encodeURIComponent(shortId)}/runs?limit=30`);
  const rows = data.runs.map(run => `<tr>
    <td>#${run.id}</td>
    <td>${esc(run.trigger === "cron" ? "定时" : "手动")}</td>
    <td><span class="pill automation-st-${esc(run.status)}">${esc(AUTOMATION_STATUS[run.status] || run.status)}</span></td>
    <td class="muted">${esc(fmtTs(run.started_at))}</td>
    <td class="muted">${run.finished_at ? `${Math.round(run.finished_at - run.started_at)}s` : "…"}</td>
    <td><button class="ghost" data-run="${run.id}"
      onclick="openAutomationRunDetail('${esc(shortId)}', this.dataset.run)">输出</button></td>
  </tr>`).join("");
  openFormDialog(`运行记录 · ${automation.name}`, `
    <table class="mgr"><tr><th>#</th><th>触发</th><th>状态</th><th>开始</th><th>耗时</th><th></th></tr>
    ${rows || '<tr><td colspan="6" class="empty">还没有运行记录</td></tr>'}</table>`,
    `<button class="ghost" onclick="fdlg.close()">关闭</button>`);
}

async function openAutomationRunDetail(shortId, runId) {
  const run = await api("GET",
    `/api/projects/${encodeURIComponent(currentProject)}/automations/${encodeURIComponent(shortId)}/runs/${runId}`);
  const section = (title, text) => text
    ? `<label>${title}</label><pre class="automation-output">${esc(text)}</pre>` : "";
  openFormDialog(`运行 #${run.id} · ${esc(AUTOMATION_STATUS[run.status] || run.status)}`, `
    <p class="muted" style="margin-top:0">开始 ${esc(fmtTs(run.started_at))} ·
      结束 ${esc(fmtTs(run.finished_at))} · 退出码 ${run.exit_code ?? "—"}</p>
    ${run.error ? `<p class="automation-error">${esc(run.error)}</p>` : ""}
    ${section("stdout", run.stdout)}
    ${section("stderr", run.stderr)}
    ${!run.stdout && !run.stderr ? '<p class="empty">本次运行没有输出</p>' : ""}`,
    `<button class="ghost" onclick="openAutomationRuns('${esc(currentProject)}:${esc(shortId)}')">← 返回列表</button>
     <button class="ghost" onclick="fdlg.close()">关闭</button>`);
}

/* ---------------- Task 自动处理规则 ---------------- */

function projTaskRules() {
  return projObj()?.task_auto_rules || [];
}

function renderTaskRuleTable() {
  const rules = projTaskRules();
  const rows = rules.map((rule, index) => `<tr>
    <td><span class="badge">${esc(rule.label)}</span></td>
    <td>${rule.role_ids.length
      ? rule.role_ids.map(id => `@${esc(id)}`).join("、")
      : '<span class="muted">项目主控</span>'}</td>
    <td class="muted task-rule-prompt">${esc(rule.prompt || "(无默认提示词)")}</td>
    <td>${rule.enabled ? '<span class="badge">启用</span>' : '<span class="muted">停用</span>'}</td>
    <td>
      <button class="ghost" data-index="${index}" onclick="openTaskRuleEditor(Number(this.dataset.index))">编辑</button>
      <button class="danger" data-index="${index}" onclick="deleteTaskRule(Number(this.dataset.index))">删除</button>
    </td></tr>`).join("");
  const table = document.getElementById("task-rule-table");
  if (table) table.innerHTML =
    `<tr><th>命中 label</th><th>处理角色</th><th>默认提示词</th><th>状态</th><th></th></tr>` +
    (rows || `<tr><td colspan="5" class="empty">暂无规则;新建 Task(含脚本同步的 Task)命中规则 label 时会自动派发。</td></tr>`);
}

function openTaskRuleEditor(index) {
  const rule = index >= 0 ? projTaskRules()[index] : null;
  if (index >= 0 && !rule) return;
  const roleBoxes = activeProjRoles().map(role => `
    <label class="automation-action-option"><input type="checkbox" value="${esc(role.id)}"
      ${rule?.role_ids?.includes(role.id) ? "checked" : ""}>
      @${esc(role.id)}(${esc(role.name || role.id)})</label>`).join("");
  openFormDialog(rule ? `编辑规则 · ${rule.label}` : "新建 Task 自动处理规则", `
    <label>命中 label(Task 含该标签即触发;不区分大小写)</label>
    <input type="text" id="tr-label" placeholder="例如 sync/alert" value="${esc(rule?.label || "")}">
    <label>处理角色(不勾选 = 交给项目主控;单角色直接执行,多角色由主控协调)</label>
    <div class="automation-actions-grid">${roleBoxes || '<span class="empty">当前项目没有已启用角色</span>'}</div>
    <label>默认提示词(作为派发消息里的处理要求)</label>
    <textarea id="tr-prompt" rows="4" style="height:auto"
      placeholder="例如:请分析这条告警的影响面,给出处理建议并更新状态简报">${esc(rule?.prompt || "")}</textarea>
    <label><input type="checkbox" id="tr-enabled" ${rule ? (rule.enabled ? "checked" : "") : "checked"}>
      启用本规则</label>`,
    `<button class="action" onclick="saveTaskRule(${index})">${rule ? "保存修改" : "创建规则"}</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
}

async function persistTaskRules(rules) {
  const project = projObj();
  await api("POST", "/api/projects", {
    id: currentProject,
    name: project.name,
    description: project.description,
    orchestrator_role_id: project.orchestrator_role_id,
    max_chain_runs: project.max_chain_runs,
    charter: project.charter,
    task_auto_rules: rules,
  });
  await loadOverview();
  renderTaskRuleTable();
}

async function saveTaskRule(index) {
  const label = document.getElementById("tr-label").value.trim();
  if (!label) { uiAlert("label 不能为空"); return; }
  const rule = {
    label,
    role_ids: [...document.querySelectorAll(
      "#fdlg-body .automation-actions-grid input:checked")].map(input => input.value),
    prompt: document.getElementById("tr-prompt").value,
    enabled: document.getElementById("tr-enabled").checked,
  };
  const rules = projTaskRules().map(item => ({ ...item }));
  if (index >= 0) rules[index] = rule; else rules.push(rule);
  await persistTaskRules(rules);
  fdlg.close();
  toast(index >= 0 ? "规则已更新" : "规则已创建", "success");
}

async function deleteTaskRule(index) {
  const rules = projTaskRules().map(item => ({ ...item }));
  const rule = rules[index];
  if (!rule || !await uiConfirm(`删除 label「${rule.label}」的自动处理规则?`)) return;
  rules.splice(index, 1);
  await persistTaskRules(rules);
  toast("规则已删除", "success");
}
