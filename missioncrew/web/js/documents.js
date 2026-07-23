/* ---- 版本化文档库 ----
   查看/编辑/历史/对比统一由 viewer.js 的 docViewer 渲染，本文件是适配层：
   侧栏目录树、上传、新建文档、以及 docViewer 的数据回调。 */
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
let docMode = "view";             // view | new；查看/编辑模式由 docViewer 管理
const docExpanded = new Set();    // 用户在当前页面显式展开的目录前缀
let docPaneRenderSignature = null;
let documentRefreshToken = 0;
let docPaneContent = null;        // 当前阅读页正文，供配置对话写入工作区快照
let docPaneContentIdentity = null;
let docPaneContentType = null;    // text | binary；二进制与图片页仍可向主控传递文件身份

function currentDocContentIdentity() {
  return JSON.stringify(docMode === "new"
    ? [currentProject, "new"]
    : [currentProject, docSelected, docViewer.mode, docViewer.viewingRevision]);
}

function currentDocPaneSignature() {
  const meta = docFilesMeta.find(file => file.path === docSelected) || null;
  return JSON.stringify([
    currentProject, docSelected, docMode, docViewer.viewingRevision,
    docViewer.historyOpen, meta,
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

// Markdown 图片等相对路径：相对当前文档目录解析为文档库内路径。
function resolveDocRelativePath(src) {
  const raw = String(src || "").split(/[?#]/, 1)[0];
  let decoded = raw;
  try { decoded = decodeURIComponent(raw); } catch (_) { /* 保留原始路径 */ }
  const parts = decoded.startsWith("/") || !docSelected
    ? [] : docSelected.split("/").slice(0, -1);
  for (const part of decoded.replace(/^\/+/, "").split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") { if (!parts.length) return null; parts.pop(); }
    else parts.push(part);
  }
  return parts.length ? parts.join("/") : null;
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
  if (target.projectId !== currentProject) {
    const switched = await setProject(target.projectId, false);
    if (switched === false) return;
  }
  await loadDocFiles();
  if (target.path && !docFiles.includes(target.path)) {
    toast(`找不到项目文档：${target.projectId}/${target.path}`, "error");
    return;
  }
  docSelected = target.path || null;
  expandDocAncestors(docSelected);
  docMode = "view";
  docViewer.activate();
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
  if (docViewer.hasUnsaved() && docMode !== "new") {
    // 编辑中有未保存修改：保留编辑现场，仅刷新侧栏
    docPaneRenderSignature = signature;
    updateConfigChatContext();
    return;
  }
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
    || `<div class="side-item" onclick="openNewDocumentFromSidebar()">＋ 新建第一篇文档…</div>`;
}

function toggleDocDir(key) {
  docExpanded.has(key) ? docExpanded.delete(key) : docExpanded.add(key);
  renderSidebar();
}

// 新建文档草稿（路径或正文已填写）也算未保存修改。
function docNewDraftDirty() {
  if (docMode !== "new") return false;
  return Boolean(document.getElementById("doc-content")?.value
    || document.getElementById("doc-new-path")?.value);
}
registerViewerDirtyChecker(docNewDraftDirty);

async function confirmDocDiscard() {
  if (docViewer.hasUnsaved() || docNewDraftDirty())
    return uiConfirm("当前文档有尚未保存的修改，离开将丢失这些修改。是否继续？", "未保存的修改");
  return true;
}

async function selectDocument(path) {
  if (!await confirmDocDiscard()) return;
  docSelected = path; docMode = "view";
  configChatSelection = null;
  docViewer.activate();
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
      docViewer.activate();
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

async function newDocument() {
  if (!currentProject) return;
  if (!await confirmDocDiscard()) return;
  docSelected = null; docMode = "new";
  configChatSelection = null;
  docViewer.activate();
  renderSidebar();
  renderDocPane();
  syncUrl();
}

async function openNewDocumentFromSidebar() {
  if (!await confirmDocDiscard()) return;
  if (currentTab !== "docs") switchTab("docs");
  docSelected = null; docMode = "new";
  configChatSelection = null;
  docViewer.activate();
  renderSidebar();
  renderDocPane();
  syncUrl();
}

function isMarkdownDoc(path) {
  return /\.(md|markdown)$/i.test(path || "");
}

function documentDownloadUrl(path = docSelected, revision = docViewer.viewingRevision,
                             inline = false) {
  let url = `/api/projects/${encodeURIComponent(currentProject)}/documents/download/${docEncode(path)}`;
  const query = new URLSearchParams();
  if (revision) query.set("revision", revision);
  if (inline) query.set("inline", "1");
  const suffix = query.toString();
  return suffix ? `${url}?${suffix}` : url;
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

/* ---- 统一查看/编辑组件：文档库实例 ---- */
const docViewer = createTextViewer({
  containerId: "doc-pane",
  textareaId: "doc-content",
  editorClass: "doc-editor",
  previewClass: "",
  surfaceClass: "viewer-edit-surface",
  identity: () => JSON.stringify([currentProject, docSelected, docViewer.viewingRevision]),
  title: () => docSelected || "",
  metaLine: () => {
    const meta = docFilesMeta.find(f => f.path === docSelected);
    return meta
      ? `${meta.size} B · ${new Date(meta.modified_at * 1000).toLocaleString()}` : "";
  },
  editKind: () => isMarkdownDoc(docSelected) ? "markdown" : "text",
  loadView: async revision => {
    docPaneContentIdentity = currentDocContentIdentity();
    if (isViewerImagePath(docSelected)) {
      docPaneContent = null;
      docPaneContentType = "binary";
      configChatSelection = null;
      return { kind: "image" };
    }
    const rev = revision ? `?revision=${encodeURIComponent(revision)}` : "";
    const payload = await fetchDocumentPayload(
      `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(docSelected)}${rev}`);
    if (payload.binary) {
      docPaneContent = null;
      docPaneContentType = "binary";
      configChatSelection = null;
      return { kind: "binary" };
    }
    docPaneContent = payload.data.content;
    docPaneContentType = "text";
    return {
      kind: isMarkdownDoc(docSelected) ? "markdown" : "text",
      content: payload.data.content,
    };
  },
  loadEdit: async () => {
    const d = await api("GET",
      `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(docSelected)}`);
    docPaneContent = d.content;
    docPaneContentIdentity = currentDocContentIdentity();
    docPaneContentType = "text";
    return d.content;
  },
  save: async content => {
    await api("PUT",
      `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(docSelected)}`, {
        content,
        message: `Update ${docSelected} from project document editor`,
      });
    docPaneContent = content;
    expandDocAncestors(docSelected);
    await loadDocFiles();
    renderSidebar();
    toast("文档已保存为新版本", "success");
  },
  listHistory: () => api("GET",
    `/api/projects/${encodeURIComponent(currentProject)}/documents/history?path=${encodeURIComponent(docSelected)}`),
  compare: (from, to) => api("POST",
    `/api/projects/${encodeURIComponent(currentProject)}/documents/compare`, {
      path: docSelected,
      from_revision: from,
      to_revision: to,
    }),
  restore: async revision => {
    if (!await uiConfirm(
      `把 ${docSelected} 恢复到版本 ${revision.slice(0, 10)}?(以新版本写入,历史保留)`))
      throw new Error("cancelled");
    const r = await api("POST",
      `/api/projects/${encodeURIComponent(currentProject)}/documents/restore`,
      { path: docSelected, revision });
    await loadDocFiles();
    renderSidebar();
    toast(`已恢复,新版本 ${r.revision.slice(0, 10)}`, "success");
  },
  extrasHtml: mode => mode === "view"
    ? `${documentLibraryButtons()}
       <button class="danger" type="button" onclick="deleteDocument()">删除</button>`
    : "",
  downloadUrl: (revision, inline) => documentDownloadUrl(docSelected, revision, inline),
  imageResolver: src => {
    const target = resolveDocRelativePath(src);
    return target ? documentDownloadUrl(target, null, true) : null;
  },
  scrollSelectors: () => ["#doc-pane"],
  onChange: () => updateConfigChatContext(),
});

async function renderDocPane(preserveScroll = false) {
  const pane = document.getElementById("doc-pane");
  if (!pane) return;
  if (docMode === "new") {
    docPaneContent = "";
    docPaneContentIdentity = currentDocContentIdentity();
    docPaneContentType = "text";
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
    docPaneRenderSignature = currentDocPaneSignature();
    updateConfigChatContext();
    return;
  }
  if (!docSelected) {
    docPaneContent = null;
    docPaneContentIdentity = null;
    docPaneContentType = null;
    pane.innerHTML = `<div class="doc-head"><span class="guideline-toolbar-spacer"></span>
        ${documentLibraryButtons()}</div>
      <div class="empty">从左侧目录树选择一个文档查看，或上传已有文件。</div>`;
    docPaneRenderSignature = currentDocPaneSignature();
    updateConfigChatContext();
    return;
  }
  await docViewer.render(preserveScroll);
  docPaneRenderSignature = currentDocPaneSignature();
}

async function cancelDocEdit() {
  if (docMode === "new") {
    if (docNewDraftDirty() && !await uiConfirm(
      "新建文档尚未保存，取消将丢失已填写的内容。是否继续？", "未保存的修改")) return;
    docSelected = null;
  }
  docMode = "view";
  configChatSelection = null;
  renderSidebar();
  renderDocPane();
}

async function saveDocument() {
  if (docMode !== "new") return;   // 已有文档的保存由 docViewer 处理
  const path = document.getElementById("doc-new-path").value.trim();
  if (!path) { uiAlert("请输入文档库内相对路径"); return; }
  docSelected = path;
  await api("PUT",
    `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(path)}`, {
    content: document.getElementById("doc-content").value,
    message: `Update ${path} from project document editor`,
  });
  expandDocAncestors(path);
  docMode = "view";
  docViewer.activate();
  await renderDocuments();
  syncUrl();
  toast("文档已保存为新版本", "success");
}

async function deleteDocument() {
  if (!docSelected || !await uiConfirm(`将文档「${docSelected}」移入项目回收站？`)) return;
  await api("DELETE",
    `/api/projects/${encodeURIComponent(currentProject)}/documents/file/${docEncode(docSelected)}`);
  docSelected = null; docMode = "view";
  docViewer.activate();
  configChatSelection = null;
  await renderDocuments();
  syncUrl();
  toast("文档已移入回收站", "success");
}
