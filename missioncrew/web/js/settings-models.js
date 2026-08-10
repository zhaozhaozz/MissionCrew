/* ---------------- 全局设置:自定义模型接入 ----------------
   平台对外提供的能力是"接入任意 OpenAI / Anthropic 兼容 API";具体由哪个
   Runtime 执行这些模型是实现细节,页面只在该 Runtime 缺失时给一句提示。 */

const API_LABELS = {
  "openai-completions": "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
  "anthropic-messages": "Anthropic Messages",
  "google-generative-ai": "Google Generative AI",
};

let modelProviders = { config: { providers: {} }, apis: [], executor: {}, path: "" };

const providerEntries = () =>
  Object.entries(modelProviders.config.providers || {});

const modelIds = spec => (spec.models || [])
  .map(m => (typeof m === "string" ? m : m && m.id))
  .filter(Boolean);

async function renderModelProviders() {
  const table = document.getElementById("model-provider-table");
  if (!table) return;
  try {
    modelProviders = await api("GET", "/api/model-providers");
  } catch (e) {
    table.innerHTML = `<tr><td class="empty">接入配置读取失败</td></tr>`;
    return;
  }
  renderModelProviderExecutorHint();
  const rows = providerEntries().map(([name, spec]) => {
    const ids = modelIds(spec);
    const shown = ids.slice(0, 4).map(id => `<span class="pill">${esc(id)}</span>`).join(" ");
    const more = ids.length > 4 ? ` <span class="muted">+${ids.length - 4}</span>` : "";
    return `<tr>
      <td><b>${esc(name)}</b></td>
      <td class="muted">${esc(API_LABELS[spec.api] || spec.api || "—")}</td>
      <td class="muted">${esc(spec.baseUrl || "—")}</td>
      <td>${apiKeyCell(spec)}</td>
      <td>${shown || `<span class="muted">未声明模型</span>`}${more}</td>
      <td><button class="ghost" onclick="editModelProvider('${esc(name)}')">编辑</button></td></tr>`;
  }).join("");
  table.innerHTML =
    `<tr><th>接入名</th><th>接口协议</th><th>Base URL</th><th>API Key</th>` +
    `<th>模型</th><th></th></tr>` +
    (rows || `<tr><td colspan="6" class="empty">尚未接入自定义 API</td></tr>`);
}

/* 密钥不回传浏览器:$ENV_VAR 是引用可以显示,字面量只说明"已保存" */
function apiKeyCell(spec) {
  const ref = String(spec.apiKey || "");
  if (ref.startsWith("$"))
    return `<code>${esc(ref)}</code> <span class="muted">环境变量</span>`;
  if (spec.apiKeySaved) return `<span class="muted">已保存</span>`;
  return `<span class="muted">未设置</span>`;
}

function renderModelProviderExecutorHint() {
  const box = document.getElementById("model-provider-hint");
  if (!box) return;
  const exec = modelProviders.executor || {};
  const count = providerEntries().length;
  let warn = "";
  if (!exec.installed)
    warn = `执行这些模型的运行时 <b>${esc(exec.id || "")}</b> 尚未安装，` +
      `请先在上方运行时列表中检测安装，接入的模型才会真正可用。`;
  else if (!exec.registered || !exec.enabled)
    warn = `执行这些模型的运行时 <b>${esc(exec.id || "")}</b> 已安装但未启用，` +
      `请在上方运行时列表中启用它。`;
  else if (count && !(exec.models || []).length)
    warn = `配置已保存，但当前没有可用执行单元，请检查接入配置是否被运行时接受。`;
  box.innerHTML = warn
    ? `<span style="color:var(--warn)">${warn}</span>`
    : (count ? `当前共 ${count} 个接入、` +
        `${(exec.models || []).length} 个可选模型。` : "");
  box.hidden = !box.innerHTML;
}

function editModelProvider(name) {
  const spec = name ? (modelProviders.config.providers[name] || {}) : {};
  // 新接入默认 OpenAI Chat Completions:兼容它的端点最多,否则会落到
  // 协议列表里字母序第一个,与实际最常见的接入形态不符。
  const api = spec.api || "openai-completions";
  const apis = (modelProviders.apis || []).map(id =>
    `<option value="${esc(id)}" ${api === id ? "selected" : ""}>` +
    `${esc(API_LABELS[id] || id)}</option>`).join("");
  openFormDialog(name ? `编辑接入 ${name}` : "接入 OpenAI / Anthropic 兼容 API", `
    <div class="row">
      <div><label>接入名(作为模型前缀,如 <code>接入名/模型 id</code>)</label>
        <input type="text" id="mp-name" value="${esc(name || "")}" ${name ? "disabled" : ""}
          placeholder="my-gateway"></div>
      <div><label>接口协议</label><select id="mp-api">${apis}</select></div>
    </div>
    <label>Base URL</label>
    <input type="text" id="mp-base" value="${esc(spec.baseUrl || "")}"
      placeholder="https://api.example.com/v1">
    <label>API Key(可填字面量,或填 <code>$环境变量名</code> 只保存引用${
      name ? ";留空表示不修改" : ""})</label>
    <input type="password" id="mp-key" value="${esc(spec.apiKey || "")}"
      placeholder="${name && spec.apiKeySaved ? "已保存,留空则不修改" : "sk-... 或 $OPENAI_API_KEY"}">
    <label>模型 id(每行一个;顺序即角色配置里的展示顺序)</label>
    <textarea id="mp-models" rows="6"
      placeholder="gpt-5.2&#10;gpt-5.2-mini">${esc(modelIds(spec).join("\n"))}</textarea>`,
    `<button class="action" onclick="saveModelProvider(${name ? `'${esc(name)}'` : "null"})">保存</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>
     ${name ? `<button class="danger" onclick="deleteModelProvider('${esc(name)}')">删除接入</button>` : ""}`);
}

async function saveModelProvider(original) {
  const name = document.getElementById("mp-name").value.trim();
  const baseUrl = document.getElementById("mp-base").value.trim();
  const ids = document.getElementById("mp-models").value
    .split("\n").map(line => line.trim()).filter(Boolean);
  if (!name) { uiAlert("接入名不能为空"); return; }
  // 接入名会拼进 "接入名/模型 id",斜杠等字符会让模型解析歧义
  if (!/^[A-Za-z0-9._-]+$/.test(name)) {
    uiAlert("接入名只能使用字母、数字和 . _ -"); return;
  }
  if (!baseUrl) { uiAlert("Base URL 不能为空"); return; }
  if (!ids.length) { uiAlert("至少声明一个模型 id"); return; }
  if (!original && modelProviders.config.providers[name]) {
    uiAlert(`接入名 ${name} 已存在`); return;
  }
  const prev = original ? (modelProviders.config.providers[original] || {}) : {};
  // 保留手工写入的额外字段(provider 的 compat、模型的 contextWindow/cost 等),
  // 界面只负责它暴露的那几项,不该把没展示的配置清掉。
  const prevModels = new Map((prev.models || [])
    .filter(m => m && typeof m === "object" && m.id).map(m => [m.id, m]));
  const spec = {
    ...prev,
    baseUrl,
    api: document.getElementById("mp-api").value,
    apiKey: document.getElementById("mp-key").value.trim(),
    models: ids.map(id => prevModels.get(id) || { id }),
  };
  delete spec.apiKeySaved;
  // 先赋值再删旧名:同名编辑时键位不变,列表不会因为一次编辑跳到末尾。
  const providers = { ...modelProviders.config.providers };
  providers[name] = spec;
  if (original && original !== name) delete providers[original];
  await putModelProviders(providers, `接入 ${name} 已保存`);
}

async function deleteModelProvider(name) {
  if (!await uiConfirm(`删除接入 ${name}？固定使用其模型的角色需要重新选择模型。`)) return;
  const providers = { ...modelProviders.config.providers };
  delete providers[name];
  await putModelProviders(providers, `接入 ${name} 已删除`);
}

async function putModelProviders(providers, message) {
  const result = await api("PUT", "/api/model-providers", { providers });
  fdlg.close();
  // 模型清单已变,角色编辑框的缓存必须作废,否则仍会展示旧的可选模型。
  delete modelCatalogCache[(result.executor || {}).id];
  await loadOverview();
  await renderModelProviders();
  toast(message, "success");
}
