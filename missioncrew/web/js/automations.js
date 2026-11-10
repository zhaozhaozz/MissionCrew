/* ---------------- 自动化脚本(侧栏 + 独立详情页) ---------------- */

const AUTOMATION_STATUS = {
  running: "运行中", succeeded: "成功", failed: "失败", timeout: "超时",
};
const AUTOMATION_TRIGGERS = { cron: "定时", webhook: "Webhook", manual: "手动" };

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
    <td title="${esc(run.trigger_detail || "")}">${esc(AUTOMATION_TRIGGERS[run.trigger] || run.trigger)}</td>
    <td><span class="pill automation-st-${esc(run.status)}">${esc(AUTOMATION_STATUS[run.status] || run.status)}</span></td>
    <td class="muted">${esc(fmtTs(run.started_at))}</td>
    <td class="muted">${run.finished_at ? `${Math.round(run.finished_at - run.started_at)}s` : "…"}</td>
    <td><button class="ghost compact" data-run="${run.id}"
      onclick="openAutomationRunDetail('${esc(shortId)}', this.dataset.run)">查看输出</button></td>
  </tr>`).join("");
}

// 触发方式摘要:定时(启用时)、Webhook(开启时)与始终可用的手动
function automationTriggerMode(automation) {
  const modes = [automation.cron && automation.enabled ? "定时" : "",
                 automation.webhook_source ? "Webhook" : "", "手动"].filter(Boolean);
  return modes.join("、") + (automation.cron && !automation.enabled ? "（定时已停用）" : "");
}

// webhook 区只展示订阅的全局来源;来源的地址、密钥与启停在「全局设置 → Webhook 来源」
// 维护(settings-webhooks.js),这里的「管理来源」打开同一个编辑框
function automationWebhookHtml(automation) {
  if (!automation.webhook_source) return "";
  const source = webhookSourceById(automation.webhook_source);
  const id = esc(automation.webhook_source);
  const others = (source?.automations || []).filter(item => item.id !== automation.id);
  const url = source ? webhookSourceUrl(source) : automation.webhook_path;
  // GitHub 轮询来源没有投递地址与端口:改为展示查询对象与轮询状态
  if (source?.kind === "github")
    return `<section class="automation-detail-section automation-webhook-section">
      <h3>Webhook</h3>
      <dl class="automation-config">
        <div><dt>来源</dt><dd class="automation-webhook-value">
          <span>${esc(source.name || source.id)}</span> <span class="muted">${id}</span>
          <button class="ghost compact" type="button" data-id="${id}"
            onclick="editWebhookSource(this.dataset.id)">管理来源</button></dd></div>
        <div><dt>订阅的仓库</dt><dd>${automationGithubReposHtml(automation, source)}</dd></div>
        <div><dt>事件</dt><dd>${(automation.github_events || []).length
          ? automation.github_events.map(name => `<code>${esc(name)}</code>`).join(" ")
          : '<span class="muted">全部</span>'}${source.github?.notifications
          ? ' <span class="pill">来源含账号通知</span>' : ""}</dd></div>
        <div><dt>状态</dt><dd>${githubPollStateHtml(source)}</dd></div>
        <div><dt>共用此来源</dt><dd>${others.length
          ? others.map(item => `<span class="pill" title="${esc(item.id)}">${esc(item.project_id)} / ${esc(item.name || item.id)}</span>`).join(" ")
          : '<span class="muted">无其他脚本</span>'}</dd></div>
      </dl>
      <p class="muted automation-webhook-hint">平台定时向 GitHub 查询这个脚本订阅的仓库，每个新事件触发一次脚本，与 Webhook 投递同形：<code>$MISSIONCREW_TRIGGER</code> 为 <code>webhook</code>，事件内容在 <code>$MISSIONCREW_WEBHOOK_EVENT_FILE</code>，由脚本决定处理哪些事件。</p>
    </section>`;
  return `<section class="automation-detail-section automation-webhook-section">
      <h3>Webhook</h3>
      <dl class="automation-config">
        <div><dt>来源</dt><dd class="automation-webhook-value">
          <span>${esc(source?.name || automation.webhook_source)}</span> <span class="muted">${id}</span>
          ${source ? (source.enabled ? "" : '<span class="pill">已停用，投递返回 404</span>') : '<span class="muted">加载中…</span>'}
          <button class="ghost compact" type="button" data-id="${id}"
            onclick="editWebhookSource(this.dataset.id)">管理来源</button></dd></div>
        <div><dt>投递地址</dt><dd class="automation-webhook-value"><code>${esc(url)}</code>
          <button class="ghost compact" type="button" data-copy="${esc(url)}"
            onclick="copyWebhookValue(this.dataset.copy, '投递地址')">复制</button></dd></div>
        <div><dt>端口</dt><dd>${source ? webhookListenerStateHtml() : "加载中…"}</dd></div>
        <div><dt>共用此来源</dt><dd>${others.length
          ? others.map(item => `<span class="pill" title="${esc(item.id)}">${esc(item.project_id)} / ${esc(item.name || item.id)}</span>`).join(" ")
          : '<span class="muted">无其他脚本</span>'}</dd></div>
      </dl>
      <p class="muted automation-webhook-hint">来源收到的每次投递都会触发所有选择它的脚本。脚本用 <code>$MISSIONCREW_TRIGGER</code> 区分触发来源，事件内容在 <code>$MISSIONCREW_WEBHOOK_EVENT_FILE</code>，由脚本决定处理哪些事件。密钥在「管理来源」里查看。</p>
    </section>`;
}

function automationDetailHtml(automation, runs) {
  const status = automation.running ? "running" : automation.last_status;
  const schedule = automation.cron ? `<code>${esc(automation.cron)}</code>` : "";
  const triggerMode = automationTriggerMode(automation);
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
          <span>${triggerMode}</span>${schedule ? `<span>${schedule}</span>` : ""}</dd></div>
        <div><dt>超时</dt><dd>${esc(automation.timeout_seconds)} 秒</dd></div>
        <div class="automation-actions-row"><dt>允许的平台动作</dt><dd>${(automation.actions || []).map(action => `<code>${esc(action)}</code>`).join("") || "无"}</dd></div>
      </dl>
    </section>
    ${automationWebhookHtml(automation)}
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

// 本脚本订阅的仓库;平台轮询不到的(不存在或 token 无权访问)在仓库名后标出原因
function automationGithubReposHtml(automation, source) {
  const repos = automation.github_repos || [];
  if (!repos.length) return '<span class="muted">未订阅仓库</span>';
  const streams = new Map((source.poll?.streams || []).map(item => [item.key, item]));
  return repos.map(repo => {
    const error = streams.get(`repo:${repo.toLowerCase()}`)?.error;
    return `<code>${esc(repo)}</code>${error ? ` <span class="automation-error">${esc(error)}</span>` : ""}`;
  }).join(" ");
}

// 选中的来源是 GitHub 轮询来源时才显示仓库与事件两栏
function toggleAutomationGithubFields() {
  const box = document.getElementById("af-github-fields");
  if (!box) return;
  const source = webhookSourceById(document.getElementById("af-webhook-source").value);
  box.hidden = source?.kind !== "github";
}

// 选择订阅的全局 webhook 来源;来源在全局设置里创建,可被多个脚本共用
function automationWebhookSourceFieldHtml(automation) {
  const current = automation?.webhook_source || "";
  const sources = webhookSources.sources || [];
  let options = sources.map(source => `<option value="${esc(source.id)}"
      ${source.id === current ? "selected" : ""}>${esc(source.name || source.id)}（${
      esc(source.kind === "github" ? "GitHub 轮询" : source.webhook_path)}）${source.enabled ? "" : " · 已停用"}</option>`).join("");
  // 来源列表没读到时保留现值,避免保存时悄悄取消订阅
  if (current && !sources.some(source => source.id === current))
    options += `<option value="${esc(current)}" selected>${esc(current)}</option>`;
  return `<label class="automation-cron-field">
      <span class="automation-cron-label">Webhook 来源（可与定时同时使用）</span>
      <select id="af-webhook-source" aria-label="Webhook 来源" onchange="toggleAutomationGithubFields()">
        <option value="">不接受 Webhook 触发</option>${options}
      </select>
      <span class="muted">来源（Webhook 的投递地址与密钥，或 GitHub 轮询的身份与间隔）在<a onclick="switchTab('settings')">全局设置 → Webhook 来源 ↗</a>中创建，同一来源可被多个脚本共用</span>
    </label>
    <div id="af-github-fields" ${sources.find(source => source.id === current)?.kind === "github" ? "" : "hidden"}>
      <label class="automation-cron-field">
        <span class="automation-cron-label">订阅的 GitHub 仓库（每行一个 <code>owner/repo</code>）</span>
        <textarea id="af-github-repos" rows="2" spellcheck="false" placeholder="acme/webshop">${
          esc((automation?.github_repos || []).join("\n"))}</textarea>
        <span class="muted">平台只把这些仓库的事件交给本脚本；新加的仓库从保存那一刻起算，之前的事件不投递</span>
      </label>
      <label class="automation-cron-field">
        <span class="automation-cron-label">只要这些事件（逗号分隔，留空 = 全部）</span>
        <input type="text" id="af-github-events" spellcheck="false" placeholder="issues, pull_request"
          value="${esc((automation?.github_events || []).join(", "))}">
        <span class="muted">事件名同 webhook：<code>issues</code>、<code>pull_request</code>（合并是 action <code>merged</code>）、<code>issue_comment</code>、<code>push</code>、<code>release</code> 等；来源开启账号通知时另有 <code>notification</code></span>
      </label>
    </div>`;
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
            <span class="automation-cron-label">crontab(五段;留空 = 不定时) ${automationCronHelpHtml()}</span>
            <input type="text" id="af-cron" aria-label="crontab"
              placeholder="例如 0 9 * * 1-5" value="${esc(automation?.cron || "")}">
          </label>
          ${automationWebhookSourceFieldHtml(automation)}
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
    webhook_source: field("af-webhook-source")?.value || "",
    github_repos: field("af-github-repos")?.value || "",
    github_events: field("af-github-events")?.value || "",
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
    const base = `/api/projects/${encodeURIComponent(currentProject)}/automations/${encodeURIComponent(shortId)}`;
    const [data] = await Promise.all([
      api("GET", `${base}/runs?limit=30`),
      automation.webhook_source ? loadWebhookSources() : null,
    ]);
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
  // 来源列表读取失败时编辑框照常打开,只是下拉框里没有可选来源
  await Promise.all([ensureTraits(), loadWebhookSources().catch(() => null)]);
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
  // 仓库与事件只对 GitHub 轮询来源有意义;换成别的来源时一并清空
  const sourceId = document.getElementById("af-webhook-source").value;
  const githubSource = webhookSourceById(sourceId)?.kind === "github";
  const listOf = id => githubSource
    ? document.getElementById(id).value.split(/[\s,]+/).filter(Boolean) : [];
  const saved = await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/automations`, {
    id: shortId,
    name: document.getElementById("af-name").value.trim(),
    description: document.getElementById("af-desc").value.trim(),
    script,
    cron: document.getElementById("af-cron").value.trim(),
    enabled: document.getElementById("af-enabled").checked,
    webhook_source: sourceId,
    github_repos: listOf("af-github-repos"),
    github_events: listOf("af-github-events"),
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
  const trigger = AUTOMATION_TRIGGERS[run.trigger] || run.trigger;
  openFormDialog(`运行 #${run.id} · ${esc(AUTOMATION_STATUS[run.status] || run.status)}`, `
    <p class="muted" style="margin-top:0">触发 ${esc(trigger)}${run.trigger_detail ? `（${esc(run.trigger_detail)}）` : ""} ·
      开始 ${esc(fmtTs(run.started_at))} ·
      结束 ${esc(fmtTs(run.finished_at))} · 退出码 ${run.exit_code ?? "—"}</p>
    ${run.error ? `<p class="automation-error">${esc(run.error)}</p>` : ""}
    ${section("stdout", run.stdout)}
    ${section("stderr", run.stderr)}
    ${!run.stdout && !run.stderr ? '<p class="empty">本次运行没有输出</p>' : ""}`,
    `<button class="ghost" onclick="fdlg.close()">关闭</button>`);
}

/* Task 自动处理规则的配置入口在看板页的标签列(tasks.js)。 */
