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
let docHistoryRows = [];
let docHistoryCanCompare = true;
let docCompareRevisions = [];     // 按用户选择顺序表示 A → B
let docCompareResult = null;
let docCompareRequestToken = 0;
const docExpanded = new Set();    // 用户在当前页面显式展开的目录前缀
let docPaneRenderSignature = null;
let docPaneRenderToken = 0;
let documentRefreshToken = 0;
let docPaneContent = null;        // 当前阅读页正文，供配置对话写入工作区快照
let docPaneContentIdentity = null;
let docPaneContentType = null;    // text | binary；二进制页仍可向主控传递文件身份

function resetDocumentVersionCompare() {
  docCompareRequestToken += 1;
  docHistoryRows = [];
  docCompareRevisions = [];
  docCompareResult = null;
}

function currentDocContentIdentity() {
  return JSON.stringify(docMode === "new"
    ? [currentProject, "new"]
    : [currentProject, docSelected, docMode, docViewingRevision]);
}

function currentDocPaneSignature() {
  const meta = docFilesMeta.find(file => file.path === docSelected) || null;
  return JSON.stringify([
    currentProject, docSelected, docMode, docViewingRevision, docHistoryOpen, meta,
  ]);
}

function docEncode(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}

function expandDocAncestors(path) {
  let prefix = "";
  for (const part of String(path || "").split("/").slice(0, -1)) {
    if (!part) continue;
    prefix = prefix ? `${prefix}/${part}` : part;
    docExpanded.add(prefix);
  }
}

function resolvedDocumentLink(path) {
  const resource = missionCrewDocumentReference(path);
  if (resource) return resource;
  const raw = String(path || "").split(/[?#]/, 1)[0];
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
  const target = parts.join("/");
  return { projectId: currentProject, path: target,
           url: missionCrewDocumentUrl(currentProject, target) };
}

function openMarkdownDocumentLink(event, path) {
  event.preventDefault();
  void revealMissionCrewDocument(path);
  return false;
}

async function revealMissionCrewDocument(path) {
  const target = resolvedDocumentLink(path);
  if (!target?.projectId || !overview.projects.some(p => p.id === target.projectId)) {
    toast("找不到 MissionCrew 项目文档", "error");
    return;
  }
  if (target.projectId !== currentProject) setProject(target.projectId, false);
  await loadDocFiles();
  if (target.path && !docFiles.includes(target.path)) {
    toast(`找不到项目文档：${target.projectId}/${target.path}`, "error");
    return;
  }
  docSelected = target.path || null;
  expandDocAncestors(docSelected);
  docMode = "view";
  docViewingRevision = null;
  docHistoryOpen = false;
  resetDocumentVersionCompare();
  configChatSelection = null;
  if (currentTab === "docs") {
    renderSidebar();
    await renderDocPane();
    syncUrl();
  } else {
    switchTab("docs");
  }
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
    syncUrl(false);
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
      const closed = !docExpanded.has(key);
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
  docExpanded.has(key) ? docExpanded.delete(key) : docExpanded.add(key);
  renderSidebar();
}

function selectDocument(path) {
  docSelected = path; docMode = "view";
  configChatSelection = null;
  docViewingRevision = null; docHistoryOpen = false;
  resetDocumentVersionCompare();
  renderSidebar(); renderDocPane(); syncUrl();
}

function documentLibraryButtons() {
  return `<button class="ghost compact" type="button" onclick="beginDocumentUpload()">上传文件</button>
    <button class="ghost compact" type="button" onclick="newDocument()">＋ 新建</button>`;
}

function beginDocumentUpload() {
  if (!currentProject) return;
  if (currentTab !== "docs") switchTab("docs");
  const input = document.getElementById("doc-upload-input");
  if (!input) return;
  input.value = "";
  input.click();
}

function currentDocumentDirectory() {
  if (!docSelected) return "";
  return docSelected.split("/").slice(0, -1).join("/");
}

async function uploadDocuments(input) {
  const files = [...(input.files || [])];
  if (!files.length || !currentProject) return;
  const projectId = currentProject;
  try {
    const directory = currentDocumentDirectory();
    const uploads = files.map(file => {
      const filename = file.name.replace(/\\/g, "/").split("/").pop();
      return { file, path: [directory, filename].filter(Boolean).join("/") };
    });
    if (uploads.some(item => !item.path || item.path.split("/")
      .some(part => !part || part === "." || part === ".."))) {
      await uiAlert("上传文件名不合法。");
      return;
    }
    if (new Set(uploads.map(item => item.path)).size !== uploads.length) {
      await uiAlert("选择的文件中有重名文件，请分批上传。");
      return;
    }
    const existing = new Set(docFiles);
    const conflicts = uploads.filter(item => existing.has(item.path)).map(item => item.path);
    if (conflicts.length) {
      const shown = conflicts.slice(0, 8).join("\n");
      const more = conflicts.length > 8 ? `\n…另有 ${conflicts.length - 8} 个` : "";
      const confirmed = await uiConfirm(
        `以下文档已存在，继续会创建覆盖它们的新版本：\n${shown}${more}`,
        "覆盖已有文档");
      if (!confirmed) return;
    }

    const conflictSet = new Set(conflicts);
    const uploaded = [], errors = [];
    for (const item of uploads) {
      const query = new URLSearchParams({
        path: item.path,
        overwrite: String(conflictSet.has(item.path)),
      });
      try {
        const response = await fetch(
          `/api/projects/${encodeURIComponent(projectId)}/documents/upload?${query}`, {
            method: "POST",
            headers: { "Content-Type": item.file.type || "application/octet-stream" },
            body: item.file,
          });
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          errors.push(`${item.path}: ${detail.detail || `HTTP ${response.status}`}`);
          continue;
        }
        uploaded.push((await response.json()).path);
      } catch (error) {
        errors.push(`${item.path}: ${error.message || error}`);
      }
    }
    if (projectId !== currentProject) return;
    await renderDocuments();
    if (uploaded.length) {
      docSelected = uploaded[0];
      expandDocAncestors(docSelected);
      docMode = "view";
      docViewingRevision = null;
      docHistoryOpen = false;
      resetDocumentVersionCompare();
      renderSidebar();
      await renderDocPane();
      syncUrl();
    }
    if (errors.length) {
      toast(`已上传 ${uploaded.length} 个文件，${errors.length} 个失败：\n${errors.slice(0, 3).join("\n")}`,
        "error", 7000);
    } else {
      toast(`已上传 ${uploaded.length} 个文件并记录文档版本`, "success", 5000);
    }
  } catch (error) {
    toast(`上传失败：${error.message || error}`, "error", 7000);
  } finally {
    input.value = "";
  }
}

function newDocument() {
  if (!currentProject) return;
  docSelected = null; docMode = "new";
  configChatSelection = null;
  docViewingRevision = null; docHistoryOpen = false;
  resetDocumentVersionCompare();
  renderSidebar();
  renderDocPane();
  syncUrl();
}

function isMarkdownDoc(path) {
  return /\.(md|markdown|txt)$/i.test(path) || !path.includes(".");
}

function documentDownloadUrl(path = docSelected, revision = docViewingRevision) {
  let url = `/api/projects/${encodeURIComponent(currentProject)}/documents/download/${docEncode(path)}`;
  if (revision) url += `?revision=${encodeURIComponent(revision)}`;
  return url;
}

function downloadDocument() {
  if (!docSelected) return;
  const link = document.createElement("a");
  link.href = documentDownloadUrl();
  link.download = docSelected.split("/").pop();
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function fetchDocumentPayload(url) {
  const response = await fetch(url);
  if (response.status === 415) return { binary: true, data: null };
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `读取失败 (${response.status})`);
  }
  return { binary: false, data: await response.json() };
}

async function renderDocPane(preserveScroll = false) {
  const pane = document.getElementById("doc-pane");
  if (!pane) return;
  const renderToken = ++docPaneRenderToken;
  const projectId = currentProject;
  const selectedPath = docSelected;
  const selectedMode = docMode;
  const selectedRevision = docViewingRevision;
  docPaneContent = null;
  docPaneContentIdentity = null;
  docPaneContentType = null;
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
    docPaneContent = "";
    docPaneContentIdentity = currentDocContentIdentity();
    docPaneContentType = "text";
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
    docPaneContent = null;
    captureScroll();
    pane.innerHTML = `<div class="doc-head"><span class="guideline-toolbar-spacer"></span>
        ${documentLibraryButtons()}</div>
      <div class="empty">从左侧目录树选择一个文档查看，或上传已有文件。</div>`;
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
    docPaneContent = content;
    docPaneContentIdentity = currentDocContentIdentity();
    docPaneContentType = "text";
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
  let d, binary = false;
  try {
    const rev = docViewingRevision ? `?revision=${encodeURIComponent(docViewingRevision)}` : "";
    const payload = await fetchDocumentPayload(
      `/api/projects/${encodeURIComponent(projectId)}/documents/file/${docEncode(selectedPath)}${rev}`);
    d = payload.data;
    binary = payload.binary;
    if (!stillCurrent()) return;
  } catch (e) {
    if (!stillCurrent()) return;
    captureScroll();
    docPaneContent = null;
    pane.innerHTML = `<div class="doc-head"><b>${esc(docSelected)}</b></div>
      <div class="empty">${esc(e.message || "无法在线查看文档")}</div>`;
    finish();
    return;
  }
  const revBanner = docViewingRevision
    ? `<div class="muted" style="margin-bottom:8px">正在查看历史版本 ${esc(docViewingRevision.slice(0, 10))}
        <button class="ghost" data-rev="${esc(docViewingRevision)}" onclick="restoreDocumentVersion(this.dataset.rev)">恢复此版本</button>
        <button class="ghost" onclick="docViewingRevision=null;renderDocPane()">返回最新</button></div>`
    : "";
  if (binary) {
    docHistoryCanCompare = false;
    docPaneContentIdentity = currentDocContentIdentity();
    docPaneContentType = "binary";
    configChatSelection = null;
    captureScroll();
    pane.innerHTML = `
      <div class="doc-head"><b>${esc(docSelected)}</b><span class="muted">${metaLine}</span>
        ${documentLibraryButtons()}
        <button class="action" onclick="downloadDocument()">下载</button>
        <button class="ghost" onclick="toggleDocHistory()">${docHistoryOpen ? "收起历史" : "版本历史"}</button>
        <button class="danger" onclick="deleteDocument()">删除</button></div>
      <div id="doc-history"></div><div id="doc-compare"></div>
      ${revBanner}<div class="empty">该文件不是 UTF-8 纯文本，不能在线编辑或比较版本；可以下载原文件。</div>`;
    finish();
    if (docHistoryOpen) await showDocumentHistory(renderToken);
    if (!stillCurrent()) return;
    return;
  }
  docHistoryCanCompare = true;
  docPaneContent = d.content;
  docPaneContentIdentity = currentDocContentIdentity();
  docPaneContentType = "text";
  const body = isMarkdownDoc(docSelected)
    ? `<article class="doc-body markdown-body">${markdownPreviewHtml(d.content)}</article>`
    : `<pre style="white-space:pre-wrap;font-size:12.5px">${esc(d.content)}</pre>`;
  captureScroll();
  pane.innerHTML = `
    <div class="doc-head"><b>${esc(docSelected)}</b><span class="muted">${metaLine}</span>
      ${documentLibraryButtons()}
      <button class="action" onclick="docMode='edit';renderDocPane()">编辑</button>
      <button class="ghost" onclick="toggleDocHistory()">${docHistoryOpen ? "收起历史" : "版本历史"}</button>
      <button class="danger" onclick="deleteDocument()">删除</button></div>
    <div id="doc-history"></div><div id="doc-compare"></div>
    ${revBanner}${body}`;
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
  expandDocAncestors(path);
  docMode = "view";
  docHistoryOpen = false;
  resetDocumentVersionCompare();
  await renderDocuments();
  syncUrl();
  toast("文档已保存为新版本", "success");
}

async function deleteDocument() {
  if (!docSelected || !await uiConfirm(`将文档「${docSelected}」移入项目回收站？`)) return;
  await api("DELETE",
    `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(docSelected)}`);
  docSelected = null; docMode = "view";
  docViewingRevision = null; docHistoryOpen = false;
  resetDocumentVersionCompare();
  configChatSelection = null;
  await renderDocuments();
  syncUrl();
  toast("文档已移入回收站", "success");
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
  docHistoryRows = rows;
  renderDocumentHistoryTable();
}

function renderDocumentHistoryTable() {
  const el = document.getElementById("doc-history");
  if (!el || !docHistoryOpen) return;
  if (!docHistoryRows.length) {
    el.innerHTML = `<section class="doc-history-panel"><div class="empty">暂无版本历史。</div></section>`;
    return;
  }
  const full = docCompareRevisions.length >= 2;
  const compareHint = docHistoryCanCompare
    ? `选择两个纯文本版本（按 A → B 比较）`
    : `二进制或非 UTF-8 文件不支持版本比较`;
  el.innerHTML = `<section class="doc-history-panel">
    <div class="doc-history-head"><h3>版本历史</h3><span class="muted">${compareHint}</span>
      <span class="guideline-toolbar-spacer"></span>
      <button class="action compact" type="button" onclick="compareDocumentVersions()"
        ${docHistoryCanCompare && docCompareRevisions.length === 2 ? "" : "disabled"}>
        比较已选版本 (${docCompareRevisions.length}/2)</button></div>
    <div class="doc-history-table-wrap"><table>
      <tr><th>比较</th><th>版本</th><th>时间</th><th>作者</th><th>说明</th><th></th></tr>` +
    docHistoryRows.map((version, index) => {
      const slot = docCompareRevisions.indexOf(version.revision);
      const checked = slot >= 0;
      const disabled = !docHistoryCanCompare || (full && !checked);
      return `<tr class="${docViewingRevision === version.revision ? "selected" : ""}">
        <td class="doc-compare-choice"><input type="checkbox" data-rev="${esc(version.revision)}"
          aria-label="选择版本 ${esc(version.revision.slice(0, 10))} 进行比较"
          onchange="toggleDocumentCompareRevision(this.dataset.rev)"
          ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}>
          ${checked ? `<span class="doc-compare-slot">${slot === 0 ? "A" : "B"}</span>` : ""}</td>
        <td><code>${esc(version.revision.slice(0, 10))}</code>
          ${index === 0 ? `<span class="pill st-done">最新</span>` : ""}</td>
        <td>${new Date(version.created_at * 1000).toLocaleString()}</td>
        <td>${esc(version.actor)}</td><td>${esc(version.message)}</td><td>
          <button class="ghost compact" data-rev="${esc(version.revision)}"
            onclick="docViewingRevision=this.dataset.rev;docHistoryOpen=true;renderDocPane()">查看</button>
          <button class="ghost compact" data-rev="${esc(version.revision)}"
            onclick="restoreDocumentVersion(this.dataset.rev)">恢复</button></td></tr>`;
    }).join("") + `</table></div></section>`;
  renderDocumentComparison();
}

function toggleDocumentCompareRevision(revision) {
  const index = docCompareRevisions.indexOf(revision);
  if (index >= 0) docCompareRevisions.splice(index, 1);
  else if (docCompareRevisions.length < 2) docCompareRevisions.push(revision);
  docCompareResult = null;
  renderDocumentHistoryTable();
}

async function compareDocumentVersions() {
  if (!docSelected || !docHistoryCanCompare || docCompareRevisions.length !== 2) return;
  const token = ++docCompareRequestToken;
  const projectId = currentProject;
  const documentPath = docSelected;
  const revisions = [...docCompareRevisions];
  const comparePane = document.getElementById("doc-compare");
  if (comparePane) comparePane.innerHTML = `<div class="muted">正在比较版本…</div>`;
  let result = null;
  try {
    result = await api(
      "POST", `/api/projects/${encodeURIComponent(projectId)}/documents/compare`, {
        path: documentPath,
        from_revision: revisions[0],
        to_revision: revisions[1],
      });
  } catch (_) { /* api() 已显示具体错误 */ }
  if (token !== docCompareRequestToken || projectId !== currentProject
      || documentPath !== docSelected
      || revisions.some((revision, index) => revision !== docCompareRevisions[index])) return;
  docCompareResult = result;
  renderDocumentComparison();
}

function renderDocumentComparison() {
  const el = document.getElementById("doc-compare");
  if (!el) return;
  if (!docCompareResult) { el.innerHTML = ""; return; }
  const result = docCompareResult;
  const heading = `<div class="doc-compare-head"><strong>版本比较</strong>
    <code>A ${esc(result.from_revision.slice(0, 10))}</code><span>→</span>
    <code>B ${esc(result.to_revision.slice(0, 10))}</code>
    <span class="st-done">+${result.additions}</span>
    <span class="st-failed">−${result.deletions}</span>
    <span class="guideline-toolbar-spacer"></span>
    <button class="ghost compact" type="button" onclick="docCompareResult=null;renderDocumentComparison()">关闭比较</button></div>`;
  if (result.identical) {
    el.innerHTML = `<section class="doc-compare-panel">${heading}
      <div class="empty">两个版本的文本内容完全相同。</div></section>`;
    return;
  }
  const lines = result.diff.split("\n").map(line => {
    let kind = "context";
    if (line.startsWith("@@") || line.startsWith("---") || line.startsWith("+++")) kind = "meta";
    else if (line.startsWith("+")) kind = "added";
    else if (line.startsWith("-")) kind = "removed";
    return `<span class="${kind}">${esc(line) || "&#8203;"}</span>`;
  }).join("");
  el.innerHTML = `<section class="doc-compare-panel">${heading}
    <pre class="doc-version-diff">${lines}</pre></section>`;
}

async function restoreDocumentVersion(revision) {
  if (!docSelected || !await uiConfirm(`把 ${docSelected} 恢复到版本 ${revision.slice(0, 10)}?(以新版本写入,历史保留)`)) return;
  const r = await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/documents/restore`,
                      { path: docSelected, revision });
  toast(`已恢复,新版本 ${r.revision.slice(0, 10)}`, "success");
  docViewingRevision = null;
  resetDocumentVersionCompare();
  await renderDocuments();
}
