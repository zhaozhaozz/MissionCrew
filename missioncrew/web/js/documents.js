/* ---- 版本化文档库 ---- */
let docFiles = [];   // 文档库文件清单缓存，供应用侧栏目录树使用

async function loadDocFiles() {
  // 轻量拉取文件清单以渲染应用侧栏，不渲染文档主区
  if (!currentProject) return false;
  const projectId = currentProject;
  const before = JSON.stringify(docFilesMeta || []);
  try {
    const d = await api("GET", `/api/projects/${encodeURIComponent(projectId)}/documents`);
    if (projectId !== currentProject) return false;
    docFilesMeta = d.files;
    docFiles = d.files.map(f => f.path);
    return before !== JSON.stringify(docFilesMeta);
  } catch (e) { /* ignore */ }
  return false;
}

// ---- 文档库:应用侧栏目录树 + 主区阅读/编辑 ----
let docFilesMeta = [];            // [{path,size,modified_at}]
let docSelected = null;           // 当前选中文件路径
let docMode = "view";             // view | edit | new
let docViewingRevision = null;    // 查看历史版本时的 revision
let docHistoryOpen = false;
const docCollapsed = new Set();   // 收起的目录前缀
let docPaneRenderSignature = null;
let docPaneRenderToken = 0;
let documentRefreshToken = 0;

function currentDocPaneSignature() {
  const meta = docFilesMeta.find(file => file.path === docSelected) || null;
  return JSON.stringify([
    currentProject, docSelected, docMode, docViewingRevision, docHistoryOpen, meta,
  ]);
}

function docEncode(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}

function normalizedDocumentLink(path) {
  const raw = path.split(/[?#]/, 1)[0];
  let decoded = raw;
  try { decoded = decodeURIComponent(raw); } catch (_) { /* 保留原始路径 */ }
  const base = currentTab === "docs" && docSelected && !decoded.startsWith("/")
    ? docSelected.split("/").slice(0, -1) : [];
  const parts = decoded.startsWith("/") ? [] : base;
  for (const part of decoded.replace(/^\/+/, "").split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") parts.pop();
    else parts.push(part);
  }
  return parts.join("/");
}

function openMarkdownDocumentLink(event, path) {
  event.preventDefault();
  const target = normalizedDocumentLink(path);
  if (!target || !docFiles.includes(target)) {
    toast(`找不到项目文档：${target || path}`, "error");
    return false;
  }
  docSelected = target;
  docMode = "view";
  docViewingRevision = null;
  docHistoryOpen = false;
  configChatSelection = null;
  if (currentTab === "docs") {
    renderSidebar();
    renderDocPane();
  } else {
    switchTab("docs");
  }
  return false;
}

async function renderDocuments(backgroundRefresh = false) {
  if (!currentProject || currentTab !== "docs") return;
  const projectId = currentProject;
  const refreshToken = ++documentRefreshToken;
  const fileListBefore = JSON.stringify(docFilesMeta);
  const d = await api("GET", `/api/projects/${encodeURIComponent(projectId)}/documents`);
  if (refreshToken !== documentRefreshToken || projectId !== currentProject
      || currentTab !== "docs") return;
  docFilesMeta = d.files;
  docFiles = d.files.map(f => f.path);
  if (docSelected && !docFiles.includes(docSelected) && docMode !== "new") {
    docSelected = null; docMode = "view";
  }
  if (!backgroundRefresh || fileListBefore !== JSON.stringify(docFilesMeta)) renderSidebar();
  const signature = currentDocPaneSignature();
  if (!backgroundRefresh || signature !== docPaneRenderSignature)
    await renderDocPane(backgroundRefresh);
  updateConfigChatContext();
}

function buildDocTree(files) {
  const root = { dirs: {}, files: [] };
  for (const f of files) {
    const parts = f.path.split("/");
    let node = root;
    for (const p of parts.slice(0, -1))
      node = node.dirs[p] ?? (node.dirs[p] = { dirs: {}, files: [] });
    node.files.push({ ...f, name: parts[parts.length - 1] });
  }
  return root;
}

function documentSidebarHtml() {
  const rows = [];
  const walk = (node, prefix, depth) => {
    for (const dir of Object.keys(node.dirs).sort()) {
      const key = prefix ? `${prefix}/${dir}` : dir;
      const closed = docCollapsed.has(key);
      rows.push(`<div class="side-item side-tree-dir" style="padding-left:${22 + depth * 14}px"
          data-dir="${esc(key)}" onclick="toggleDocDir(this.dataset.dir)">
          <span class="caret">${closed ? "▸" : "▾"}</span> 📁 ${esc(dir)}</div>`);
      if (!closed) walk(node.dirs[dir], key, depth + 1);
    }
    for (const f of node.files.sort((a, b) => a.name.localeCompare(b.name))) {
      rows.push(`<div class="side-item ${f.path === docSelected && currentTab === "docs" ? "selected" : ""}"
          style="padding-left:${22 + depth * 14}px" data-path="${esc(f.path)}"
          onclick="openDocFromSidebar(this.dataset.path)" title="${esc(f.path)}">
          📄 ${esc(f.name)}</div>`);
    }
  };
  walk(buildDocTree(docFilesMeta), "", 0);
  return rows.join("")
    || `<div class="side-item" onclick="switchTab('docs');newDocument()">＋ 新建第一篇文档…</div>`;
}

function toggleDocDir(key) {
  docCollapsed.has(key) ? docCollapsed.delete(key) : docCollapsed.add(key);
  renderSidebar();
}

function selectDocument(path) {
  docSelected = path; docMode = "view";
  configChatSelection = null;
  docViewingRevision = null; docHistoryOpen = false;
  renderSidebar(); renderDocPane();
}

function newDocument() {
  if (!currentProject) return;
  docSelected = null; docMode = "new";
  configChatSelection = null;
  docViewingRevision = null; docHistoryOpen = false;
  renderSidebar();
  renderDocPane();
}

function isMarkdownDoc(path) {
  return /\.(md|markdown|txt)$/i.test(path) || !path.includes(".");
}

async function renderDocPane(preserveScroll = false) {
  const pane = document.getElementById("doc-pane");
  if (!pane) return;
  const renderToken = ++docPaneRenderToken;
  const projectId = currentProject;
  const selectedPath = docSelected;
  const selectedMode = docMode;
  const selectedRevision = docViewingRevision;
  const stillCurrent = () => renderToken === docPaneRenderToken
    && projectId === currentProject && selectedPath === docSelected
    && selectedMode === docMode && selectedRevision === docViewingRevision;
  let scrollState = [];
  const captureScroll = () => {
    scrollState = preserveScroll ? captureScrollPositions(["#doc-pane"]) : [];
  };
  const finish = () => {
    docPaneRenderSignature = currentDocPaneSignature();
    restoreScrollPositions(scrollState);
    updateConfigChatContext();
  };
  if (docMode === "new") {
    captureScroll();
    pane.innerHTML = `
      <div class="doc-head"><b>新建文档</b>
        <button class="action" onclick="saveDocument()">保存新版本</button>
        <button class="ghost" onclick="cancelDocEdit()">取消</button></div>
      <label>文档库内相对路径</label>
      <input type="text" id="doc-new-path" value="" placeholder="specs/design.md" autofocus
        oninput="updateConfigChatContext()">
      <label>正文</label>
      <textarea id="doc-content" class="doc-editor" aria-label="文档正文"></textarea>`;
    document.getElementById("doc-new-path")?.focus();
    finish();
    return;
  }
  if (!docSelected) {
    captureScroll();
    pane.innerHTML = `<div class="empty">从左侧目录树选择一个文档查看。</div>`;
    finish();
    return;
  }
  const meta = docFilesMeta.find(f => f.path === docSelected);
  const metaLine = meta ? `${meta.size} B · ${new Date(meta.modified_at * 1000).toLocaleString()}` : "";
  // 编辑模式:文本编辑器
  if (docMode === "edit") {
    let content = "";
    const d = await api("GET",
      `/api/projects/${encodeURIComponent(projectId)}/documents/file/${docEncode(selectedPath)}`);
    if (!stillCurrent()) return;
    content = d.content;
    captureScroll();
    pane.innerHTML = `
      <div class="doc-head"><b>${esc(docSelected)}</b><span class="muted">编辑中</span>
        <button class="action" onclick="saveDocument()">保存新版本</button>
        <button class="ghost" onclick="cancelDocEdit()">取消</button></div>
      <textarea id="doc-content" class="doc-editor" aria-label="文档正文">${esc(content)}</textarea>`;
    finish();
    return;
  }
  // 查看:渲染 markdown / 纯文本;支持查看历史版本
  let d;
  try {
    const rev = docViewingRevision ? `?revision=${encodeURIComponent(docViewingRevision)}` : "";
    d = await api("GET",
      `/api/projects/${encodeURIComponent(projectId)}/documents/file/${docEncode(selectedPath)}${rev}`);
    if (!stillCurrent()) return;
  } catch (e) {
    if (!stillCurrent()) return;
    captureScroll();
    pane.innerHTML = `<div class="doc-head"><b>${esc(docSelected)}</b></div>
      <div class="empty">无法在线查看(可能是二进制文件),可直接在文档库目录中操作。</div>`;
    finish();
    return;
  }
  const revBanner = docViewingRevision
    ? `<div class="muted" style="margin-bottom:8px">正在查看历史版本 ${esc(docViewingRevision.slice(0, 10))}
        <button class="ghost" data-rev="${esc(docViewingRevision)}" onclick="restoreDocumentVersion(this.dataset.rev)">恢复此版本</button>
        <button class="ghost" onclick="docViewingRevision=null;renderDocPane()">返回最新</button></div>`
    : "";
  const body = isMarkdownDoc(docSelected)
    ? `<article class="doc-body markdown-body">${markdownPreviewHtml(d.content)}</article>`
    : `<pre style="white-space:pre-wrap;font-size:12.5px">${esc(d.content)}</pre>`;
  captureScroll();
  pane.innerHTML = `
    <div class="doc-head"><b>${esc(docSelected)}</b><span class="muted">${metaLine}</span>
      <button class="action" onclick="docMode='edit';renderDocPane()">编辑</button>
      <button class="ghost" onclick="toggleDocHistory()">${docHistoryOpen ? "收起历史" : "版本历史"}</button>
      <button class="danger" onclick="deleteDocument()">删除</button></div>
    ${revBanner}${body}<div id="doc-history"></div>`;
  finish();
  if (docHistoryOpen) await showDocumentHistory(renderToken);
  if (!stillCurrent()) return;
}

function cancelDocEdit() {
  if (docMode === "new") { docSelected = null; }
  docMode = "view";
  configChatSelection = null;
  renderSidebar();
  renderDocPane();
}

async function saveDocument() {
  const path = docMode === "new"
    ? document.getElementById("doc-new-path").value.trim()
    : docSelected;
  if (!path) { uiAlert("请输入文档库内相对路径"); return; }
  docSelected = path;
  await api("PUT",
    `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(path)}`, {
    content: document.getElementById("doc-content").value,
    message: `Update ${path} from project document editor`,
  });
  docMode = "view";
  await renderDocuments();
  toast("文档已保存为新版本", "success");
}

async function deleteDocument() {
  if (!docSelected || !await uiConfirm(`删除文档「${docSelected}」?历史版本仍可查阅。`)) return;
  await api("DELETE",
    `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(docSelected)}`);
  docSelected = null; docMode = "view";
  configChatSelection = null;
  await renderDocuments();
  toast("文档已删除", "success");
}

async function toggleDocHistory() {
  docHistoryOpen = !docHistoryOpen;
  await renderDocPane();
}

async function showDocumentHistory(renderToken = docPaneRenderToken) {
  if (!docSelected) return;
  const rows = await api("GET",
    `/api/projects/${encodeURIComponent(currentProject)}/documents/history?path=${encodeURIComponent(docSelected)}`);
  if (renderToken !== docPaneRenderToken) return;
  const el = document.getElementById("doc-history");
  if (!el) return;
  el.innerHTML = `<h3>版本历史</h3><table>
    <tr><th>版本</th><th>时间</th><th>作者</th><th>说明</th><th></th></tr>` +
    rows.map(v => `<tr><td>${esc(v.revision.slice(0, 10))}</td><td>${new Date(v.created_at * 1000).toLocaleString()}</td>
      <td>${esc(v.actor)}</td><td>${esc(v.message)}</td><td>
      <button class="ghost" data-rev="${esc(v.revision)}" onclick="docViewingRevision=this.dataset.rev;docHistoryOpen=true;renderDocPane()">查看</button>
      <button class="ghost" data-rev="${esc(v.revision)}" onclick="restoreDocumentVersion(this.dataset.rev)">恢复此版本</button></td></tr>`).join("") + `</table>`;
}

async function restoreDocumentVersion(revision) {
  if (!docSelected || !await uiConfirm(`把 ${docSelected} 恢复到版本 ${revision.slice(0, 10)}?(以新版本写入,历史保留)`)) return;
  const r = await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/documents/restore`,
                      { path: docSelected, revision });
  toast(`已恢复,新版本 ${r.revision.slice(0, 10)}`, "success");
  docViewingRevision = null;
  await renderDocuments();
}
