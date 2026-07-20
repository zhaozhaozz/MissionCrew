/* ---- 版本化文档库 ---- */
let docFiles = [];   // 文档库文件清单缓存，供应用侧栏目录树使用

async function loadDocFiles() {
  // 轻量拉取文件清单以渲染应用侧栏，不渲染文档主区
  if (!currentProject) return;
  try {
    const d = await api("GET", `/api/projects/${encodeURIComponent(currentProject)}/documents`);
    docFilesMeta = d.files;
    docFiles = d.files.map(f => f.path);
  } catch (e) { /* ignore */ }
}

// ---- 文档库:应用侧栏目录树 + 主区阅读/编辑 ----
let docFilesMeta = [];            // [{path,size,modified_at}]
let docSelected = null;           // 当前选中文件路径
let docMode = "view";             // view | edit | new
let docViewingRevision = null;    // 查看历史版本时的 revision
let docHistoryOpen = false;
const docCollapsed = new Set();   // 收起的目录前缀

function docEncode(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}

async function renderDocuments() {
  if (!currentProject || currentTab !== "docs") return;
  document.getElementById("docs-proj-label").textContent = `— 项目「${esc(currentProject)}」`;
  const d = await api("GET", `/api/projects/${encodeURIComponent(currentProject)}/documents`);
  docFilesMeta = d.files;
  docFiles = d.files.map(f => f.path);
  document.getElementById("docs-root").textContent =
    `Runtime 目录：${d.root}(平台工作区内可经 ./documents 软链访问)`;
  if (docSelected && !docFiles.includes(docSelected) && docMode !== "new") {
    docSelected = null; docMode = "view";
  }
  renderSidebar();
  await renderDocPane();
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

async function renderDocPane() {
  const pane = document.getElementById("doc-pane");
  if (!pane) return;
  if (docMode === "new") {
    pane.innerHTML = `
      <div class="doc-head"><b>新建文档</b>
        <button class="action" onclick="saveDocument()">保存新版本</button>
        <button class="ghost" onclick="cancelDocEdit()">取消</button></div>
      <label>文档库内相对路径</label>
      <input type="text" id="doc-new-path" value="" placeholder="specs/design.md" autofocus
        oninput="updateConfigChatContext()">
      <label>正文</label>
      <textarea id="doc-content" rows="20" style="width:100%;height:auto"></textarea>`;
    document.getElementById("doc-new-path")?.focus();
    updateConfigChatContext();
    return;
  }
  if (!docSelected) {
    pane.innerHTML = `<div class="empty">从左侧目录树选择一个文档查看。</div>`;
    updateConfigChatContext();
    return;
  }
  const meta = docFilesMeta.find(f => f.path === docSelected);
  const metaLine = meta ? `${meta.size} B · ${new Date(meta.modified_at * 1000).toLocaleString()}` : "";
  // 编辑模式:文本编辑器
  if (docMode === "edit") {
    let content = "";
    const d = await api("GET",
      `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(docSelected)}`);
    content = d.content;
    pane.innerHTML = `
      <div class="doc-head"><b>${esc(docSelected)}</b><span class="muted">编辑中</span>
        <button class="action" onclick="saveDocument()">保存新版本</button>
        <button class="ghost" onclick="cancelDocEdit()">取消</button></div>
      <textarea id="doc-content" rows="20" style="width:100%;height:auto">${esc(content)}</textarea>`;
    updateConfigChatContext();
    return;
  }
  // 查看:渲染 markdown / 纯文本;支持查看历史版本
  let d;
  try {
    const rev = docViewingRevision ? `?revision=${encodeURIComponent(docViewingRevision)}` : "";
    d = await api("GET",
      `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(docSelected)}${rev}`);
  } catch (e) {
    pane.innerHTML = `<div class="doc-head"><b>${esc(docSelected)}</b></div>
      <div class="empty">无法在线查看(可能是二进制文件),可直接在文档库目录中操作。</div>`;
    updateConfigChatContext();
    return;
  }
  const revBanner = docViewingRevision
    ? `<div class="muted" style="margin-bottom:8px">正在查看历史版本 ${esc(docViewingRevision.slice(0, 10))}
        <button class="ghost" data-rev="${esc(docViewingRevision)}" onclick="restoreDocumentVersion(this.dataset.rev)">恢复此版本</button>
        <button class="ghost" onclick="docViewingRevision=null;renderDocPane()">返回最新</button></div>`
    : "";
  const body = isMarkdownDoc(docSelected)
    ? `<div class="doc-body">${miniMarkdown(d.content)}</div>`
    : `<pre style="white-space:pre-wrap;font-size:12.5px">${esc(d.content)}</pre>`;
  pane.innerHTML = `
    <div class="doc-head"><b>${esc(docSelected)}</b><span class="muted">${metaLine}</span>
      <button class="action" onclick="docMode='edit';renderDocPane()">编辑</button>
      <button class="ghost" onclick="toggleDocHistory()">${docHistoryOpen ? "收起历史" : "版本历史"}</button>
      <button class="danger" onclick="deleteDocument()">删除</button></div>
    ${revBanner}${body}<div id="doc-history"></div>`;
  if (docHistoryOpen) await showDocumentHistory();
  updateConfigChatContext();
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

async function showDocumentHistory() {
  if (!docSelected) return;
  const rows = await api("GET",
    `/api/projects/${encodeURIComponent(currentProject)}/documents/history?path=${encodeURIComponent(docSelected)}`);
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
