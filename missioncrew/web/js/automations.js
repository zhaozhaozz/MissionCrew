/* ---------------- 自动化脚本(侧栏 + 独立详情页) ---------------- */

const AUTOMATION_STATUS = {
  running: "运行中", succeeded: "成功", failed: "失败", timeout: "超时",
};

const projAutomations = () =>
  (overview.automations || []).filter(item => item.project_id === currentProject);
let selectedAutomationId;
let automationPageRequest = 0;
// undefined = view mode, null = new automation, string = editing automation id
let automationEditingId;
let automationInitialFormSnapshot = "";

function automationShortId(automation) {
  return automation.id.replace(`${automation.project_id}:`, "");
}

function fmtTs(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "—";
}

async function confirmAutomationDiscard(message = "当前脚本尚未保存，继续后将丢失修改。是否继续？") {
  return !automationFormDirty() || uiConfirm(message, "未保存的修改");
}

function clearAutomationEditState() {
  automationEditingId = undefined;
  automationInitialFormSnapshot = "";
}

function automationSidebarItem(automation) {
  const status = automation.running ? "running" : automation.last_status;
  return `<div class="side-item automation-side-item ${
      automation.id === selectedAutomationId && currentTab === "automations" ? "selected" : ""}"
      data-id="${esc(automation.id)}" onclick="selectAutomation(this.dataset.id)"
      title="${esc(automation.description || automation.name || automationShortId(automation))}">
    <span class="automation-side-name">⚙ ${esc(automation.name || automationShortId(automation))}</span>
    ${status ? `<i class="automation-side-status automation-st-${esc(status)}"
      title="${esc(AUTOMATION_STATUS[status] || status)}"></i>` : ""}
  </div>`;
}

async function selectAutomation(id) {
  if (automationEditingId !== undefined && automationEditingId !== id) {
    if (!await confirmAutomationDiscard("当前脚本尚未保存，切换后将丢失修改。是否继续？")) return;
    clearAutomationEditState();
  }
  selectedAutomationId = id;
  if (currentTab !== "automations") switchTab("automations");
  else { renderAutomationPage(true); renderSidebar(); syncUrl(); }
  closeMobileSidebar();
}

function automationRunRows(automation, runs) {
  const shortId = automationShortId(automation);
  return runs.map(run => `<tr>
    <td>#${run.id}</td>
    <td>${esc(run.trigger === "cron" ? "定时" : "手动")}</td>
    <td><span class="pill automation-st-${esc(run.status)}">${esc(AUTOMATION_STATUS[run.status] || run.status)}</span></td>
    <td class="muted">${esc(fmtTs(run.started_at))}</td>
    <td class="muted">${run.finished_at ? `${Math.round(run.finished_at - run.started_at)}s` : "…"}</td>
    <td><button class="ghost compact" data-run="${run.id}"
      onclick="openAutomationRunDetail('${esc(shortId)}', this.dataset.run)">查看输出</button></td>
  </tr>`).join("");
}

function automationDetailHtml(automation, runs) {
  const status = automation.running ? "running" : automation.last_status;
  const schedule = automation.cron ? `<code>${esc(automation.cron)}</code>` : "仅手动触发";
  const triggerMode = automation.cron
    ? (automation.enabled ? "定时与手动" : "仅手动（定时已停用）") : "仅手动";
  const creator = automation.created_by_role_id ? `@${automation.created_by_role_id}` : "human/platform";
  return `<div class="automation-page-head">
      <div><h2>${esc(automation.name || automationShortId(automation))}</h2>
        <span class="muted">${esc(automationShortId(automation))}</span></div>
      <div class="content-topbar-actions">
        <button class="ghost compact" onclick="renderAutomationPage(true)">刷新</button>
        <button class="ghost compact" data-id="${esc(automation.id)}"
          onclick="openAutomationEditor(this.dataset.id)">编辑</button>
        <button class="action compact" data-id="${esc(automation.id)}" ${automation.running ? "disabled" : ""}
          onclick="runAutomationNow(this.dataset.id)">立即运行</button>
        <button class="danger compact" data-id="${esc(automation.id)}"
          onclick="deleteAutomation(this.dataset.id)">删除</button>
      </div>
    </div>
    <section class="automation-overview">
      <div class="automation-description"><h3>描述</h3><p>${esc(automation.description || "未填写描述")}</p></div>
      <dl class="automation-facts">
        <div><dt>下次运行</dt><dd>${esc(fmtTs(automation.next_run_at))}</dd></div>
        <div><dt>最近运行</dt><dd>${status ? `<span class="pill automation-st-${esc(status)}">${esc(AUTOMATION_STATUS[status] || status)}</span>` : "未运行"} · ${esc(fmtTs(automation.last_run_at))}</dd></div>
        <div><dt>创建者</dt><dd>${esc(creator)}</dd></div>
      </dl>
    </section>
    <section class="automation-detail-section">
      <h3>配置</h3>
      <dl class="automation-config">
        <div><dt>触发方式</dt><dd class="automation-trigger-summary">
          <span>${triggerMode}</span><span>${schedule}</span></dd></div>
        <div><dt>超时</dt><dd>${esc(automation.timeout_seconds)} 秒</dd></div>
        <div class="automation-actions-row"><dt>允许的平台动作</dt><dd>${(automation.actions || []).map(action => `<code>${esc(action)}</code>`).join("") || "无"}</dd></div>
      </dl>
    </section>
    <section class="automation-detail-section">
      <h3>脚本预览</h3>
      <pre class="automation-script-preview"><code>${esc(automation.script)}</code></pre>
    </section>
    <section class="automation-detail-section automation-runs-section">
      <h3>运行记录 <span class="muted">最近 ${runs.length} 条</span></h3>
      <div class="runtime-table-wrap"><table class="mgr automation-runs-table">
        <tr><th>#</th><th>触发</th><th>状态</th><th>开始</th><th>耗时</th><th></th></tr>
        ${automationRunRows(automation, runs) || '<tr><td colspan="6" class="empty">还没有运行记录</td></tr>'}
      </table></div>
    </section>`;
}

function automationCronHelpHtml() {
  return `<span class="automation-cron-help" tabindex="0" aria-label="查看 crontab 基础语法"
      aria-describedby="automation-cron-tip">i
    <span class="automation-cron-tip" id="automation-cron-tip" role="tooltip">
      <strong>分 时 日 月 周</strong>
      <span>分 0–59 · 时 0–23 · 日 1–31 · 月 1–12 · 周 0–7（0/7 为周日）</span>
      <span><code>*</code> 任意值 · <code>,</code> 多个值 · <code>-</code> 范围 · <code>/</code> 步长</span>
      <span><code>0 9 * * 1-5</code> 工作日 9:00</span>
      <span><code>*/15 * * * *</code> 每 15 分钟</span>
    </span>
  </span>`;
}

// 平台动作统一命名为 "<资源>.<动作>";编辑器按资源前缀分组,顺序即此表顺序,
// 未登记的前缀排在最后并直接显示前缀本身。
const AUTOMATION_ACTION_GROUPS = {
  task: "任务", document: "文档", message: "消息", channel: "频道",
  dashboard: "面板", board_source: "面板数据源", guideline: "准则",
  skill: "Skill", automation: "自动化脚本", recycle: "回收站",
};

function automationActionGroups(names) {
  const known = Object.keys(AUTOMATION_ACTION_GROUPS);
  const rank = prefix => (known.includes(prefix) ? known.indexOf(prefix) : known.length);
  const groups = new Map();
  for (const name of names) {
    const prefix = name.split(".")[0];
    if (!groups.has(prefix)) groups.set(prefix, []);
    groups.get(prefix).push(name);
  }
  return [...groups].sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));
}

function automationEditorHtml(automation, runsSection = "") {
  const actions = automation?.actions || traitMeta.automation_default_actions || [];
  const actionBoxes = automationActionGroups(traitMeta.automation_actions || [])
    .map(([prefix, names]) => `
    <fieldset class="automation-action-group">
      <legend>${esc(AUTOMATION_ACTION_GROUPS[prefix] || prefix)} <code>${esc(prefix)}</code></legend>
      <div class="automation-actions-grid">${names.map(name => `
        <label class="automation-action-option"><input type="checkbox" value="${esc(name)}"
          ${actions.includes(name) ? "checked" : ""}> ${esc(name)}</label>`).join("")}</div>
    </fieldset>`).join("");
  const status = automation?.running ? "running" : automation?.last_status;
  const creator = automation?.created_by_role_id
    ? `@${automation.created_by_role_id}` : "human/platform";
  const existingId = automation ? `'${esc(automation.id)}'` : "null";
  return `<div class="automation-page-head automation-edit-head">
      <div class="automation-edit-title">
        ${automation
          ? `<input type="text" id="af-name" class="automation-title-input" aria-label="名称"
              value="${esc(automation.name || "")}">
            <span class="muted">${esc(automationShortId(automation))}</span>`
          : `<h2>新建自动化脚本</h2>
            <div class="automation-new-identity">
              <input type="text" id="af-id" aria-label="脚本 id" placeholder="脚本 id，例如 daily-report">
              <input type="text" id="af-name" aria-label="名称" placeholder="名称">
            </div>`}
      </div>
      <div class="content-topbar-actions">
        <button class="action compact" onclick="saveAutomationForm(${existingId})">${automation ? "保存修改" : "创建脚本"}</button>
        <button class="ghost compact" onclick="cancelAutomationEdit()">取消</button>
      </div>
    </div>
    <section class="automation-overview automation-edit-overview form">
      <div class="automation-description"><h3>描述</h3>
        <textarea id="af-desc" class="automation-description-input" rows="3"
          aria-label="用途说明" placeholder="用途说明">${esc(automation?.description || "")}</textarea></div>
      <dl class="automation-facts">
        <div><dt>下次运行</dt><dd>${esc(fmtTs(automation?.next_run_at))}</dd></div>
        <div><dt>最近运行</dt><dd>${status
          ? `<span class="pill automation-st-${esc(status)}">${esc(AUTOMATION_STATUS[status] || status)}</span> · ${esc(fmtTs(automation.last_run_at))}`
          : "未运行"}</dd></div>
        <div><dt>创建者</dt><dd>${esc(automation ? creator : "human/platform")}</dd></div>
      </dl>
    </section>
    <section class="automation-detail-section form">
      <h3>配置</h3>
      <dl class="automation-config automation-edit-config">
        <div><dt>触发方式</dt><dd class="automation-trigger-editor">
          <label class="automation-toggle"><input type="checkbox" id="af-enabled"
            ${automation ? (automation.enabled ? "checked" : "") : "checked"}> 启用定时触发</label>
          <label class="automation-cron-field">
            <span class="automation-cron-label">crontab(五段;留空 = 仅手动触发) ${automationCronHelpHtml()}</span>
            <input type="text" id="af-cron" aria-label="crontab"
              placeholder="例如 0 9 * * 1-5" value="${esc(automation?.cron || "")}">
          </label>
        </dd></div>
        <div><dt>超时</dt><dd><input type="number" id="af-timeout" min="1" aria-label="超时秒数"
          value="${esc(automation?.timeout_seconds ?? 600)}"><span class="muted"> 秒</span></dd></div>
        <div class="automation-actions-row"><dt>允许的平台动作</dt>
          <dd class="automation-action-groups">${actionBoxes}</dd></div>
      </dl>
    </section>
    <section class="automation-detail-section form">
      <h3>脚本内容</h3>
      <p class="muted automation-script-hint">有 shebang 时按可执行文件运行，否则使用 bash；可通过
        <code>missioncrew-tool</code> 环境变量调用平台动作。</p>
      <textarea id="af-script" rows="18" class="automation-script" spellcheck="false"
        placeholder="#!/bin/bash">${esc(automation?.script || "")}</textarea>
    </section>
    ${runsSection}`;
}

function automationFormSnapshot() {
  const field = id => document.getElementById(id);
  return JSON.stringify({
    id: field("af-id")?.value.trim() || "",
    name: field("af-name")?.value || "",
    description: field("af-desc")?.value || "",
    cron: field("af-cron")?.value || "",
    enabled: Boolean(field("af-enabled")?.checked),
    timeout: field("af-timeout")?.value || "",
    actions: [...document.querySelectorAll(".automation-actions-grid input:checked")]
      .map(input => input.value).sort(),
    script: field("af-script")?.value || "",
  });
}

function automationFormDirty() {
  return automationEditingId !== undefined
    && automationInitialFormSnapshot !== automationFormSnapshot();
}

registerViewerDirtyChecker(automationFormDirty);

async function renderAutomationPage(refresh = false) {
  const detail = document.getElementById("automation-detail");
  if (!detail || currentTab !== "automations") return;
  if (automationEditingId !== undefined) { renderSidebar(); return; }
  const request = ++automationPageRequest;
  const workspace = detail.closest(".automation-workspace");
  let list = projAutomations();
  if (selectedAutomationId === undefined
      || (selectedAutomationId !== null && !list.some(item => item.id === selectedAutomationId)))
    selectedAutomationId = list[0]?.id ?? null;
  if (!list.length) {
    detail.dataset.automationId = "";
    detail.innerHTML = `<div class="automation-empty empty"><p>暂无自动化脚本</p>
      <button class="action" onclick="openAutomationEditor(null)">＋ 新建自动化脚本</button></div>`;
    renderSidebar(); syncUrl(); return;
  }
  if (!selectedAutomationId) selectedAutomationId = list[0].id;
  let automation = list.find(item => item.id === selectedAutomationId) || list[0];
  const sameAutomation = detail.dataset.automationId === automation.id;
  const scrollTop = sameAutomation ? workspace?.scrollTop || 0 : 0;
  if (!sameAutomation) {
    detail.dataset.automationId = automation.id;
    detail.innerHTML = automationDetailHtml(automation, []);
    if (workspace) workspace.scrollTop = 0;
  }
  renderSidebar(); syncUrl();
  try {
    if (refresh || automation.next_run_at === undefined) {
      const data = await api("GET",
        `/api/projects/${encodeURIComponent(currentProject)}/automations`);
      if (request !== automationPageRequest) return;
      overview.automations = (overview.automations || [])
        .filter(item => item.project_id !== currentProject).concat(data.automations);
      list = projAutomations();
      automation = list.find(item => item.id === selectedAutomationId) || list[0];
      selectedAutomationId = automation?.id ?? null;
    }
    if (!automation) return renderAutomationPage(false);
    const shortId = automationShortId(automation);
    const data = await api("GET",
      `/api/projects/${encodeURIComponent(currentProject)}/automations/${encodeURIComponent(shortId)}/runs?limit=30`);
    if (request !== automationPageRequest) return;
    detail.dataset.automationId = automation.id;
    detail.innerHTML = automationDetailHtml(automation, data.runs);
    if (workspace) workspace.scrollTop = scrollTop;
    renderSidebar(); syncUrl(false);
  } catch (error) {
    if (request === automationPageRequest)
      detail.insertAdjacentHTML("beforeend", `<p class="automation-error">加载运行记录失败：${esc(error.message || error)}</p>`);
  }
}

async function openAutomationEditor(id) {
  const nextEditingId = id || null;
  if (automationEditingId !== undefined && automationEditingId !== nextEditingId
  && !await confirmAutomationDiscard()) return;
  await ensureTraits();
  const automation = id
    ? projAutomations().find(item => item.id === id) : null;
  if (id && !automation) { uiAlert("脚本不存在,请刷新后重试"); return; }
  const detail = document.getElementById("automation-detail");
  const runsSection = automation && detail?.dataset.automationId === automation.id
    ? detail.querySelector(".automation-runs-section")?.outerHTML || "" : "";
  automationPageRequest++;
  automationEditingId = automation?.id ?? null;
  selectedAutomationId = automation?.id ?? null;
  if (currentTab !== "automations") switchTab("automations");
  if (!detail) return;
  detail.dataset.automationId = automation?.id || "";
  detail.innerHTML = automationEditorHtml(automation, runsSection);
  automationInitialFormSnapshot = automationFormSnapshot();
  detail.closest(".automation-workspace")?.scrollTo({ top: 0 });
  renderSidebar(); syncUrl(); closeMobileSidebar();
  document.getElementById(automation ? "af-name" : "af-id")?.focus();
}

function cancelAutomationEdit() {
  const creating = automationEditingId === null;
  clearAutomationEditState();
  if (creating) selectedAutomationId = projAutomations()[0]?.id ?? null;
  renderAutomationPage(true);
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
  const saved = await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/automations`, {
    id: shortId,
    name: document.getElementById("af-name").value.trim(),
    description: document.getElementById("af-desc").value.trim(),
    script,
    cron: document.getElementById("af-cron").value.trim(),
    enabled: document.getElementById("af-enabled").checked,
    actions,
    timeout_seconds: Number(document.getElementById("af-timeout").value) || 600,
  });
  selectedAutomationId = saved.id;
  clearAutomationEditState();
  await loadOverview();
  if (currentTab !== "automations") switchTab("automations");
  else renderAutomationPage(true);
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
  selectedAutomationId = projAutomations()[0]?.id ?? null;
  renderSidebar();
  if (currentTab === "automations") renderAutomationPage(true);
  toast("脚本已删除", "success");
}

async function runAutomationNow(id) {
  const automation = projAutomations().find(item => item.id === id);
  if (!automation) return;
  const result = await api("POST",
    `/api/projects/${encodeURIComponent(currentProject)}/automations/${encodeURIComponent(automationShortId(automation))}/run`);
  toast(`已触发运行(记录 #${result.run_id});稍后在运行记录中查看输出`, "success");
  if (currentTab === "automations") renderAutomationPage(true);
  setTimeout(() => {
    if (currentTab === "automations" && selectedAutomationId === id)
      renderAutomationPage(true);
  }, 800);
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
    `<button class="ghost" onclick="fdlg.close()">关闭</button>`);
}

/* Task 自动处理规则的配置入口在看板页的标签列(tasks.js)。 */
