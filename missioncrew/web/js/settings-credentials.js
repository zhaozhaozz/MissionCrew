/* ---------------- 凭据:全局共享 + 项目独有(项目同名覆盖全局) ----------------
   值只写不读:页面只提交新值,接口从不返回值,列表只显示末 4 位指纹,要改就整份替换。
   Agent 从工作区 credentials/ 的文件读取;Agent 缺凭据时经 credential.request 登记成
   "待填写"条目,人在这里填。聊天发送前检测疑似密钥,引导存成凭据而不是发进聊天。 */

let globalCredentials = [];
let projectCredentials = { credentials: [], inherited: [] };

const credentialApiBase = scope => scope === "global" ? "/api/credentials"
  : `/api/projects/${encodeURIComponent(currentProject)}/credentials`;

function credentialStateHtml(item) {
  if (!item.has_value)
    return `<span class="pill st-in_progress" title="${esc(item.requested_by
      ? `${item.requested_by} 请求,填写后 Agent 即可读取` : "填写后 Agent 即可读取")}">待填写</span>`;
  return `<span class="pill st-done">已保存</span> <span class="muted">${esc(item.fingerprint)}</span>`;
}

function credentialRowsHtml(items, scope, { inherited = false } = {}) {
  return items.map(item => {
    const note = inherited && item.overridden
      ? `<span class="pill" title="本项目有同名凭据,Agent 读到的是项目那份">已被项目覆盖</span>` : "";
    const actions = inherited
      ? (item.overridden ? "" : `<button class="ghost" data-name="${esc(item.name)}"
          onclick="editCredential('project', null, this.dataset.name)">在本项目覆盖</button>`)
      : `<button class="ghost" data-name="${esc(item.name)}"
          onclick="editCredential('${scope}', this.dataset.name)">${item.has_value ? "替换" : "填写"}</button>
         <button class="danger" data-name="${esc(item.name)}"
          onclick="deleteCredential('${scope}', this.dataset.name)">删除</button>`;
    return `<tr><td><code>${esc(item.name)}</code> ${note}</td>
      <td class="muted">${esc(item.description || "—")}</td>
      <td>${credentialStateHtml(item)}</td><td>${actions}</td></tr>`;
  }).join("");
}

const CREDENTIAL_TABLE_HEAD = "<tr><th>名称</th><th>用途</th><th>状态</th><th></th></tr>";

async function renderGlobalCredentials() {
  const table = document.getElementById("global-credential-table");
  if (!table) return;
  try {
    globalCredentials = (await api("GET", "/api/credentials")).credentials;
  } catch (e) {
    table.innerHTML = `<tr><td class="empty">凭据读取失败</td></tr>`;
    return;
  }
  table.innerHTML = CREDENTIAL_TABLE_HEAD + (credentialRowsHtml(globalCredentials, "global")
    || `<tr><td colspan="4" class="empty">还没有全局凭据</td></tr>`);
  renderGithubIdentity("global");   // 核对要访问 GitHub,不阻塞列表
}

async function renderProjectCredentials() {
  const table = document.getElementById("proj-credential-table");
  const inheritedTable = document.getElementById("proj-inherited-credential-table");
  if (!table || !currentProject) return;
  try {
    projectCredentials = await api("GET", credentialApiBase("project"));
  } catch (e) {
    table.innerHTML = `<tr><td class="empty">凭据读取失败</td></tr>`;
    return;
  }
  // 待填写(Agent 请求的)排在最前,打开页面先看到要处理的
  const own = [...projectCredentials.credentials].sort((a, b) => a.has_value - b.has_value);
  table.innerHTML = CREDENTIAL_TABLE_HEAD + (credentialRowsHtml(own, "project")
    || `<tr><td colspan="4" class="empty">本项目还没有自己的凭据</td></tr>`);
  if (inheritedTable)
    inheritedTable.innerHTML = CREDENTIAL_TABLE_HEAD + (
      credentialRowsHtml(projectCredentials.inherited, "global", { inherited: true })
      || `<tr><td colspan="4" class="empty">没有全局凭据</td></tr>`);
  renderGithubIdentity("project");
}

/* ---- GitHub 身份:名为 GITHUB_TOKEN 的凭据(个人访问令牌)就是项目在 GitHub 上的身份 ----
   保存后平台让 Agent 与自动化脚本里的 gh、git 直接以它登录;这里显示核对结果
   (账号、令牌类型、权限、到期时间),token 本身仍走凭据表单,只写不读。 */
const GITHUB_TOKEN_NAME = "GITHUB_TOKEN";
const GITHUB_TOKEN_KINDS = {
  classic: "classic PAT", fine_grained: "fine-grained PAT", oauth: "OAuth 令牌",
  app_user: "GitHub App 用户令牌", app_installation: "GitHub App 安装令牌", unknown: "未知类型",
};

function githubIdentityHtml(scope, status) {
  const own = scope === "global" ? globalCredentials : projectCredentials.credentials;
  const hasOwn = own.some(item => item.name === GITHUB_TOKEN_NAME);
  const setButton = label => `<button class="ghost compact" type="button"
    onclick="editCredential('${scope}', ${hasOwn ? `'${GITHUB_TOKEN_NAME}'` : "null"}, '${GITHUB_TOKEN_NAME}')">${label}</button>`;
  if (!status.configured)
    return `<strong>GitHub 身份</strong> <span class="muted">未设置。保存名为
      <code>${GITHUB_TOKEN_NAME}</code> 的凭据(个人访问令牌,classic 或 fine-grained 均可)后,Agent
      与自动化脚本里的 <code>gh</code>、<code>git</code> 直接使用这个身份,GitHub 轮询来源也用它查询事件。</span>
      ${setButton("设置 GitHub token")}`;
  const identity = status.identity || {};
  const inherited = scope === "project" && status.scope === "global";
  const facts = [];
  if (identity.ok) {
    facts.push(`<a href="${esc(identity.url)}" target="_blank" rel="noopener">@${esc(identity.login)}</a>${
      identity.name ? ` (${esc(identity.name)})` : ""}`);
    facts.push(esc(GITHUB_TOKEN_KINDS[identity.kind] || identity.kind));
    if (identity.scopes) facts.push(`权限 ${esc(identity.scopes.join(", ") || "无")}`);
    else if (identity.kind === "fine_grained") facts.push("权限以令牌自身的仓库与权限项为准");
    if (identity.expires_at) facts.push(`到期 ${esc(identity.expires_at)}`);
    if (identity.notifications === true) facts.push("通知接口可用");
    if (identity.notifications === false) facts.push("通知接口不可用(需要带 notifications 或 repo 权限的 classic PAT)");
  }
  const state = identity.ok ? `<span class="pill st-done">已连接</span>`
    : `<span class="pill st-failed" title="${esc(identity.error || "")}">核对失败</span>
       <span class="automation-error">${esc(identity.error || "")}</span>`;
  return `<strong>GitHub 身份</strong> ${state} <span>${facts.join(" · ")}</span>
    ${inherited ? '<span class="pill">沿用全局 token</span>' : ""}
    <button class="ghost compact" type="button" onclick="renderGithubIdentity('${scope}', { refresh: true })">重新核对</button>
    ${setButton(inherited ? "在本项目单独设置" : "替换 token")}`;
}

async function renderGithubIdentity(scope, { refresh = false } = {}) {
  const box = document.getElementById(scope === "global" ? "global-github-identity" : "proj-github-identity");
  if (!box || (scope === "project" && !currentProject)) return;
  const project = currentProject;
  const base = scope === "global" ? "/api/github" : `/api/projects/${encodeURIComponent(project)}/github`;
  if (refresh) box.classList.add("loading");
  try {
    const status = await api("GET", base + (refresh ? "?refresh=true" : ""));
    if (scope === "project" && project !== currentProject) return;   // 期间已切换项目
    box.innerHTML = githubIdentityHtml(scope, status);
  } catch (e) {
    box.innerHTML = `<strong>GitHub 身份</strong> <span class="automation-error">读取失败</span>`;
  } finally {
    box.classList.remove("loading");
  }
}

function credentialByName(scope, name) {
  const items = scope === "global" ? globalCredentials : projectCredentials.credentials;
  return items.find(item => item.name === name);
}

// 多行值(PEM 私钥等)改用文本框;单行默认密码框,输入时不露出明文
function credentialValueInputHtml(multiline, placeholder) {
  return multiline
    ? `<textarea id="cred-value" rows="6" autocomplete="off" spellcheck="false"
         placeholder="${esc(placeholder)}"></textarea>`
    : `<input type="password" id="cred-value" autocomplete="new-password" spellcheck="false"
         placeholder="${esc(placeholder)}">`;
}

function toggleCredentialMultiline() {
  const current = document.getElementById("cred-value");
  const multiline = current.tagName !== "TEXTAREA";
  const value = current.value;
  current.outerHTML = credentialValueInputHtml(multiline, current.placeholder);
  document.getElementById("cred-value").value = value;
  document.getElementById("cred-multiline").textContent = multiline ? "改为单行密码框" : "改为多行输入";
}

/* name 为空 = 新建;presetName 用于"在本项目覆盖"预填同名;presetValue 用于聊天里
   检测到密钥后直接存。onSaved 收到保存后的条目。 */
function editCredential(scope, name, presetName = "", { presetValue = "", onSaved = null } = {}) {
  const existing = name ? credentialByName(scope, name) : null;
  if (name && !existing) { uiAlert("凭据不存在,请刷新后重试"); return; }
  const where = scope === "global" ? "全局凭据(所有项目可用)" : `项目「${currentProject}」的凭据`;
  const multiline = presetValue.includes("\n");
  const placeholder = existing?.has_value ? "留空 = 保留原值" : "粘贴凭据值";
  const requested = existing && !existing.has_value && existing.requested_by
    ? `<p class="muted">${esc(existing.requested_by)} 请求了这份凭据。</p>` : "";
  openFormDialog(existing ? `${existing.has_value ? "替换" : "填写"}凭据 ${existing.name}` : `新建${where}`, `
    ${requested}
    <label>名称(大写字母、数字、下划线;Agent 读取的文件名)</label>
    <input type="text" id="cred-name" value="${esc(existing?.name || presetName)}"
      ${existing ? "disabled" : ""} placeholder="例如 GITHUB_TOKEN">
    <label>用途(写给 Agent 看:做什么用、权限范围)</label>
    <textarea id="cred-desc" rows="2" placeholder="例如:webshop 仓库的细粒度 PAT,只有 Issues/PR 读写">${
      esc(existing?.description || "")}</textarea>
    ${(existing?.name || presetName) === GITHUB_TOKEN_NAME ? `<p class="muted">这份凭据是 GitHub 身份:填个人访问令牌。
      classic(<code>ghp_…</code>)按 scope 授权,读写仓库勾 <code>repo</code>,要用通知接口勾 <code>notifications</code>
      或 <code>repo</code>;fine-grained(<code>github_pat_…</code>)按仓库与权限项授权,不能用通知接口。</p>` : ""}
    <label class="label-with-action">值 <button class="ghost compact" type="button"
      id="cred-multiline" onclick="toggleCredentialMultiline()">${
      multiline ? "改为单行密码框" : "改为多行输入"}</button></label>
    ${credentialValueInputHtml(multiline, placeholder)}
    <p class="muted">值保存后不再显示,只留末 4 位供辨认。Agent 经工作区
      <code>credentials/${esc(existing?.name || presetName || "<名称>")}</code> 文件读取,
      平台会把消息、运行日志里出现的值替换成 <code>[credential:名称]</code>。${
      scope === "project" ? "与全局凭据同名时,本项目使用这一份。" : "项目里有同名凭据时,该项目使用项目那份。"}</p>`,
    `<button class="action" data-scope="${scope}" data-name="${esc(existing?.name || "")}"
       onclick="saveCredential(this.dataset.scope, this.dataset.name || null)">保存</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
  if (presetValue) document.getElementById("cred-value").value = presetValue;
  credentialSaveCallback = onSaved;
}

let credentialSaveCallback = null;

async function saveCredential(scope, existingName) {
  const name = existingName || document.getElementById("cred-name").value.trim();
  if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(name)) {
    uiAlert("名称只能使用大写字母、数字和下划线,以字母开头,例如 GITHUB_TOKEN");
    return;
  }
  const value = document.getElementById("cred-value").value;
  const existing = existingName ? credentialByName(scope, existingName) : null;
  if (!value.trim() && !existing?.has_value) { uiAlert("请填写凭据值"); return; }
  const saved = await api("POST", credentialApiBase(scope), {
    name, description: document.getElementById("cred-desc").value.trim(),
    ...(value.trim() ? { value } : {}),
  });
  fdlg.close();
  const callback = credentialSaveCallback;
  credentialSaveCallback = null;
  await refreshCredentialViews();
  toast(`凭据 ${saved.name} 已保存,Agent 下一条命令即可读取`, "success");
  if (callback) callback(saved);
}

async function deleteCredential(scope, name) {
  const inheritedNote = scope === "project"
    && (projectCredentials.inherited || []).some(item => item.name === name)
    ? "删除后本项目改用同名的全局凭据。" : "删除后 Agent 读不到这份凭据。";
  if (!await uiConfirm(`删除凭据 ${name}?${inheritedNote}`, "删除凭据")) return;
  await api("DELETE", `${credentialApiBase(scope)}/${encodeURIComponent(name)}`);
  await refreshCredentialViews();
  toast(`凭据 ${name} 已删除`, "success");
}

async function refreshCredentialViews() {
  await renderGlobalCredentials();
  await renderProjectCredentials();
}

/* ---- 聊天发送前的密钥检测:只认格式特征明显的常见密钥,避免误拦普通文本 ---- */
const SECRET_PATTERNS = [
  ["GitHub token", /\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b/],
  ["GitLab token", /\bglpat-[A-Za-z0-9_-]{20,}\b/],
  ["API key", /\bsk-[A-Za-z0-9_-]{20,}/],
  ["AWS access key", /\bAKIA[0-9A-Z]{16}\b/],
  ["Google API key", /\bAIza[0-9A-Za-z_-]{35}\b/],
  ["Slack token", /\bxox[abprs]-[A-Za-z0-9-]{10,}/],
  ["私钥", /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/],
];

function detectSecret(text) {
  for (const [label, pattern] of SECRET_PATTERNS) {
    const match = String(text || "").match(pattern);
    if (match) return { label, value: match[0] };
  }
  return null;
}

// 把输入框里的密钥原文换成占位符;原文被拆进多个节点时返回 false,让人手动删
function replaceSecretInComposer(box, secret, replacement) {
  const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
  let replaced = false;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.nodeValue.includes(secret)) {
      node.nodeValue = node.nodeValue.split(secret).join(replacement);
      replaced = true;
    }
  }
  return replaced;
}

/* 消息含疑似密钥时询问:返回 true = 照常发送;false = 不发(改为存凭据或返回修改)。 */
function confirmSecretInMessage(content, box) {
  const hit = detectSecret(content);
  if (!hit) return Promise.resolve(true);
  return new Promise(resolve => {
    _udlgResolve = resolve;
    _udlgOpen("消息里像是有密钥", `<div style="white-space:pre-wrap">${esc(
      `检测到疑似 ${hit.label}。发进聊天的内容会进入消息记录和 Agent 上下文。\n` +
      "建议存为项目凭据:Agent 从凭据文件读取,消息里只留引用。")}</div>`,
      `<button class="action" onclick="_udlgClose('store')">存为凭据</button>
       <button class="ghost" onclick="_udlgClose(true)">仍然发送</button>
       <button class="ghost" onclick="_udlgClose(false)">返回修改</button>`);
  }).then(choice => {
    if (choice !== "store") return choice === true;
    editCredential("project", null, "", {
      presetValue: hit.value,
      onSaved: saved => {
        const placeholder = `[credential:${saved.name}]`;
        if (!replaceSecretInComposer(box, hit.value, placeholder))
          uiAlert(`已保存为凭据 ${saved.name},请手动删除输入框里的原文后再发送。`);
        else toast(`已替换为 ${placeholder},确认后再发送`, "success");
        box.focus();
      },
    });
    return false;
  });
}
