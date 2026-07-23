/* ---- 统一文本查看/编辑组件：版本化文档库与准则文档共用 ----
   查看模式：Markdown 原始/预览切换、纯文本行号、图片预览、历史版本与
   行内/左右两种版本对比（对比视图直接替换正文区）。
   编辑模式：独立「编辑」按钮进入，修改后显示「已修改」徽标，
   离开（切换条目/项目/关闭页面）前统一提醒保存。 */

const VIEWER_IMAGE_EXTS = new Set([
  "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "ico",
]);

function isViewerImagePath(path) {
  const ext = String(path || "").split(".").pop().toLowerCase();
  return VIEWER_IMAGE_EXTS.has(ext);
}

// 带行号的纯文本视图；行号不参与选中复制。
function lineNumberedTextHtml(text) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  return `<div class="text-lines">` + lines.map((line, index) =>
    `<div class="text-line"><span class="line-no">${index + 1}</span>` +
    `<span class="line-text">${esc(line) || "&#8203;"}</span></div>`).join("") + `</div>`;
}

/* 由后端 compare 端点的 ops（SequenceMatcher opcodes）渲染两种 diff。 */
const VIEWER_DIFF_FOLD = 6;   // 相同行超过该数量时折叠
const VIEWER_DIFF_CONTEXT = 3;

function viewerInlineDiffHtml(ops) {
  const spans = [];
  const push = (cls, text) =>
    spans.push(`<span class="${cls}">${esc(text) || "&#8203;"}</span>`);
  for (const op of ops || []) {
    if (op.tag === "equal") {
      const lines = op.a || [];
      if (lines.length > VIEWER_DIFF_FOLD) {
        lines.slice(0, VIEWER_DIFF_CONTEXT).forEach(line => push("context", ` ${line}`));
        push("meta", `⋯ ${lines.length - VIEWER_DIFF_CONTEXT * 2} 行相同 ⋯`);
        lines.slice(-VIEWER_DIFF_CONTEXT).forEach(line => push("context", ` ${line}`));
      } else {
        lines.forEach(line => push("context", ` ${line}`));
      }
    } else {
      (op.a || []).forEach(line => push("removed", `-${line}`));
      (op.b || []).forEach(line => push("added", `+${line}`));
    }
  }
  return `<pre class="doc-version-diff">${spans.join("")}</pre>`;
}

function viewerSplitDiffHtml(ops) {
  const rows = [];
  let aNo = 1, bNo = 1;
  const row = (aNum, aText, aCls, bNum, bText, bCls) => rows.push(
    `<tr><td class="diff-no">${aNum || ""}</td>` +
    `<td class="diff-text ${aCls}">${aText === null ? "" : (esc(aText) || "&#8203;")}</td>` +
    `<td class="diff-no">${bNum || ""}</td>` +
    `<td class="diff-text ${bCls}">${bText === null ? "" : (esc(bText) || "&#8203;")}</td></tr>`);
  for (const op of ops || []) {
    if (op.tag === "equal") {
      const lines = op.a || [];
      const folded = lines.length > VIEWER_DIFF_FOLD;
      const head = folded ? lines.slice(0, VIEWER_DIFF_CONTEXT) : lines;
      const tail = folded ? lines.slice(-VIEWER_DIFF_CONTEXT) : [];
      for (const line of head) row(aNo++, line, "", bNo++, line, "");
      if (folded) {
        const hidden = lines.length - VIEWER_DIFF_CONTEXT * 2;
        rows.push(`<tr class="diff-fold"><td colspan="4">⋯ ${hidden} 行相同 ⋯</td></tr>`);
        aNo += hidden; bNo += hidden;
      }
      for (const line of tail) row(aNo++, line, "", bNo++, line, "");
    } else if (op.tag === "replace") {
      const count = Math.max(op.a.length, op.b.length);
      for (let index = 0; index < count; index += 1) {
        const hasA = index < op.a.length, hasB = index < op.b.length;
        row(hasA ? aNo++ : "", hasA ? op.a[index] : null, hasA ? "removed" : "",
            hasB ? bNo++ : "", hasB ? op.b[index] : null, hasB ? "added" : "");
      }
    } else if (op.tag === "delete") {
      for (const line of op.a) row(aNo++, line, "removed", "", null, "");
    } else {   // insert
      for (const line of op.b) row("", null, "", bNo++, line, "added");
    }
  }
  return `<div class="doc-diff-split-wrap"><table class="doc-diff-split"><tbody>${rows.join("")}</tbody></table></div>`;
}

/* config 回调（宿主适配层提供）：
   containerId / textareaId / editorClass / previewClass / surfaceClass — 元素与样式
   identity()             — 当前条目唯一标识（含项目、路径、版本），用于竞态防护与缓存
   title() / metaLine()   — 头部标题（原始文本，组件负责转义）与元信息
   editKind()             — 编辑对象类型："markdown" | "text"
   loadView(revision)     — {kind: "markdown"|"text"|"image"|"binary", content?}
   loadEdit()             — 进入编辑模式的初始文本
   save(content)          — 保存（宿主负责 API/刷新/toast，抛错则留在编辑模式）
   listHistory()          — 版本历史行 [{revision, created_at, actor, message}]
   compare(a, b)          — {from_revision, to_revision, additions, deletions, identical, ops}
   restore(revision)      — 恢复版本（宿主负责确认/API/刷新/toast）
   extrasHtml(mode, kind) — 头部宿主按钮（上传/新建/删除、启用开关…）
   downloadUrl(revision, inline) — 可选；提供时 image/binary 显示下载按钮
   imageResolver(src)     — 可选；Markdown 相对图片路径 → 可加载 URL
   scrollSelectors()      — preserveScroll 时记录/恢复位置的滚动容器
   onChange() / onDirty() — 状态/脏标记变化钩子 */
function createTextViewer(config) {
  const container = document.getElementById(config.containerId);
  const V = {
    mode: "view",             // view | edit
    dirty: false,
    raw: false,               // 查看模式：Markdown 原始 / 预览
    editPreview: false,       // 编辑模式：编辑器 / 实时预览
    viewingRevision: null,
    historyOpen: false,
    historyRows: null,
    canCompare: true,
    compareRevisions: [],
    compareResult: null,
    compareStyle: localStorage.getItem("mc.diffStyle") === "split" ? "split" : "inline",
    renderToken: 0,
    compareToken: 0,
    viewData: null,           // {identity, kind, content}
    editIdentity: null,
  };

  /* ---- 事件委托：容器本身稳定，内部按钮统一走 data-vact ---- */
  container.addEventListener("click", event => {
    const button = event.target.closest("[data-vact]");
    if (!button || !container.contains(button)) return;
    const action = button.dataset.vact;
    const revision = button.dataset.rev;
    if (action === "enter-edit") void enterEdit();
    else if (action === "cancel-edit") void cancelEdit();
    else if (action === "save") void save();
    else if (action === "toggle-history") void toggleHistory();
    else if (action === "view-rev") void viewRevision(revision);
    else if (action === "close-rev") void closeRevision();
    else if (action === "restore-rev") void restoreRevision(revision);
    else if (action === "run-compare") void runCompare();
    else if (action === "exit-compare") void exitCompare();
    else if (action === "compare-style") setCompareStyle(button.dataset.style);
    else if (action === "set-raw") void setRaw(button.dataset.raw === "1");
    else if (action === "edit-surface") setEditSurface(button.dataset.surface);
    else if (action === "download") downloadCurrent();
  });
  container.addEventListener("input", event => {
    if (event.target.id === config.textareaId && V.mode === "edit") {
      V.markDirty();
      if (V.editPreview) updateEditPreview();
    }
  });
  container.addEventListener("change", event => {
    const box = event.target.closest("[data-compare-rev]");
    if (box && container.contains(box)) toggleCompareRevision(box.dataset.compareRev);
  });

  /* ---- 状态查询 ---- */
  V.hasUnsaved = () => V.mode === "edit" && V.dirty;
  V.confirmDiscard = async () => !V.hasUnsaved() || await uiConfirm(
    "当前内容有尚未保存的修改，离开将丢失这些修改。是否继续？", "未保存的修改");
  V.currentText = () => {
    if (V.mode === "edit")
      return document.getElementById(config.textareaId)?.value ?? "";
    return V.viewData?.content ?? null;
  };
  V.markDirty = () => {
    if (V.dirty) return;
    V.dirty = true;
    container.querySelector(".viewer-dirty-badge")?.removeAttribute("hidden");
    config.onDirty?.();
  };

  /* ---- 状态重置 ---- */
  V.activate = () => {   // 切换条目：回到最新版本的查看模式
    V.mode = "view"; V.dirty = false; V.raw = false; V.editPreview = false;
    V.viewingRevision = null; V.historyOpen = false; V.historyRows = null;
    V.compareRevisions = []; V.compareResult = null;
    V.viewData = null; V.editIdentity = null;
    V.renderToken += 1; V.compareToken += 1;
  };
  V.reset = () => V.activate();   // 切换项目：同上（调用前已确认放弃修改）

  /* ---- 渲染 ---- */
  function dirtyBadgeHtml() {
    return `<span class="viewer-dirty-badge" ${V.dirty ? "" : "hidden"}>已修改</span>`;
  }

  function rawToggleHtml() {
    return `<div class="guideline-view-toggle" aria-label="Markdown 显示方式">
      <button class="ghost compact ${V.raw ? "active" : ""}" type="button"
        data-vact="set-raw" data-raw="1">原始</button>
      <button class="ghost compact ${V.raw ? "" : "active"}" type="button"
        data-vact="set-raw" data-raw="0">预览</button>
    </div>`;
  }

  function viewHeadHtml(kind) {
    const historical = V.viewingRevision;
    const meta = config.metaLine();
    return `<div class="viewer-head">
      <b>${esc(config.title())}</b>
      ${meta ? `<span class="muted">${esc(meta)}</span>` : ""}
      ${historical ? `<span class="guideline-history-badge">历史版本
        <code>${esc(historical.slice(0, 10))}</code></span>` : ""}
      <span class="guideline-toolbar-spacer"></span>
      ${config.extrasHtml("view", kind)}
      ${historical ? `
        <button class="action compact" type="button" data-vact="restore-rev"
          data-rev="${esc(historical)}">恢复此版本</button>
        <button class="ghost compact" type="button" data-vact="close-rev">返回最新</button>` : `
        ${kind === "markdown" || kind === "text"
          ? `<button class="action compact" type="button" data-vact="enter-edit">编辑</button>` : ""}`}
      <button class="ghost compact" type="button" data-vact="toggle-history"
        ${(config.hasHistory?.() ?? true) ? "" : "hidden"}>
        ${V.historyOpen ? "收起历史" : "版本历史"}</button>
      ${kind === "markdown" ? rawToggleHtml() : ""}
      ${(kind === "image" || kind === "binary") && config.downloadUrl
        ? `<button class="ghost compact" type="button" data-vact="download">下载</button>` : ""}
    </div>`;
  }

  function editHeadHtml() {
    const kind = config.editKind();
    return `<div class="viewer-head">
      <b>${esc(config.title())}</b><span class="muted">编辑中</span>${dirtyBadgeHtml()}
      <span class="guideline-toolbar-spacer"></span>
      ${config.extrasHtml("edit", kind)}
      ${kind === "markdown" ? `
        <div class="guideline-view-toggle" aria-label="编辑或预览">
          <button class="ghost compact ${V.editPreview ? "" : "active"}" type="button"
            data-vact="edit-surface" data-surface="edit">编辑</button>
          <button class="ghost compact ${V.editPreview ? "active" : ""}" type="button"
            data-vact="edit-surface" data-surface="preview">预览</button>
        </div>` : ""}
      <button class="action" type="button" data-vact="save">保存</button>
      <button class="ghost" type="button" data-vact="cancel-edit">取消</button>
    </div>`;
  }

  function editBodyHtml(content) {
    const kind = config.editKind();
    return `<div class="${config.surfaceClass}">
      <textarea id="${esc(config.textareaId)}" class="${config.editorClass}" aria-label="正文"
        spellcheck="false" ${V.editPreview ? "hidden" : ""}>${esc(content)}</textarea>
      ${kind === "markdown" ? `
        <article class="${config.previewClass} markdown-body viewer-edit-preview"
          ${V.editPreview ? "" : "hidden"}></article>` : ""}
    </div>`;
  }

  function viewBodyHtml(data) {
    if (V.compareResult) return compareHtml();
    if (data.kind === "image")
      return `<div class="doc-image"><img src="${esc(config.downloadUrl(V.viewingRevision, true))}"
        alt="${esc(config.title())}" loading="lazy"></div>`;
    if (data.kind === "binary")
      return `<div class="empty">该文件不是 UTF-8 纯文本，不能在线编辑或比较版本；可以下载原文件。</div>`;
    if (data.kind === "markdown")
      return V.raw ? lineNumberedTextHtml(data.content)
        : `<article class="doc-body markdown-body">${markdownPreviewHtml(
            data.content, { imageResolver: config.imageResolver || null })}</article>`;
    return lineNumberedTextHtml(data.content);
  }

  function historyPanelHtml() {
    if (!V.historyOpen) return "";
    if (V.historyRows === null)
      return `<section class="doc-history-panel"><div class="empty">正在读取版本历史…</div></section>`;
    if (!V.historyRows.length)
      return `<section class="doc-history-panel"><div class="empty">暂无版本历史。</div></section>`;
    const full = V.compareRevisions.length >= 2;
    const hint = V.canCompare
      ? "选择两个纯文本版本（按 A → B 比较）"
      : "二进制或非 UTF-8 文件不支持版本比较";
    return `<section class="doc-history-panel">
      <div class="doc-history-head"><h3>版本历史</h3><span class="muted">${hint}</span>
        <span class="guideline-toolbar-spacer"></span>
        <button class="action compact" type="button" data-vact="run-compare"
          ${V.canCompare && V.compareRevisions.length === 2 ? "" : "disabled"}>
          比较已选版本 (${V.compareRevisions.length}/2)</button></div>
      <div class="doc-history-table-wrap"><table>
        <tr><th>比较</th><th>版本</th><th>时间</th><th>作者</th><th>说明</th><th></th></tr>` +
      V.historyRows.map((row, index) => {
        const slot = V.compareRevisions.indexOf(row.revision);
        const checked = slot >= 0;
        const disabled = !V.canCompare || (full && !checked);
        return `<tr class="${V.viewingRevision === row.revision ? "selected" : ""}">
          <td class="doc-compare-choice"><input type="checkbox" data-compare-rev="${esc(row.revision)}"
            aria-label="选择版本 ${esc(row.revision.slice(0, 10))} 进行比较"
            ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}>
            ${checked ? `<span class="doc-compare-slot">${slot === 0 ? "A" : "B"}</span>` : ""}</td>
          <td><code>${esc(row.revision.slice(0, 10))}</code>
            ${index === 0 ? `<span class="pill st-done">最新</span>` : ""}</td>
          <td>${new Date(row.created_at * 1000).toLocaleString()}</td>
          <td>${esc(row.actor)}</td><td>${esc(row.message)}</td><td>
            <button class="ghost compact" type="button" data-vact="view-rev"
              data-rev="${esc(row.revision)}">查看</button>
            <button class="ghost compact" type="button" data-vact="restore-rev"
              data-rev="${esc(row.revision)}">恢复</button></td></tr>`;
      }).join("") + `</table></div></section>`;
  }

  function compareHtml() {
    const result = V.compareResult;
    if (result.loading)
      return `<section class="doc-compare-panel"><div class="empty">正在比较版本…</div></section>`;
    const head = `<div class="doc-compare-head"><strong>版本比较</strong>
      <code>A ${esc(result.from_revision.slice(0, 10))}</code><span>→</span>
      <code>B ${esc(result.to_revision.slice(0, 10))}</code>
      <span class="st-done">+${result.additions}</span>
      <span class="st-failed">−${result.deletions}</span>
      <span class="guideline-toolbar-spacer"></span>
      <div class="guideline-view-toggle" aria-label="对比显示方式">
        <button class="ghost compact ${V.compareStyle === "inline" ? "active" : ""}"
          type="button" data-vact="compare-style" data-style="inline">行内</button>
        <button class="ghost compact ${V.compareStyle === "split" ? "active" : ""}"
          type="button" data-vact="compare-style" data-style="split">左右</button>
      </div>
      <button class="ghost compact" type="button" data-vact="exit-compare">退出比较</button></div>`;
    if (result.identical)
      return `<section class="doc-compare-panel">${head}
        <div class="empty">两个版本的文本内容完全相同。</div></section>`;
    const body = V.compareStyle === "split"
      ? viewerSplitDiffHtml(result.ops)
      : viewerInlineDiffHtml(result.ops);
    return `<section class="doc-compare-panel">${head}${body}</section>`;
  }

  async function render(preserveScroll = false) {
    const token = ++V.renderToken;
    const identity = config.identity();
    const stillCurrent = () => token === V.renderToken && identity === config.identity();
    const scrollState = preserveScroll
      ? captureScrollPositions(config.scrollSelectors()) : [];
    if (V.mode === "edit") {
      // 已有未保存内容时不得重新拉取覆盖编辑器
      if (V.editIdentity === identity && document.getElementById(config.textareaId)) return;
      const content = await config.loadEdit();
      if (!stillCurrent()) return;
      V.editIdentity = identity;
      container.innerHTML = editHeadHtml() + editBodyHtml(content);
      if (V.editPreview) updateEditPreview();
      restoreScrollPositions(scrollState);
      config.onChange?.();
      return;
    }
    V.editIdentity = null;
    let data = V.viewData?.identity === identity ? V.viewData : null;
    if (!data) {
      try {
        data = { ...(await config.loadView(V.viewingRevision)), identity };
      } catch (error) {
        if (!stillCurrent()) return;
        container.innerHTML = `<div class="viewer-head"><b>${esc(config.title())}</b></div>
          <div class="empty">${esc(error.message || "无法在线查看")}</div>`;
        config.onChange?.();
        return;
      }
      if (!stillCurrent()) return;
      V.viewData = data;
    }
    V.canCompare = data.kind === "markdown" || data.kind === "text";
    if (!V.canCompare) { V.compareRevisions = []; V.compareResult = null; }
    container.innerHTML = viewHeadHtml(data.kind) + historyPanelHtml() + viewBodyHtml(data);
    restoreScrollPositions(scrollState);
    config.onChange?.();
    if (V.historyOpen && V.historyRows === null) void loadHistory();
  }
  V.render = render;

  async function loadHistory() {
    const token = V.renderToken;
    const identity = config.identity();
    try {
      const rows = await config.listHistory();
      if (token !== V.renderToken || identity !== config.identity()) return;
      V.historyRows = rows;
    } catch (_) {
      if (token !== V.renderToken) return;
      V.historyRows = [];
    }
    await render();
  }

  function updateEditPreview() {
    const preview = container.querySelector(".viewer-edit-preview");
    if (!preview) return;
    const markdown = document.getElementById(config.textareaId)?.value ?? "";
    preview.innerHTML = markdown.trim()
      ? markdownPreviewHtml(markdown, { imageResolver: config.imageResolver || null })
      : `<div class="empty">正文为空。切换到“编辑”输入 Markdown。</div>`;
  }

  /* ---- 查看模式动作 ---- */
  async function enterEdit() {
    V.mode = "edit";
    V.dirty = false;
    V.editPreview = false;
    await render();
  }
  V.enterEdit = enterEdit;

  async function cancelEdit() {
    if (!await V.confirmDiscard()) return;
    V.mode = "view";
    V.dirty = false;
    await render();
  }
  V.cancelEdit = cancelEdit;

  async function save() {
    const editor = document.getElementById(config.textareaId);
    if (!editor) return;
    try {
      await config.save(editor.value);
    } catch (_) {   // api() 已显示错误；保留编辑现场
      return;
    }
    V.dirty = false;
    V.mode = "view";
    V.viewData = null;
    V.historyRows = null;
    V.compareResult = null;
    V.compareRevisions = [];
    await render();
  }
  V.save = save;

  async function toggleHistory() {
    if (!(config.hasHistory?.() ?? true)) return;
    V.historyOpen = !V.historyOpen;
    if (!V.historyOpen) V.compareResult = null;
    await render();
  }
  V.toggleHistory = toggleHistory;

  async function viewRevision(revision) {
    V.viewingRevision = revision;
    V.viewData = null;
    V.compareResult = null;
    await render();
  }
  V.viewRevision = viewRevision;

  async function closeRevision() {
    V.viewingRevision = null;
    V.viewData = null;
    await render();
  }
  V.closeRevision = closeRevision;

  async function restoreRevision(revision) {
    if (!revision) return;
    try {
      await config.restore(revision);
    } catch (_) {   // 宿主已提示；保持现状
      return;
    }
    V.viewingRevision = null;
    V.viewData = null;
    V.historyRows = null;
    V.compareResult = null;
    V.compareRevisions = [];
    await render();
  }
  V.restoreRevision = restoreRevision;

  function toggleCompareRevision(revision) {
    const index = V.compareRevisions.indexOf(revision);
    if (index >= 0) V.compareRevisions.splice(index, 1);
    else if (V.compareRevisions.length < 2) V.compareRevisions.push(revision);
    V.compareResult = null;
    void render();
  }
  V.toggleCompareRevision = toggleCompareRevision;

  async function runCompare() {
    if (!V.canCompare || V.compareRevisions.length !== 2) return;
    const token = ++V.compareToken;
    const revisions = [...V.compareRevisions];
    V.compareResult = { loading: true };
    await render();
    let result = null;
    try {
      result = await config.compare(revisions[0], revisions[1]);
    } catch (_) { /* api() 已显示具体错误 */ }
    if (token !== V.compareToken) return;
    V.compareResult = result;
    await render();
  }
  V.runCompare = runCompare;

  async function exitCompare() {
    V.compareResult = null;
    await render();
  }
  V.exitCompare = exitCompare;

  function setCompareStyle(style) {
    V.compareStyle = style === "split" ? "split" : "inline";
    localStorage.setItem("mc.diffStyle", V.compareStyle);
    void render();
  }
  V.setCompareStyle = setCompareStyle;

  async function setRaw(raw) {
    V.raw = Boolean(raw);
    await render();
  }
  V.setRaw = setRaw;

  function setEditSurface(surface) {
    V.editPreview = surface === "preview";
    const editor = document.getElementById(config.textareaId);
    const preview = container.querySelector(".viewer-edit-preview");
    if (editor) editor.hidden = V.editPreview;
    if (preview) preview.hidden = !V.editPreview;
    container.querySelectorAll("[data-vact='edit-surface']").forEach(button =>
      button.classList.toggle("active",
        (button.dataset.surface === "preview") === V.editPreview));
    if (V.editPreview) updateEditPreview();
  }
  V.setEditSurface = setEditSurface;

  function downloadCurrent() {
    if (!config.downloadUrl) return;
    const link = document.createElement("a");
    link.href = config.downloadUrl(V.viewingRevision, false);
    link.download = String(config.title()).split("/").pop();
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  textViewerRegistry.push(V);
  return V;
}

/* ---- 跨页面未保存守卫 ----
   各 viewer 的脏状态 + 宿主注册的额外检查（如新建文档草稿）。
   标签页切换不会销毁编辑现场（重新渲染有脏守卫），因此只在真正会
   丢失修改的入口（切换条目/项目、路由跳转、关闭页面）调用确认。 */
const textViewerRegistry = [];
const viewerDirtyCheckers = [];

function registerViewerDirtyChecker(fn) {
  viewerDirtyCheckers.push(fn);
}

function hasUnsavedViewerEdits() {
  return textViewerRegistry.some(viewer => viewer.hasUnsaved())
    || (typeof configEditorDirty !== "undefined" && configEditorDirty.skills)
    || viewerDirtyCheckers.some(check => {
      try { return check(); } catch (_) { return false; }
    });
}

async function confirmDiscardUnsaved() {
  if (!hasUnsavedViewerEdits()) return true;
  return uiConfirm(
    "有尚未保存的修改，继续操作将丢失这些修改。是否继续？", "未保存的修改");
}

window.addEventListener("beforeunload", event => {
  if (!hasUnsavedViewerEdits()) return;
  event.preventDefault();
  event.returnValue = "";
});
