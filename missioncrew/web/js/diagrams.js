/* ---- Markdown 图表:Mermaid 按需渲染与放大预览 ----
   miniMarkdown 把 ```mermaid 围栏输出为 .markdown-diagram 容器并保留源码;这里监听
   DOM 变化,页面首次出现图表时才加载 vendor 里的 Mermaid,把源码渲染成 SVG。
   渲染库加载失败或图表语法错误时保留源码并标注原因,不影响其余正文。
   渲染成功的图表支持点击画布或角上放大按钮在模态弹窗中全屏预览,并支持缩放与拖动平移。 */
(function () {
  const LIBRARY_URL = "/assets/vendor/mermaid.min.js";
  const MIN_SCALE = 0.2;
  const MAX_SCALE = 5;
  const ZOOM_STEP = 1.25;
  const STAGE_PAD = 56;
  const rendered = new Map();   // `${主题}\n${源码}` → SVG;轮询重绘同一消息时直接复用
  let library = null;           // Promise<mermaid | null>,整页只加载一次
  let queue = Promise.resolve(); // 串行渲染:initialize 与 render 成对执行,主题切换不串台
  let sequence = 0;
  let previewSequence = 0;
  let lightbox = null;

  function diagramTheme() {
    return isDarkTheme() ? "dark" : "default";
  }

  function loadLibrary() {
    if (!library) library = new Promise(resolve => {
      if (globalThis.mermaid) return resolve(globalThis.mermaid);
      const script = document.createElement("script");
      script.src = LIBRARY_URL;
      script.async = true;
      script.onload = () => resolve(globalThis.mermaid || null);
      script.onerror = () => resolve(null);
      document.head.appendChild(script);
    });
    return library;
  }

  function renderSvg(source, theme) {
    const key = `${theme}\n${source}`;
    if (rendered.has(key)) return Promise.resolve(rendered.get(key));
    const job = queue.then(async () => {
      const mermaid = await loadLibrary();
      if (!mermaid) throw new Error("渲染库加载失败");
      // strict:HTML 标签经净化、禁用点击回调,文档来自用户与 Agent 时也安全;
      // suppressErrorRendering:语法错误时不往页面塞 Mermaid 自带的错误图。
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict",
                           suppressErrorRendering: true, theme });
      const { svg } = await mermaid.render(`markdown-diagram-${++sequence}`, source);
      rendered.set(key, svg);
      return svg;
    });
    queue = job.catch(() => {});
    return job;
  }

  function ensureChild(node, className) {
    let child = node.querySelector(`:scope > .${className}`);
    if (!child) {
      child = document.createElement("div");
      child.className = className;
      node.prepend(child);
    }
    return child;
  }

  function ensureZoomButton(node) {
    let button = node.querySelector(":scope > .markdown-diagram-zoom");
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.className = "markdown-diagram-zoom";
      button.title = "放大查看";
      button.setAttribute("aria-label", "放大查看");
      node.appendChild(button);
    }
    return button;
  }

  /* ---- 放大预览弹窗 ---- */
  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // 克隆到弹窗的 SVG 与正文里的原图同在文档内,重命名内部 id 及所有引用避免 marker/clipPath 冲突
  function namespaceSvgIds(svg, prefix) {
    const idMap = new Map();
    const elements = [svg, ...svg.querySelectorAll("*")];
    for (const el of elements) {
      const id = el.getAttribute("id");
      if (!id) continue;
      const next = `${prefix}${id}`;
      idMap.set(id, next);
      el.setAttribute("id", next);
    }
    if (!idMap.size) return;
    // 按长度降序替换,避免短 id 误匹配带后缀的长 id(如 diagram-1 与 diagram-1_marker)
    const pattern = Array.from(idMap.keys())
      .sort((a, b) => b.length - a.length)
      .map(escapeRegExp)
      .join("|");
    const urlRe = new RegExp(`url\\((['"]?)#(${pattern})\\1\\)`, "g");
    const cssIdRe = new RegExp(`#(${pattern})(?![A-Za-z0-9_-])`, "g");
    const replaceUrl = value => value.replace(
      urlRe, (_, quote, id) => `url(${quote}#${idMap.get(id)}${quote})`);

    for (const el of elements) {
      if (el.tagName.toLowerCase() === "style") {
        let css = replaceUrl(el.textContent || "");
        css = css.replace(cssIdRe, (match, id) =>
          /^[0-9a-f]{3}([0-9a-f]{3})?$/i.test(id) ? match : `#${idMap.get(id)}`);
        el.textContent = css;
        continue;
      }
      for (const attr of Array.from(el.attributes || [])) {
        if (attr.name === "id") continue;
        const value = attr.value;
        if (!value || !value.includes("#")) continue;
        if ((attr.name === "href" || attr.name === "xlink:href") && value.startsWith("#")) {
          const mapped = idMap.get(value.slice(1));
          if (mapped) el.setAttribute(attr.name, `#${mapped}`);
        } else if (value.includes("url(")) {
          const replaced = replaceUrl(value);
          if (replaced !== value) el.setAttribute(attr.name, replaced);
        }
      }
    }
  }

  function measureSvg(svg, sourceSvg) {
    const vb = (svg.getAttribute("viewBox") || "").trim();
    if (vb) {
      const parts = vb.split(/[\s,]+/).map(Number);
      if (parts.length === 4 && parts[2] > 0 && parts[3] > 0) {
        return { width: parts[2], height: parts[3], hasViewBox: true };
      }
    }
    const rawW = svg.getAttribute("width") || "";
    const rawH = svg.getAttribute("height") || "";
    const attrW = rawW.includes("%") ? NaN : parseFloat(rawW);
    const attrH = rawH.includes("%") ? NaN : parseFloat(rawH);
    const rect = sourceSvg?.getBoundingClientRect?.();
    const width = Number.isFinite(attrW) && attrW > 0 ? attrW : (rect?.width || 600);
    const height = Number.isFinite(attrH) && attrH > 0 ? attrH : (rect?.height || 400);
    return { width, height, hasViewBox: false };
  }

  function preparePreviewSvg(sourceSvg) {
    const clone = sourceSvg.cloneNode(true);
    const { width, height, hasViewBox } = measureSvg(clone, sourceSvg);
    namespaceSvgIds(clone, `diagram-preview-${++previewSequence}-`);
    if (!hasViewBox) clone.setAttribute("viewBox", `0 0 ${width} ${height}`);
    clone.removeAttribute("width");
    clone.removeAttribute("height");
    clone.style.maxWidth = "none";
    clone.style.maxHeight = "none";
    return { svg: clone, width, height };
  }

  function rawFitScale(box) {
    const availW = Math.max(120, (box.stage.clientWidth || 800) - STAGE_PAD);
    const availH = Math.max(120, (box.stage.clientHeight || 600) - STAGE_PAD);
    return Math.min(availW / box.baseWidth, availH / box.baseHeight);
  }

  function computeFitScale(box) {
    return Math.min(rawFitScale(box), 2);
  }

  function minScaleFor(box) {
    return Math.min(MIN_SCALE, rawFitScale(box));
  }

  function applyScale(box, nextScale) {
    if (!box.svg) return;
    box.scale = clamp(nextScale, minScaleFor(box), MAX_SCALE);
    box.svg.style.width = `${box.baseWidth * box.scale}px`;
    box.svg.style.height = `${box.baseHeight * box.scale}px`;
    box.scaleLabel.textContent = `${Math.round(box.scale * 100)}%`;
  }

  function zoomAround(box, nextScale, clientX, clientY) {
    if (!box.svg) return;
    const oldScale = box.scale;
    const targetScale = clamp(nextScale, minScaleFor(box), MAX_SCALE);
    if (Math.abs(targetScale - oldScale) < 1e-4) return;
    const before = box.svg.getBoundingClientRect();
    const offsetX = clientX - before.left;
    const offsetY = clientY - before.top;
    applyScale(box, targetScale);
    const ratio = box.scale / oldScale;
    const after = box.svg.getBoundingClientRect();
    box.stage.scrollLeft += (after.left + offsetX * ratio) - clientX;
    box.stage.scrollTop += (after.top + offsetY * ratio) - clientY;
  }

  function zoomAroundCenter(box, nextScale) {
    const rect = box.stage.getBoundingClientRect();
    zoomAround(box, nextScale, rect.left + rect.width / 2, rect.top + rect.height / 2);
  }

  function fitLightbox(box) {
    box.fitScale = computeFitScale(box);
    applyScale(box, box.fitScale);
    box.stage.scrollLeft = 0;
    box.stage.scrollTop = 0;
  }

  function endDrag(box) {
    if (!box.drag) return;
    const { pointerId, active } = box.drag;
    box.drag = null;
    box.stage.classList.remove("dragging");
    if (active) {
      // 拖动画布滑出弹窗边缘再松手时,浏览器可能把随后的 click 落到 dialog 自身,避免误关弹窗
      box.suppressBackdropClick = true;
      setTimeout(() => { box.suppressBackdropClick = false; }, 0);
      if (box.stage.hasPointerCapture?.(pointerId)) {
        box.stage.releasePointerCapture(pointerId);
      }
    }
  }

  async function copyLightboxSource(box) {
    if (!box.source || typeof copyTextToClipboard !== "function") return;
    const copied = await copyTextToClipboard(box.source);
    if (!copied) {
      if (typeof toast === "function") toast("复制失败,请手动选中后复制", "error");
      return;
    }
    box.copyBtn.textContent = "已复制";
    clearTimeout(box.copyTimer);
    box.copyTimer = setTimeout(() => { box.copyBtn.textContent = "复制源码"; }, 1500);
  }

  function ensureLightbox() {
    if (lightbox) return lightbox;
    const dialog = document.createElement("dialog");
    dialog.id = "diagram-dlg";
    dialog.className = "diagram-lightbox";
    dialog.setAttribute("aria-label", "图表预览");
    dialog.innerHTML =
      `<div class="diagram-lightbox-bar">` +
        `<div class="diagram-lightbox-title">` +
          `<strong>图表预览</strong>` +
          `<span class="diagram-lightbox-hint muted">滚轮缩放 · 拖动平移 · 双击切换</span>` +
        `</div>` +
        `<div class="diagram-lightbox-controls">` +
          `<button type="button" class="ghost compact" data-action="zoom-out" title="缩小 (-)" aria-label="缩小">−</button>` +
          `<span class="diagram-lightbox-scale" aria-live="polite">100%</span>` +
          `<button type="button" class="ghost compact" data-action="zoom-in" title="放大 (+)" aria-label="放大">＋</button>` +
          `<button type="button" class="ghost compact" data-action="fit" title="适应窗口 (0)">适应</button>` +
          `<button type="button" class="ghost compact" data-action="reset" title="原始大小 (1)">1:1</button>` +
          `<button type="button" class="ghost compact" data-action="copy" title="复制源码">复制源码</button>` +
          `<button type="button" class="ghost compact" data-action="close" title="关闭 (Esc)">关闭</button>` +
        `</div>` +
      `</div>` +
      `<div class="diagram-lightbox-stage"><div class="diagram-lightbox-canvas"></div></div>`;
    document.body.appendChild(dialog);

    const box = {
      dialog,
      stage: dialog.querySelector(".diagram-lightbox-stage"),
      canvas: dialog.querySelector(".diagram-lightbox-canvas"),
      scaleLabel: dialog.querySelector(".diagram-lightbox-scale"),
      copyBtn: dialog.querySelector('[data-action="copy"]'),
      svg: null,
      baseWidth: 600,
      baseHeight: 400,
      scale: 1,
      fitScale: 1,
      source: "",
      copyTimer: null,
      drag: null,
      suppressBackdropClick: false,
    };

    dialog.addEventListener("click", event => {
      if (event.target === dialog) {
        if (!box.suppressBackdropClick) dialog.close();
        return;
      }
      const actionBtn = event.target.closest?.("button[data-action]");
      if (!actionBtn) return;
      const action = actionBtn.dataset.action;
      if (action === "zoom-in") zoomAroundCenter(box, box.scale * ZOOM_STEP);
      else if (action === "zoom-out") zoomAroundCenter(box, box.scale / ZOOM_STEP);
      else if (action === "fit") fitLightbox(box);
      else if (action === "reset") zoomAroundCenter(box, 1);
      else if (action === "copy") copyLightboxSource(box);
      else if (action === "close") dialog.close();
    });

    dialog.addEventListener("keydown", event => {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "+" || event.key === "=") {
        event.preventDefault(); zoomAroundCenter(box, box.scale * ZOOM_STEP);
      } else if (event.key === "-" || event.key === "_") {
        event.preventDefault(); zoomAroundCenter(box, box.scale / ZOOM_STEP);
      } else if (event.key === "0") {
        event.preventDefault(); fitLightbox(box);
      } else if (event.key === "1") {
        event.preventDefault(); zoomAroundCenter(box, 1);
      }
    });

    box.stage.addEventListener("wheel", event => {
      if (!box.svg) return;
      event.preventDefault();
      const factor = event.ctrlKey
        ? clamp(Math.exp(-event.deltaY * 0.01), 0.7, 1.4)
        : (event.deltaY < 0 ? 1.15 : 1 / 1.15);
      zoomAround(box, box.scale * factor, event.clientX, event.clientY);
    }, { passive: false });

    box.stage.addEventListener("dblclick", event => {
      if (!box.svg) return;
      if (Math.abs(box.scale - box.fitScale) < 0.05) {
        const target = Math.min(MAX_SCALE, Math.max(box.fitScale * 1.8, 1.25));
        zoomAround(box, target, event.clientX, event.clientY);
      } else {
        fitLightbox(box);
      }
    });

    box.stage.addEventListener("pointerdown", event => {
      if (event.button !== 0 || !box.svg) return;
      const rect = box.stage.getBoundingClientRect();
      // 点击滚动条轨道时留给浏览器原生滚动,不进入拖动画布
      if (event.clientX >= rect.left + box.stage.clientWidth
          || event.clientY >= rect.top + box.stage.clientHeight) return;
      box.drag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        scrollLeft: box.stage.scrollLeft,
        scrollTop: box.stage.scrollTop,
        active: false,
      };
    });

    box.stage.addEventListener("pointermove", event => {
      if (!box.drag || event.pointerId !== box.drag.pointerId) return;
      const dx = event.clientX - box.drag.startX;
      const dy = event.clientY - box.drag.startY;
      if (!box.drag.active && Math.hypot(dx, dy) > 3) {
        box.drag.active = true;
        box.stage.classList.add("dragging");
        box.stage.setPointerCapture?.(event.pointerId);
      }
      if (box.drag.active) {
        box.stage.scrollLeft = box.drag.scrollLeft - dx;
        box.stage.scrollTop = box.drag.scrollTop - dy;
      }
    });

    box.stage.addEventListener("pointerup", () => endDrag(box));
    box.stage.addEventListener("pointercancel", () => endDrag(box));

    dialog.addEventListener("close", () => {
      endDrag(box);
      clearTimeout(box.copyTimer);
      box.svg = null;
      box.source = "";
      box.canvas.innerHTML = "";
    });

    lightbox = box;
    return box;
  }

  function mountPreviewSvg(box, sourceSvg, preserveScale = false) {
    const prevScale = box.scale;
    const prevLeft = box.stage.scrollLeft;
    const prevTop = box.stage.scrollTop;
    const prepared = preparePreviewSvg(sourceSvg);
    box.svg = prepared.svg;
    box.baseWidth = prepared.width;
    box.baseHeight = prepared.height;
    box.canvas.innerHTML = "";
    box.canvas.appendChild(prepared.svg);
    if (preserveScale) {
      box.fitScale = computeFitScale(box);
      applyScale(box, prevScale);
      box.stage.scrollLeft = prevLeft;
      box.stage.scrollTop = prevTop;
    } else {
      fitLightbox(box);
    }
  }

  function openDiagramLightbox(node) {
    const sourceSvg = node.querySelector(":scope > .markdown-diagram-canvas svg");
    if (!sourceSvg) return;
    const box = ensureLightbox();
    const pre = node.querySelector(":scope > .markdown-diagram-source");
    box.source = pre ? pre.textContent : "";
    box.copyBtn.hidden = !box.source.trim();
    clearTimeout(box.copyTimer);
    box.copyBtn.textContent = "复制源码";
    if (!box.dialog.open) box.dialog.showModal();
    mountPreviewSvg(box, sourceSvg, false);
  }

  async function refreshLightboxTheme() {
    if (!lightbox?.dialog?.open || !lightbox.source) return;
    const source = lightbox.source;
    const theme = diagramTheme();
    try {
      const svgMarkup = await renderSvg(source, theme);
      if (!lightbox.dialog.open || lightbox.source !== source || diagramTheme() !== theme) return;
      const temp = document.createElement("div");
      temp.innerHTML = svgMarkup;
      const freshSvg = temp.querySelector("svg");
      if (freshSvg) mountPreviewSvg(lightbox, freshSvg, true);
    } catch (_) { /* 主题重绘失败时保留当前预览图 */ }
  }

  async function renderDiagram(node) {
    const theme = diagramTheme();
    if (node.dataset.state && node.dataset.theme === theme) return;
    const pre = node.querySelector(":scope > .markdown-diagram-source");
    const source = pre ? pre.textContent : "";
    if (!source.trim()) return;
    node.dataset.state = "pending";
    node.dataset.theme = theme;
    try {
      const svg = await renderSvg(source, theme);
      if (!node.isConnected || node.dataset.theme !== theme) return;
      const canvas = ensureChild(node, "markdown-diagram-canvas");
      canvas.innerHTML = svg;
      canvas.title = "点击放大查看";
      ensureZoomButton(node);
      node.querySelector(":scope > .markdown-diagram-error")?.remove();
      pre.hidden = true;
      node.dataset.state = "done";
    } catch (error) {
      if (!node.isConnected || node.dataset.theme !== theme) return;
      const reason = String(error?.message || error).split("\n")[0].slice(0, 200);
      ensureChild(node, "markdown-diagram-error").textContent = `图表未能渲染:${reason}`;
      node.querySelector(":scope > .markdown-diagram-canvas")?.remove();
      node.querySelector(":scope > .markdown-diagram-zoom")?.remove();
      pre.hidden = false;
      node.dataset.state = "error";
    }
  }

  function scan(root) {
    if (!(root instanceof Element)) return;
    const nodes = root.matches(".markdown-diagram") ? [root]
      : root.querySelectorAll(".markdown-diagram");
    nodes.forEach(node => { if (!node.dataset.state) renderDiagram(node); });
  }

  function refreshAll() {   // 主题切换后按新配色重绘页面上已有的图表
    document.querySelectorAll(".markdown-diagram").forEach(renderDiagram);
    refreshLightboxTheme();
  }

  if (typeof document === "undefined" || typeof MutationObserver === "undefined") return;
  document.addEventListener("click", event => {
    const zoomBtn = event.target.closest?.(".markdown-diagram-zoom");
    if (zoomBtn) {
      const node = zoomBtn.closest(".markdown-diagram");
      if (node) { event.preventDefault(); openDiagramLightbox(node); }
      return;
    }
    const canvas = event.target.closest?.(".markdown-diagram-canvas");
    if (!canvas || event.target.closest?.(".markdown-copy, a[href]")) return;
    const selection = globalThis.getSelection?.();
    if (selection && !selection.isCollapsed) return;
    const node = canvas.closest(".markdown-diagram");
    if (node) openDiagramLightbox(node);
  });
  new MutationObserver(records => {
    for (const record of records) record.addedNodes.forEach(scan);
  }).observe(document.body, { childList: true, subtree: true });
  new MutationObserver(refreshAll)
    .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refreshAll);
  scan(document.body);
})();
