/* ---------------- 全局设置:Webhook 来源 ----------------
   来源是全局的投递入口(地址 + 密钥 + 启停),外部系统按来源配置一次;各项目的
   自动化脚本选择来源订阅它,来源收到的每次投递触发所有订阅它的脚本。
   列表不带密钥,密钥只经 /secret 端点按需读取。自动化页也复用这里的缓存与对话框。 */

let webhookSources = { sources: [], listener: { enabled: false } };
// 当前对话框里的来源密钥({sourceId, secret, visible});关闭对话框即作废
let webhookSecretState = null;

async function loadWebhookSources() {
  webhookSources = await api("GET", "/api/webhook-sources");
  return webhookSources;
}

const webhookSourceById = id => (webhookSources.sources || []).find(item => item.id === id);

// 监听器是纯 HTTP,地址按当前访问的主机名拼;外部系统要经端口转发或隧道才能访问
function webhookSourceUrl(source) {
  const listener = webhookSources.listener || {};
  return listener.enabled
    ? `http://${location.hostname}:${listener.port}${source.webhook_path}` : source.webhook_path;
}

function webhookListenerStateHtml() {
  const listener = webhookSources.listener || { enabled: false };
  if (!listener.enabled)
    return '<span class="automation-error">服务未开启 webhook 端口（mc serve --webhook-port）</span>';
  if (listener.listening)
    return `<span class="pill automation-st-succeeded">监听中</span> <span class="muted">${
      esc(listener.host)}:${esc(listener.port)}</span>`;
  return `<span class="automation-error">${esc(listener.error || "未在监听")}</span>`;
}

function webhookSubscribersHtml(source) {
  const users = source.automations || [];
  if (!users.length) return '<span class="muted">未被使用</span>';
  return users.map(item => `<span class="pill" title="${esc(item.id)}">${
    esc(item.project_id)} / ${esc(item.name || item.id)}</span>`).join(" ");
}

async function renderWebhookSources() {
  const table = document.getElementById("webhook-source-table");
  if (!table) return;
  try {
    await loadWebhookSources();
  } catch (e) {
    table.innerHTML = `<tr><td class="empty">Webhook 来源读取失败</td></tr>`;
    return;
  }
  const state = document.getElementById("webhook-listener-state");
  if (state) state.innerHTML = `Webhook 端口：${webhookListenerStateHtml()}`;
  const rows = webhookSources.sources.map(source => {
    const url = webhookSourceUrl(source);
    return `<tr>
      <td><b>${esc(source.name || source.id)}</b><div class="muted">${esc(source.id)}</div></td>
      <td><span class="automation-webhook-value"><code>${esc(url)}</code>
        <button class="ghost compact" type="button" data-copy="${esc(url)}"
          onclick="copyWebhookValue(this.dataset.copy, '投递地址')">复制</button></span></td>
      <td>${source.enabled ? '<span class="pill automation-st-succeeded">启用</span>'
        : '<span class="pill">已停用</span>'}</td>
      <td>${webhookSubscribersHtml(source)}</td>
      <td><button class="ghost" data-id="${esc(source.id)}"
        onclick="editWebhookSource(this.dataset.id)">编辑</button></td></tr>`;
  }).join("");
  table.innerHTML =
    `<tr><th>来源</th><th>投递地址</th><th>状态</th><th>使用的自动化脚本</th><th></th></tr>` +
    (rows || `<tr><td colspan="5" class="empty">还没有 Webhook 来源</td></tr>`);
}

function webhookSecretRowHtml(source) {
  const state = webhookSecretState?.sourceId === source.id ? webhookSecretState : null;
  const secret = state?.secret || "";
  const shown = secret ? (state.visible ? secret : "•".repeat(20)) : "加载中…";
  return `<div class="automation-webhook-value" id="ws-secret-row"><code>${esc(shown)}</code>
    ${secret ? `<button class="ghost compact" type="button" onclick="toggleWebhookSecret()">${
      state.visible ? "隐藏" : "显示"}</button>
    <button class="ghost compact" type="button"
      onclick="copyWebhookValue(webhookSecretState?.secret, '密钥')">复制</button>
    <button class="ghost compact" type="button" data-id="${esc(source.id)}"
      onclick="rotateWebhookSecret(this.dataset.id)">重新生成</button>` : ""}</div>`;
}

function rerenderWebhookSecret() {
  const source = webhookSourceById(webhookSecretState?.sourceId);
  const row = document.getElementById("ws-secret-row");
  if (source && row) row.outerHTML = webhookSecretRowHtml(source);
}

// id 为空 = 新建;revealSecret 用于刚创建后直接显示密钥,方便复制到外部系统
async function editWebhookSource(id, { revealSecret = false } = {}) {
  const source = id ? webhookSourceById(id) : null;
  if (id && !source) { uiAlert("来源不存在,请刷新后重试"); return; }
  webhookSecretState = source ? { sourceId: source.id, secret: "", visible: revealSecret } : null;
  const users = source?.automations || [];
  openFormDialog(source ? `编辑 Webhook 来源 ${source.id}` : "新建 Webhook 来源", `
    <div class="row">
      <div><label>来源 id</label>
        <input type="text" id="ws-id" value="${esc(source?.id || "")}" ${source ? "disabled" : ""}
          placeholder="例如 github-org"></div>
      <div><label>名称</label>
        <input type="text" id="ws-name" value="${esc(source?.name || "")}" placeholder="GitHub 组织"></div>
    </div>
    <label>投递路径(<code>/hooks/</code> 之后的部分;留空 = 同来源 id${
      source ? ";修改后外部系统要同步改地址" : ""})</label>
    <input type="text" id="ws-path" value="${esc(source && source.path !== source.id ? source.path : "")}"
      placeholder="${esc(source?.id || "同来源 id")}">
    <label>描述</label>
    <textarea id="ws-desc" rows="2" placeholder="例如:GitHub 组织级 webhook,推送 issues 与 pull_request 事件">${
      esc(source?.description || "")}</textarea>
    <label class="automation-toggle"><input type="checkbox" id="ws-enabled"
      ${source ? (source.enabled ? "checked" : "") : "checked"}> 接受投递(停用后投递返回 404,订阅关系保留)</label>
    ${source ? `<label>投递地址</label>
      <div class="automation-webhook-value"><code>${esc(webhookSourceUrl(source))}</code>
        <button class="ghost compact" type="button" data-copy="${esc(webhookSourceUrl(source))}"
          onclick="copyWebhookValue(this.dataset.copy, '投递地址')">复制</button></div>
      <label>密钥(重新生成后旧密钥立即失效)</label>
      ${webhookSecretRowHtml(source)}
      <label>使用的自动化脚本</label><div>${webhookSubscribersHtml(source)}</div>` : ""}
    <p class="muted automation-webhook-hint">GitHub：Payload URL 填投递地址，Content type 选 <code>application/json</code>，Secret 填密钥；GitCode 选签名密钥或密码模式，填同一个密钥。对外只转发 webhook 端口，不要暴露主服务端口。一个来源的每次投递会触发所有选择它的脚本，由脚本按 <code>$MISSIONCREW_WEBHOOK_EVENT_FILE</code> 里的事件自行过滤。</p>`,
    `<button class="action" data-id="${esc(source?.id || "")}"
       onclick="saveWebhookSource(this.dataset.id || null)">${source ? "保存" : "创建"}</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>
     ${source ? `<button class="danger" data-id="${esc(source.id)}" ${users.length ? "disabled" : ""}
       title="${users.length ? "仍有自动化脚本使用该来源,先在脚本里改选其他来源" : ""}"
       onclick="deleteWebhookSource(this.dataset.id)">删除来源</button>` : ""}`);
  if (!source) return;
  const { secret } = await api("GET", `/api/webhook-sources/${encodeURIComponent(source.id)}/secret`);
  if (webhookSecretState?.sourceId !== source.id) return;   // 期间已切到别的来源
  webhookSecretState.secret = secret;
  rerenderWebhookSecret();
}

// 来源变化会影响全局设置的列表和自动化页的 webhook 区,两处都刷新
async function refreshWebhookSourceViews() {
  await renderWebhookSources();
  if (currentTab === "automations" && typeof renderAutomationPage === "function")
    renderAutomationPage(true);
}

async function saveWebhookSource(existingId) {
  const id = existingId || document.getElementById("ws-id").value.trim();
  if (!/^[\w-]+$/.test(id)) { uiAlert("来源 id 只能使用字母、数字、下划线和连字符"); return; }
  const saved = await api("POST", "/api/webhook-sources", {
    id,
    name: document.getElementById("ws-name").value.trim(),
    description: document.getElementById("ws-desc").value.trim(),
    path: document.getElementById("ws-path").value.trim(),
    enabled: document.getElementById("ws-enabled").checked,
  });
  fdlg.close();
  await refreshWebhookSourceViews();
  toast(existingId ? `来源 ${saved.id} 已保存` : `来源 ${saved.id} 已创建，复制密钥配置到外部系统`, "success");
  if (!existingId) editWebhookSource(saved.id, { revealSecret: true });
}

async function deleteWebhookSource(id) {
  if (!await uiConfirm(`删除 Webhook 来源 ${id}？外部系统再向它投递会收到 404。`, "删除来源")) return;
  await api("DELETE", `/api/webhook-sources/${encodeURIComponent(id)}`);
  fdlg.close();
  await refreshWebhookSourceViews();
  toast(`来源 ${id} 已删除`, "success");
}

function toggleWebhookSecret() {
  if (!webhookSecretState) return;
  webhookSecretState.visible = !webhookSecretState.visible;
  rerenderWebhookSecret();
}

async function rotateWebhookSecret(id) {
  if (!await uiConfirm(
    "重新生成后旧密钥立即失效，所有向这个来源投递的外部系统（GitHub/GitCode 等）都要同步改成新密钥。是否继续？",
    "重新生成密钥")) return;
  const { secret } = await api("POST", `/api/webhook-sources/${encodeURIComponent(id)}/secret`);
  // 刚生成的新密钥直接显示,方便复制到外部系统
  webhookSecretState = { sourceId: id, secret, visible: true };
  rerenderWebhookSecret();
  toast("已生成新密钥", "success");
}

async function copyWebhookValue(value, label) {
  if (!value) return;
  const copied = await copyTextToClipboard(value);
  toast(copied ? `${label}已复制` : "复制失败，请手动选择复制", copied ? "success" : "error");
}
