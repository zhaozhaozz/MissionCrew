/* ---------------- 自定义面板 ----------------
   两种形态:widgets(主控 Agent 通过 dashboard.save 创建维护的组件网格)与
   taskboard(任务看板:选数据源,按标签筛选列分列)。日常只读渲染;
   编辑模式从侧栏面板列表的 ✎ 进入——widgets 面板可继续与主控在面板专属频道
   对话,或直接改布局 JSON;taskboard 面板改名称与数据源,筛选列在看板页
   顶部工具条直接增删(每列一个标签表达式,卡片状态也是可筛选标签)。
   列定义与卡片由服务端数据源注册表统一解析(/boards/{id}/data),前端只做
   通用渲染,不感知数据来自任务还是脚本同步的外部列表(如 GitCode Issue)。 */
let currentCustomBoard = null;
let customBoardEditing = false;
let boardEditorVisible = false;   // JSON 编辑表单是否展开(仅编辑模式内)
let boardEditMode = false;        // 编辑模式:侧栏 ✎ 进入,"完成编辑"退出
let customBoardRenderSignature = null;
let boardWidgetRenderToken = 0;
let taskboardRenderToken = 0;

/* ---- 看板数据源清单:按项目缓存;取不到时回退内置任务源 ---- */
let boardSourcesCache = { project: null, list: null };

async function loadBoardSources() {
  if (boardSourcesCache.project !== currentProject || !boardSourcesCache.list) {
    try {   // 静默取数:失败走回退,不弹窗
      const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/board_sources`);
      if (r.ok) boardSourcesCache = { project: currentProject, list: await r.json() };
    } catch (_) { /* ignore */ }
  }
  return (boardSourcesCache.project === currentProject && boardSourcesCache.list)
    || [{ id: "tasks", name: "项目任务", description: "" }];
}

function boardSourceName(id) {
  const hit = (boardSourcesCache.list || []).find(s => s.id === id);
  return hit ? hit.name : (id || "tasks");
}

function boardSourceOptionsHtml(selected) {
  return (boardSourcesCache.list || [{ id: "tasks", name: "项目任务", description: "" }])
    .map(s => `<option value="${esc(s.id)}"${s.id === selected ? " selected" : ""}>
      ${esc(s.name)}${s.description ? ` — ${esc(s.description)}` : ""}</option>`).join("");
}

function currentCustomBoardStateSignature() {
  const board = projBoards().find(value => value.id === currentCustomBoard) || null;
  return JSON.stringify([currentProject, currentCustomBoard, boardEditorVisible,
                         boardEditMode, board]);
}

function boardHasLiveWidgets(board) {
  // taskboard(组件或整板)直接读 overview 里的任务数据,需随轮询重绘
  if (board?.kind === "taskboard") return true;
  return (board?.layout || []).some(widget =>
    widget.content?.source || widget.type === "taskboard");
}

function renderCustomBoards(force = false) {
  if (customBoardEditing && !force) return;
  const boards = projBoards();
  if (currentCustomBoard && !boards.some(b => b.id === currentCustomBoard)) {
    currentCustomBoard = null;
    boardEditMode = false;
  }
  if (!currentCustomBoard && boards.length) currentCustomBoard = boards[0].id;
  const board = boards.find(value => value.id === currentCustomBoard);
  const signature = currentCustomBoardStateSignature();
  if (!force && signature === customBoardRenderSignature) {
    if (boardHasLiveWidgets(board)) renderBoardContent(board);
    return;
  }
  renderCustomBoardEditor();
}

function selectCustomBoard(id) {
  customBoardEditing = false;
  boardEditorVisible = false;
  boardEditMode = false;
  currentCustomBoard = id || null;
  renderCustomBoardEditor();
  syncUrl();
}

function toggleBoardEditor() {
  boardEditorVisible = !boardEditorVisible;
  if (!boardEditorVisible) customBoardEditing = false;
  renderCustomBoardEditor();
}

function exitBoardEditMode() {
  boardEditMode = false;
  boardEditorVisible = false;
  customBoardEditing = false;
  renderCustomBoardEditor();
}

function renderCustomBoardEditor() {
  const form = document.getElementById("custom-board-form");
  const preview = document.getElementById("custom-board-preview");
  if (!form || !preview) return;
  const board = projBoards().find(b => b.id === currentCustomBoard);
  customBoardRenderSignature = currentCustomBoardStateSignature();
  const title = document.getElementById("custom-board-title");
  const desc = document.getElementById("custom-board-desc");
  const jsonBtn = document.getElementById("custom-board-json-btn");
  const doneBtn = document.getElementById("custom-board-done-btn");
  if (!board) {
    if (title) title.textContent = "自定义面板";
    if (desc) desc.textContent = "";
    if (jsonBtn) jsonBtn.hidden = true;
    if (doneBtn) doneBtn.hidden = true;
    form.style.display = "none";
    preview.classList.remove("taskboard-mode");
    preview.innerHTML = `<div class="empty" style="grid-column:1/-1">还没有面板:点击侧栏「面板」的 ＋ 新建。</div>`;
    if (typeof updateConfigChatContext === "function") updateConfigChatContext();
    return;
  }
  const editable = boardEditMode && board.kind !== "taskboard";
  if (title) title.textContent = (board.name || board.id)
    + (boardEditMode ? " · 编辑中" : "");
  if (desc) desc.textContent = board.kind === "taskboard"
    ? `数据源:${boardSourceName(board.source)}`
    : (board.description || "");
  if (jsonBtn) jsonBtn.hidden = !editable;
  if (doneBtn) doneBtn.hidden = !boardEditMode;
  if (!editable || !boardEditorVisible) {
    form.style.display = "none";
  } else {
    form.style.display = "block";
    form.innerHTML = `
      <div class="row">
        <div><label>面板 id</label><input id="cb-id" value="${esc(board.id.split(":").pop())}" disabled></div>
        <div><label>名称</label><input id="cb-name" oninput="customBoardEditing=true" value="${esc(board.name)}"></div>
      </div>
      <label>用途说明</label><input id="cb-desc" oninput="customBoardEditing=true" value="${esc(board.description || "")}">
      <label>布局 JSON(应急手动编辑;日常修改建议在下方主控对话里提需求)</label>
      <textarea id="cb-layout" rows="14" spellcheck="false" oninput="previewBoardDraft()">${esc(JSON.stringify(board.layout || [], null, 2))}</textarea>
      <div class="form-actions"><button class="action" onclick="saveCustomBoard()">保存面板</button>
        <button class="danger" onclick="deleteCustomBoard()">删除面板</button></div>`;
  }
  renderBoardContent(board);
  if (typeof updateConfigChatContext === "function") updateConfigChatContext();
}

function renderBoardContent(board) {
  if (!board) return;
  if (board.kind === "taskboard") renderTaskboardBoard(board);
  else renderBoardWidgets(board.layout || []);
}

/* ---- 标签表达式:与 & 或 | 非 ! 与括号;标签不区分大小写,&&/|| 同义 ---- */
function compileLabelQuery(expr) {
  const tokens = [];
  let buffer = "";
  const flush = () => {
    const text = buffer.trim();
    if (text) tokens.push({ label: text.toLowerCase() });
    buffer = "";
  };
  for (let i = 0; i < expr.length; i++) {
    const ch = expr[i];
    if ("&|!()".includes(ch)) {
      flush();
      if ((ch === "&" || ch === "|") && expr[i + 1] === ch) i++;
      tokens.push(ch);
    } else buffer += ch;
  }
  flush();
  if (!tokens.length) throw new Error("表达式不能为空");
  let pos = 0;
  const peek = () => tokens[pos];
  const parseOr = () => {
    let node = parseAnd();
    while (peek() === "|") {
      pos++;
      const left = node, right = parseAnd();
      node = labels => left(labels) || right(labels);
    }
    return node;
  };
  const parseAnd = () => {
    let node = parseNot();
    while (peek() === "&") {
      pos++;
      const left = node, right = parseNot();
      node = labels => left(labels) && right(labels);
    }
    return node;
  };
  const parseNot = () => {
    const token = peek();
    if (token === "!") { pos++; const inner = parseNot(); return labels => !inner(labels); }
    if (token === "(") {
      pos++;
      const inner = parseOr();
      if (peek() !== ")") throw new Error("缺少右括号");
      pos++;
      return inner;
    }
    if (!token || typeof token === "string") throw new Error("运算符后缺少标签");
    pos++;
    const label = token.label;
    return labels => labels.has(label);
  };
  const matcher = parseOr();
  if (pos !== tokens.length) throw new Error("表达式有多余内容");
  return matcher;
}

/* 看板通用卡片:task_id 打开平台任务详情,url 打开外部页面(如 GitHub Issue) */
function boardCardHtml(card) {
  const click = card.task_id
    ? `data-task-id="${esc(card.task_id)}" onclick="openTask(this.dataset.taskId)"`
    : (card.url ? `data-url="${esc(card.url)}" onclick="window.open(this.dataset.url,'_blank','noopener')"` : "");
  const meta = [`更新 ${new Date(card.updated_at * 1000).toLocaleString()}`,
                ...(card.meta || [])];
  return `<div class="card" ${click}>
    <div class="title">${esc(card.title)}</div>
    ${card.summary ? `<div class="task-card-summary">${esc(card.summary)}</div>` : ""}
    <div class="meta">${meta.map(esc).join(" · ")}</div>
    <div class="meta">${(card.labels || []).map(l => `<span class="badge">${esc(l)}</span>`).join("")}</div>
  </div>`;
}

/* ---- 筛选列工具条:每列一个标签表达式,状态(待处理/处理中/…)也是标签 ---- */
let taskboardLastData = null;   // 最近一次 /data 响应,增删筛选列时物化当前列

function taskboardFilterBarHtml(data) {
  const chips = data.columns.map((col, i) =>
    `<span class="tb-chip" title="${esc(col.query)}">${esc(col.title)}<button
       class="tb-chip-x" data-index="${i}" title="移除此列"
       onclick="removeTaskboardFilter(+this.dataset.index)">×</button></span>`).join("");
  const options = (data.labels || [])
    .map(l => `<option value="${esc(l)}"></option>`).join("");
  return `<div class="tb-filter-bar">
    <span class="muted">筛选列:</span>${chips}
    <input id="tb-new-filter" list="tb-label-options"
      placeholder="标签表达式,如 处理中 & bug"
      onkeydown="if(event.key==='Enter')addTaskboardFilter()">
    <datalist id="tb-label-options">${options}</datalist>
    <button class="ghost" onclick="addTaskboardFilter()">＋加列</button>
  </div>`;
}

function currentTaskboardFilters() {
  // 面板未持久化 filters 时(旧版/默认状态列),把当前渲染列物化成显式筛选列
  return (taskboardLastData?.columns || [])
    .map(c => ({ title: c.title, query: c.query, color: c.color || "" }));
}

async function saveTaskboardFilters(filters) {
  const board = projBoards().find(b => b.id === currentCustomBoard);
  if (!board) return;
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/boards`, {
    id: board.id.replace(`${currentProject}:`, ""), name: board.name, filters,
  });
  await loadOverview();
  renderCustomBoards(true);
}

async function addTaskboardFilter() {
  const input = document.getElementById("tb-new-filter");
  const query = (input?.value || "").trim();
  if (!query) return;
  try { compileLabelQuery(query); }
  catch (error) { uiAlert(`标签表达式不合法:${error.message}`); return; }
  await saveTaskboardFilters(
    [...currentTaskboardFilters(), { title: query, query, color: "" }]);
  toast("已添加筛选列", "success");
}

async function removeTaskboardFilter(index) {
  const filters = currentTaskboardFilters();
  if (filters.length <= 1) { uiAlert("看板至少保留一列"); return; }
  filters.splice(index, 1);
  await saveTaskboardFilters(filters);
}

async function renderTaskboardBoard(board) {
  const preview = document.getElementById("custom-board-preview");
  if (!preview) return;
  preview.classList.add("taskboard-mode");
  const token = ++taskboardRenderToken;
  const shortId = board.id.replace(`${currentProject}:`, "");
  let data;
  try {   // 服务端解析:数据源取数 + 按筛选列分列;失败保留上一帧,首帧才提示
    const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/boards/${encodeURIComponent(shortId)}/data`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `请求失败 (${r.status})`);
    }
    data = await r.json();
  } catch (error) {
    if (token !== taskboardRenderToken) return;
    if (!preview.querySelector(".taskboard-grid"))
      preview.innerHTML = `<div class="empty">看板数据不可用:${esc(error.message)};点击侧栏 ✎ 修改。</div>`;
    return;
  }
  if (token !== taskboardRenderToken) return;
  taskboardLastData = data;
  const desc = document.getElementById("custom-board-desc");
  if (desc) desc.textContent =
    `数据源:${data.source.name} · ${data.columns.length} 列`;
  const scrollState = captureKeyedScrollPositions(preview);
  // 轮询重绘会整体替换 DOM:保留筛选输入框的草稿与焦点
  const prevInput = preview.querySelector("#tb-new-filter");
  const draft = prevInput ? prevInput.value : "";
  const hadFocus = prevInput && document.activeElement === prevInput;
  const columns = data.columns.map(col => {
    const cards = col.cards.map(boardCardHtml).join("")
      || `<div class="empty" style="padding:6px 4px">暂无条目</div>`;
    return `<section class="col"><h2><span class="col-dot" style="background:${esc(col.color || "var(--muted)")}"></span>
      ${esc(col.title)}<span class="col-count">${col.cards.length}</span></h2>
      <div class="col-list" data-scroll-key="tb:${esc(col.key)}">${cards}</div></section>`;
  }).join("");
  preview.innerHTML = taskboardFilterBarHtml(data)
    + `<div class="taskboard-grid">${columns}</div>`;
  const nextInput = preview.querySelector("#tb-new-filter");
  if (nextInput && draft) nextInput.value = draft;
  if (nextInput && hadFocus) nextInput.focus();
  restoreKeyedScrollPositions(preview, scrollState);
}

let _previewTimer = null;
function previewBoardDraft() {
  customBoardEditing = true;
  clearTimeout(_previewTimer);   // 防抖:避免每个按键都触发数据源解析请求
  _previewTimer = setTimeout(() => {
    try { renderBoardWidgets(JSON.parse(document.getElementById("cb-layout").value)); }
    catch (_) { document.getElementById("custom-board-preview").innerHTML = `<div class="empty">布局 JSON 尚未完成。</div>`; }
  }, 400);
}

// ---- 组件渲染器:按 type 分派,未知类型回退 JSON 展示 ----
function widgetTable(columns, rows) {
  const rowList = Array.isArray(rows) ? rows : [];
  // 列定义:[{key,label}] / [str];缺省从首行对象键自动推导
  let cols = Array.isArray(columns) ? columns.map(c =>
    typeof c === "string" ? { key: c, label: c } : c) : [];
  if (!cols.length && rowList.length && !Array.isArray(rowList[0]))
    cols = Object.keys(rowList[0]).map(k => ({ key: k, label: k }));
  const body = rowList.map(r => {
    const cells = Array.isArray(r) ? r : cols.map(c => (r || {})[c.key]);
    return `<tr>${cells.map(c => `<td>${esc(typeof c === "object" && c !== null ? JSON.stringify(c) : c ?? "")}</td>`).join("")}</tr>`;
  }).join("");
  return `<table><tr>${cols.map(c => `<th>${esc(c.label ?? c.key)}</th>`).join("")}</tr>${
    body || `<tr><td colspan="${cols.length || 1}" class="empty">暂无数据</td></tr>`}</table>`;
}

// 极简 SVG 图表:bar / line / pie,无外部依赖
function widgetChart(c) {
  const data = Array.isArray(c.data) ? c.data : [];
  if (!data.length) return `<div class="empty">暂无数据</div>`;
  const xk = c.x_key || c.xKey || "x", yk = c.y_key || c.yKey || "y";
  const vals = data.map(d => Number(d[yk]) || 0);
  const labels = data.map(d => String(d[xk] ?? ""));
  const W = 560, H = 200, max = Math.max(...vals, 1);
  const colors = ["#3564d7", "#2e9e5b", "#c98a1b", "#c94b3c", "#8b5cf6", "#0e9488"];
  if (c.kind === "pie") {
    const total = vals.reduce((a, b) => a + b, 0) || 1;
    let angle = -Math.PI / 2, slices = "", legend = "";
    vals.forEach((v, i) => {
      const a2 = angle + v / total * Math.PI * 2;
      const large = (a2 - angle) > Math.PI ? 1 : 0;
      const [x1, y1] = [100 + 80 * Math.cos(angle), 100 + 80 * Math.sin(angle)];
      const [x2, y2] = [100 + 80 * Math.cos(a2), 100 + 80 * Math.sin(a2)];
      slices += `<path d="M100,100 L${x1},${y1} A80,80 0 ${large} 1 ${x2},${y2} Z" fill="${colors[i % colors.length]}"></path>`;
      legend += `<span class="pill"><span style="color:${colors[i % colors.length]}">●</span> ${esc(labels[i])}: ${vals[i]}</span> `;
      angle = a2;
    });
    return `<svg viewBox="0 0 200 200" style="max-height:160px">${slices}</svg><div>${legend}</div>`;
  }
  const bw = W / data.length;
  let marks = "";
  if (c.kind === "line") {
    const pts = vals.map((v, i) => `${bw * i + bw / 2},${H - 20 - v / max * (H - 40)}`).join(" ");
    marks = `<polyline points="${pts}" fill="none" stroke="${colors[0]}" stroke-width="2"></polyline>` +
      vals.map((v, i) => `<circle cx="${bw * i + bw / 2}" cy="${H - 20 - v / max * (H - 40)}" r="3" fill="${colors[0]}"></circle>`).join("");
  } else {   // bar
    marks = vals.map((v, i) =>
      `<rect x="${bw * i + bw * 0.15}" y="${H - 20 - v / max * (H - 40)}" width="${bw * 0.7}" height="${v / max * (H - 40)}" fill="${colors[0]}" rx="2"></rect>`).join("");
  }
  const axis = labels.map((l, i) =>
    `<text x="${bw * i + bw / 2}" y="${H - 5}" font-size="10" text-anchor="middle" fill="currentColor" opacity=".6">${esc(l.slice(0, 8))}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%">${marks}${axis}</svg>`;
}

function renderWidgetContent(w, resolved) {
  if (resolved && resolved.error)
    return `<div class="empty">${esc(resolved.error)}</div>`;
  const c = { ...(w.content || {}), ...(resolved || {}) };
  switch (w.type) {
    case "markdown":
      return `<div class="markdown-body">${markdownPreviewHtml(c.markdown || c.text || "")}</div>`;
    case "table":
      return widgetTable(c.columns, c.rows);
    case "card": {
      const items = Array.isArray(c.metrics) ? c.metrics
        : Object.entries(c).filter(([k]) => k !== "source")
            .map(([label, value]) => ({ label, value }));
      return `<div style="display:flex;flex-wrap:wrap;gap:14px">` + (items.map(m =>
        `<div><div style="font-size:22px;font-weight:700">${esc(m.value)}${m.unit ? `<span class="muted" style="font-size:12px"> ${esc(m.unit)}</span>` : ""}</div>
         <div class="muted">${esc(m.label)}</div></div>`).join("")
        || `<div class="empty">暂无指标</div>`) + `</div>`;
    }
    case "chart":
      return widgetChart(c);
    case "list": {
      const items = (Array.isArray(c.items) ? c.items : []).map(it => {
        const o = typeof it === "string" ? { text: it } : (it || {});
        const tone = { danger: "var(--bad)", warning: "var(--warn)", success: "var(--ok)" }[o.tone];
        return `<li${tone ? ` style="color:${tone}"` : ""}>${esc(o.text ?? "")}</li>`;
      }).join("");
      return items ? `<ul style="margin:4px 0;padding-left:18px">${items}</ul>`
                   : `<div class="empty">暂无条目</div>`;
    }
    case "log": {
      const lines = Array.isArray(c.lines) ? c.lines.join("\n") : (c.text || "");
      return `<pre style="white-space:pre-wrap;margin:0;font-size:12px;max-height:100%;overflow:auto">${esc(lines) || "(空)"}</pre>`;
    }
    case "code":
      return `${c.language ? `<span class="pill">${esc(c.language)}</span>` : ""}
        <pre style="white-space:pre-wrap;margin:4px 0 0;font-size:12px">${esc(c.code || "")}</pre>`;
    case "taskboard": {   // 标签任务看板:与看板页标签列同数据、同规则入口
      const label = String(c.label || "").trim();
      if (!label) return `<div class="empty">缺少 content.label(要聚合的 Task 标签)</div>`;
      const tasks = projTasks().filter(task =>
        !task.archived && taskHasLabel(task, label));
      const rule = ruleForLabel(label);
      const ruleText = rule
        ? (rule.enabled ? "⚡ 自动规则已启用" : "⚡ 自动规则已停用") : "⚡ 设置自动规则";
      const cards = tasks.map(task => taskCardHtml(task, { showStatus: true })).join("")
        || `<div class="empty">暂无带此标签的 Task</div>`;
      return `<div class="widget-taskboard-head">
          <span class="badge">${esc(label)}</span>
          <span class="muted">${tasks.length} 个 Task</span>
          <button class="ghost" data-label="${esc(label)}"
            onclick="openLabelRule(this.dataset.label)">${ruleText}</button>
        </div><div class="widget-taskboard-cards">${cards}</div>`;
    }
    default:
      return `<pre style="white-space:pre-wrap;margin:0">${esc(JSON.stringify(c, null, 2))}</pre>`;
  }
}

async function renderBoardWidgets(layout) {
  const renderToken = ++boardWidgetRenderToken;
  const preview = document.getElementById("custom-board-preview");
  preview?.classList.remove("taskboard-mode");
  const widgets = Array.isArray(layout) ? layout : [];
  // 有数据源的卡片:批量向平台解析(保存态与预览态共用同一端点)
  let resolved = {};
  const sourced = widgets.filter(w => w.content && w.content.source);
  if (sourced.length && currentProject) {
    try {   // 静默取数:失败时按静态内容渲染,不弹窗
      const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/widget_data`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ widgets: sourced }),
      });
      if (r.ok) resolved = await r.json();
    } catch (e) { /* ignore */ }
  }
  if (renderToken !== boardWidgetRenderToken || !preview) return;
  const scrollState = captureKeyedScrollPositions(preview);
  preview.innerHTML = widgets.map(w =>
    `<section class="widget" data-scroll-key="widget:${esc(w.id)}" style="grid-column:${Number(w.x || 0) + 1}/span ${Number(w.width || 6)};grid-row:${Number(w.y || 0) + 1}/span ${Number(w.height || 4)}">
      <span class="widget-type">${esc(w.type)}${w.content?.source ? " · 实时" : ""}</span><h3>${esc(w.title || w.id)}</h3>${renderWidgetContent(w, resolved[w.id])}</section>`
  ).join("") || `<div class="empty" style="grid-column:1/-1">面板中还没有组件。</div>`;
  restoreKeyedScrollPositions(preview, scrollState);
}

async function saveCustomBoard() {
  let layout;
  try { layout = JSON.parse(document.getElementById("cb-layout").value); }
  catch (_) { uiAlert("布局必须是合法 JSON"); return; }
  const id = document.getElementById("cb-id").value.trim();
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/boards`, {
    id, name: document.getElementById("cb-name").value.trim(),
    description: document.getElementById("cb-desc").value, layout,
  });
  currentCustomBoard = `${currentProject}:${id}`;
  customBoardEditing = false;
  window._newBoardDraft = null;
  await loadOverview(); renderCustomBoards(true);
  toast("面板已保存", "success");
}

async function deleteCustomBoard(boardId = null) {
  const board = projBoards().find(b => b.id === (boardId || currentCustomBoard));
  if (!board || !await uiConfirm(`将面板「${board.name}」移入项目回收站？`)) return;
  const shortId = board.id.replace(`${currentProject}:`, "");
  await api("DELETE", `/api/projects/${encodeURIComponent(currentProject)}/boards/${encodeURIComponent(shortId)}`);
  if (currentCustomBoard === board.id) currentCustomBoard = null;
  customBoardEditing = false;
  boardEditMode = false;
  if (fdlg.open) fdlg.close();
  await loadOverview(); renderCustomBoards(true);
  toast("面板已移入回收站", "success");
}

/* ---------------- 新建面板与 taskboard 编辑 ---------------- */

async function openNewBoardDialog() {
  if (!currentProject) { uiAlert("请先创建/选择项目"); return; }
  await loadBoardSources();
  openFormDialog("新建面板", `
    <label>面板类型</label>
    <div class="new-board-kinds">
      <label><input type="radio" name="nb-kind" value="taskboard" checked
        onchange="toggleNewBoardKind()"> 任务看板 <span class="muted">选数据源,按标签表达式筛选,按状态分列</span></label>
      <label><input type="radio" name="nb-kind" value="agent"
        onchange="toggleNewBoardKind()"> 主控创建面板 <span class="muted">一句话提需求,主控用组件搭建</span></label>
    </div>
    <div id="nb-taskboard">
      <label>名称</label><input type="text" id="nb-name" placeholder="例如 缺陷追踪">
      <label>数据源</label>
      <select id="nb-source">${boardSourceOptionsHtml("tasks")}</select>
      <p class="muted">创建后按数据源状态列分列;可在看板顶部用标签表达式增删筛选列,
        状态(待处理/处理中/已阻塞/已完成)也是可筛选标签。</p>
    </div>
    <div id="nb-agent" style="display:none">
      <label>需求描述</label>
      <textarea id="nb-request" rows="4" style="height:auto"
        placeholder="例如:建一个需求管理面板,上面是需求清单表格,下面实时显示进行中的任务"></textarea>
      <p class="muted">需求会发到项目 general 频道交给主控,过程可在聊天页查看;创建完成后面板自动出现在侧栏,之后的修改在面板编辑模式的专属频道里继续对话。</p>
    </div>`,
    `<button class="action" onclick="submitNewBoard()">创建</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
}

function toggleNewBoardKind() {
  const kind = document.querySelector('input[name="nb-kind"]:checked')?.value;
  document.getElementById("nb-taskboard").style.display =
    kind === "taskboard" ? "block" : "none";
  document.getElementById("nb-agent").style.display =
    kind === "agent" ? "block" : "none";
}

async function submitNewBoard() {
  const kind = document.querySelector('input[name="nb-kind"]:checked')?.value;
  if (kind === "taskboard") {
    const name = document.getElementById("nb-name").value.trim();
    const source = document.getElementById("nb-source").value;
    if (!name) { uiAlert("名称不能为空"); return; }
    const shortId = `tb-${Math.random().toString(36).slice(2, 8)}`;
    await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/boards`, {
      id: shortId, name, kind: "taskboard", source, layout: [],
    });
    fdlg.close();
    currentCustomBoard = `${currentProject}:${shortId}`;
    boardEditMode = false;
    await loadOverview();
    if (currentTab !== "custom") switchTab("custom");
    else { renderCustomBoards(true); renderSidebar(); syncUrl(); }
    toast(`已创建任务看板「${name}」`, "success");
    return;
  }
  // 主控创建:需求发到 general 频道,创建过程全程可见
  const project = projObj();
  const request = document.getElementById("nb-request").value.trim();
  if (!request) { uiAlert("请描述你想要的面板"); return; }
  const channel = projChannels().find(c =>
    c.id.endsWith(":general") || c.id === "general") || projChannels()[0];
  if (!channel) { uiAlert("本项目还没有频道,请先在项目设置中创建"); return; }
  await api("POST", `/api/chat/${encodeURIComponent(channel.id)}/messages`, {
    author: "human",
    content: `@${project.orchestrator_role_id} 自定义面板需求:${request}\n` +
      `请用 MissionCrew Agent Tool 的 dashboard.save 完成,面板 id 用英文短横线命名。`,
  });
  fdlg.close();
  toast(`已交给主控 @${project.orchestrator_role_id}(频道 #${channel.name});完成后面板会出现在侧栏`, "success", 6000);
}

async function openTaskboardDialog(boardId) {
  const board = projBoards().find(item => item.id === boardId);
  if (!board) return;
  await loadBoardSources();
  openFormDialog(`编辑任务看板 · ${board.name || board.id}`, `
    <label>名称</label>
    <input type="text" id="tbf-name" value="${esc(board.name || "")}">
    <label>数据源</label>
    <select id="tbf-source">${boardSourceOptionsHtml(board.source || "tasks")}</select>
    <p class="muted">筛选列在看板顶部工具条直接增删,每列一个标签表达式。</p>`,
    `<button class="action" data-id="${esc(board.id)}"
       onclick="saveTaskboardDialog(this.dataset.id)">保存</button>
     <button class="danger" data-id="${esc(board.id)}"
       onclick="deleteCustomBoard(this.dataset.id)">删除面板</button>
     <button class="ghost" onclick="fdlg.close()">取消</button>`);
}

async function saveTaskboardDialog(boardId) {
  const board = projBoards().find(item => item.id === boardId);
  if (!board) return;
  const name = document.getElementById("tbf-name").value.trim();
  const source = document.getElementById("tbf-source").value;
  if (!name) { uiAlert("名称不能为空"); return; }
  await api("POST", `/api/projects/${encodeURIComponent(currentProject)}/boards`, {
    id: board.id.replace(`${currentProject}:`, ""), name, source,
  });
  fdlg.close();
  await loadOverview();
  renderCustomBoards(true);
  renderSidebar();
  toast("任务看板已更新", "success");
}
