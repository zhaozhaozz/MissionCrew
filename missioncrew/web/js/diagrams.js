/* ---- Markdown 图表:Mermaid 按需渲染 ----
   miniMarkdown 把 ```mermaid 围栏输出为 .markdown-diagram 容器并保留源码;这里监听
   DOM 变化,页面首次出现图表时才加载 vendor 里的 Mermaid,把源码渲染成 SVG。
   渲染库加载失败或图表语法错误时保留源码并标注原因,不影响其余正文。 */
(function () {
  const LIBRARY_URL = "/assets/vendor/mermaid.min.js";
  const rendered = new Map();   // `${主题}\n${源码}` → SVG;轮询重绘同一消息时直接复用
  let library = null;           // Promise<mermaid | null>,整页只加载一次
  let queue = Promise.resolve(); // 串行渲染:initialize 与 render 成对执行,主题切换不串台
  let sequence = 0;

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
      ensureChild(node, "markdown-diagram-canvas").innerHTML = svg;
      node.querySelector(":scope > .markdown-diagram-error")?.remove();
      pre.hidden = true;
      node.dataset.state = "done";
    } catch (error) {
      if (!node.isConnected || node.dataset.theme !== theme) return;
      const reason = String(error?.message || error).split("\n")[0].slice(0, 200);
      ensureChild(node, "markdown-diagram-error").textContent = `图表未能渲染:${reason}`;
      node.querySelector(":scope > .markdown-diagram-canvas")?.remove();
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
  }

  if (typeof document === "undefined" || typeof MutationObserver === "undefined") return;
  new MutationObserver(records => {
    for (const record of records) record.addedNodes.forEach(scan);
  }).observe(document.body, { childList: true, subtree: true });
  new MutationObserver(refreshAll)
    .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refreshAll);
  scan(document.body);
})();
