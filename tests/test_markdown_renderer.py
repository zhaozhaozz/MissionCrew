import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_JS = ROOT / "missioncrew" / "web" / "js" / "markdown.js"
NODE = shutil.which("node")
BLOCK_COPY = '<button type="button" class="markdown-copy" title="复制"></button>'
INLINE_COPY = '<button type="button" class="markdown-copy" tabindex="-1" title="复制"></button>'


def render_markdown(markdown: str) -> str:
    if NODE is None:
        pytest.skip("node is required to execute the browser Markdown renderer")
    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const markdown = fs.readFileSync(0, "utf8");
const context = {
  location: { origin: "http://localhost" },
  esc: value => String(value ?? "").replace(
    /[&<>"]/g,
    char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char],
  ),
};
vm.createContext(context);
vm.runInContext(source, context);
process.stdout.write(
  vm.runInContext(`miniMarkdown(${JSON.stringify(markdown)})`, context),
);
"""
    result = subprocess.run(
        [NODE, "-e", script, str(MARKDOWN_JS)],
        input=markdown,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def test_relative_link_preserves_code_formatted_label():
    html = render_markdown(
        "- 原始结果: [`results.json`](./results.json)",
    )

    assert 'data-doc-link="./results.json"' in html
    assert f"<code>results.json{INLINE_COPY}</code>" in html
    assert ">undefined</a>" not in html


def test_table_pipe_inside_inline_code_does_not_split_cell():
    html = render_markdown(
        "| 对象 kind | 内容 | 类比 | 现状 |\n"
        "|---|---|---|---|\n"
        "| `tree` | 有序条目 `[{name, kind: blob|tree, hash, size, mode?}]`，表示目录 "
        "| git tree / Bazel `Directory` | 无 |\n",
    )

    assert html.count("<th>") == 4
    assert html.count("<td>") == 4
    assert (
        "<td>有序条目 <code>[{name, kind: blob|tree, hash, size, mode?}]"
        + INLINE_COPY + "</code>，表示目录</td>"
        in html
    )
    assert f"<td>git tree / Bazel <code>Directory{INLINE_COPY}</code></td>" in html


def test_table_cell_br_tag_renders_line_break_and_other_html_stays_text():
    html = render_markdown(
        "| 步骤 | 说明 |\n"
        "|---|---|\n"
        "| 1 | 先安装<br>再启动<br/>最后验证<BR />完成 |\n"
        "| 2 | 代码里的 `<br>` 与转义 \\<br> 保持字面，<b>加粗</b> 不放行 |\n",
    )

    assert "<td>先安装<br>再启动<br>最后验证<br>完成</td>" in html
    assert f"<code>&lt;br&gt;{INLINE_COPY}</code>" in html
    assert "转义 &lt;br&gt; 保持字面" in html
    assert "&lt;b&gt;加粗&lt;/b&gt; 不放行" in html
    assert html.count("<br>") == 3


def test_nested_list_does_not_restart_ordered_numbering():
    html = render_markdown(
        "1. 第一步\n"
        "2. 第二步\n"
        "   - 子项 A\n"
        "     - 更深一层\n"
        "   - 子项 B\n"
        "3. 第三步\n",
    )

    # 子列表嵌进第二项，整段仍是一个 <ol>，第三项不会回到 1
    assert html.count("<ol>") == 1
    assert "<li>第二步<ul><li>子项 A<ul><li>更深一层</li></ul></li>" in html
    assert "<li>第三步</li></ol>" in html


def test_ordered_list_keeps_explicit_start_number():
    html = render_markdown("3. 三\n4. 四\n")

    assert html.startswith('<ol start="3">')


def test_blank_line_separated_items_stay_in_one_ordered_list():
    html = render_markdown("1. 一\n2. 二\n\n3. 三\n")

    assert html.count("<ol") == 1
    assert html.count("<li>") == 3


def test_switching_marker_type_starts_a_new_list():
    html = render_markdown("- 甲\n1. 乙\n")

    assert "<ul><li>甲</li></ul>" in html
    assert "<ol><li>乙</li></ol>" in html


def test_list_item_carries_indented_block_content():
    html = render_markdown("- 第一项\n\n  ```py\n  x = 1\n  ```\n- 第二项\n")

    assert (f'<div class="markdown-code">{BLOCK_COPY}'
            '<pre><code class="language-py">x = 1</code></pre></div></li>') in html


def test_mermaid_fence_becomes_diagram_container_keeping_source():
    html = render_markdown(
        "```mermaid\nflowchart LR\n  A[\"甲<br/>乙\"] --> B\n```\n\n```text\nplain\n```\n",
    )

    assert ('<div class="markdown-diagram" data-diagram="mermaid">'
            '<button type="button" class="markdown-copy" title="复制源码"></button>') in html
    assert ('<pre class="markdown-diagram-source"><code class="language-mermaid">'
            "flowchart LR\n  A[&quot;甲&lt;br/&gt;乙&quot;] --&gt; B</code></pre></div>") in html
    assert (f'<div class="markdown-code">{BLOCK_COPY}'
            '<pre><code class="language-text">plain</code></pre></div>') in html


def test_code_blocks_and_inline_code_carry_copy_buttons():
    """围栏代码块与行内代码都带复制按钮:块级常显在容器角上,行内默认隐藏、悬停才出现;
    点击由 document 捕获阶段统一委托,所有 Markdown 宿主自动生效。"""
    html = render_markdown("先运行 `mc serve` 启动\n\n```sh\nmc serve --port 8321\n```\n")

    assert f"<code>mc serve{INLINE_COPY}</code>" in html
    assert (f'<div class="markdown-code">{BLOCK_COPY}'
            '<pre><code class="language-sh">mc serve --port 8321</code></pre></div>') in html

    js = MARKDOWN_JS.read_text(encoding="utf-8")
    ui = (ROOT / "missioncrew" / "web" / "js" / "ui.js").read_text(encoding="utf-8")
    css = (ROOT / "missioncrew" / "web" / "css" / "app.css").read_text(encoding="utf-8")
    assert 'document.addEventListener("click", event => {' in js
    assert 'event.target.closest?.(".markdown-copy")' in js
    assert "copyTextToClipboard(markdownCopySource(button))" in js
    # 局域网 http 不是安全上下文,navigator.clipboard 不存在时退回 execCommand
    assert 'document.execCommand("copy")' in ui
    assert ".markdown-body code > .markdown-copy { visibility: hidden" in css
    assert ".markdown-body code:hover > .markdown-copy" in css
    # 折行的行内代码作包含块时左右缘可能颠倒,按钮必须停在文本末尾的静态位置,不能设偏移量
    inline_rule = re.search(r"\.markdown-body code > \.markdown-copy \{([^}]*)\}", css).group(1)
    assert "display: inline-flex" in inline_rule
    assert not re.search(r"(^|[\s;])(top|right|bottom|left|inset):", inline_rule)
    assert ".markdown-code { position: relative" in css


def test_fence_info_string_with_code_citation_keeps_blocks_in_place():
    """Agent 常写 ```14:29:src/a.ts 这类「起行:止行:路径」信息串。开栏行不能退化成正文,
    否则之后每个闭栏都被当成开栏,围栏内外整体颠倒;路径与行号显示成代码块标题条。"""
    html = render_markdown(
        "原文:\n\n```14:14:test/spec.ts\nconst a = 1;\n```\n\n中间的说明\n\n"
        "```3:5:src/x.py\ndef f():\n    pass\n```\n\n```python title=\"x.py\"\nprint(1)\n```\n",
    )

    assert ('<div class="markdown-code"><div class="markdown-code-title">test/spec.ts:14</div>'
            f'{BLOCK_COPY}<pre><code>const a = 1;</code></pre></div>') in html
    assert '<div class="markdown-code-title">src/x.py:3-5</div>' in html
    assert "<p>中间的说明</p>" in html
    assert '<pre><code class="language-python">print(1)</code></pre>' in html
    assert "```" not in html


def test_longer_fence_keeps_nested_fences_as_source():
    """````markdown 包住一份含 ``` 代码块的文档时,内层围栏是源码的一部分,只有同样长的闭栏
    才结束外层;一行里首尾都是 ``` 的是行内写法,不是开栏。"""
    html = render_markdown(
        "````markdown\n# 标题\n\n```sh\nls\n```\n````\n\n之后 ```inline``` 照常\n",
    )

    assert ('<pre><code class="language-markdown"># 标题\n\n```sh\nls\n```</code></pre>') in html
    assert "<h1" not in html
    assert html.count('<div class="markdown-code">') == 1
    assert "<p>之后 " in html


def test_backslash_escapes_render_literal_punctuation_without_breaking_links():
    """CommonMark 反斜杠转义:标点按字面输出且不再参与标记匹配。Agent 常把 Issue 标题
    里的 [Bug] 写成 \\[Bug\\] 放进链接文字,整条链接不能因此退化成纯文本。"""
    html = render_markdown(
        "已提 Issue:[#85 \\[Bug\\]: 卡片对用户没有价值](https://example.com/issues/85)(已打标签)\n\n"
        "\\*不是强调\\* 与 \\_也不是\\_,反斜杠本身 \\\\,尖括号 \\<b\\>,"
        "代码里的 `\\[` 原样保留,\\`不是代码\\`,非标点 C:\\Users 不变\n",
    )

    assert ('<a href="https://example.com/issues/85" target="_blank" rel="noopener noreferrer">'
            "#85 [Bug]: 卡片对用户没有价值</a>(已打标签)") in html
    assert "*不是强调* 与 _也不是_,反斜杠本身 \\,尖括号 &lt;b&gt;," in html
    assert f"<code>\\[{INLINE_COPY}</code>" in html
    assert "`不是代码`" in html and html.count("<code>") == 1
    assert "C:\\Users 不变" in html
    assert "<em>" not in html and "<strong>" not in html


def test_backslash_escapes_inside_link_target_and_image_alt_are_restored():
    """链接目标里的 \\( \\) 与图片 alt 里的转义都还原成原字符,不能把占位符写进属性。"""
    html = render_markdown(
        "[维基](https://example.com/wiki/Foo_\\(bar\\)) 与 "
        "![图 \\[1\\]](https://example.com/a.png)\n",
    )

    assert '<a href="https://example.com/wiki/Foo_(bar)" target="_blank"' in html
    assert 'alt="图 [1]"' in html
    assert "\uE002" not in html and "\uE003" not in html


def test_diagram_renderer_and_vendored_mermaid_are_served():
    """图表渲染库随仓库落库、按需加载,页面不依赖外网 CDN。"""
    client = TestClient(create_app())
    html = client.get("/").text
    diagrams = client.get("/assets/js/diagrams.js").text
    css = client.get("/assets/css/app.css").text
    vendor = client.get("/assets/vendor/mermaid.min.js")
    assert "/assets/js/diagrams.js" in html
    assert 'const LIBRARY_URL = "/assets/vendor/mermaid.min.js"' in diagrams
    assert 'securityLevel: "strict"' in diagrams
    assert vendor.status_code == 200 and 'globalThis["mermaid"]' in vendor.text
    assert ".markdown-diagram-canvas" in css and "cursor: zoom-in" in css
    assert ".markdown-diagram-zoom" in css
    assert "dialog.diagram-lightbox[open] { display: flex" in css


def test_diagram_click_opens_zoomable_lightbox_and_namespaces_svg_ids():
    """渲染成功的 Mermaid 图表支持点击画布或放大按钮在弹窗中放大查看,
    支持缩放/复原/复制源码/拖动平移/点击遮罩关闭,且克隆 SVG 会重命名内部 id 避免与正文冲突。"""
    if NODE is None:
        pytest.skip("node is required to execute diagrams.js")
    diagrams_js = ROOT / "missioncrew" / "web" / "js" / "diagrams.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const source = fs.readFileSync(process.argv[1], "utf8");

// 轻量 DOM 桩:支持 diagrams.js 用到的节点遍历、选择器、克隆、事件与 dialog 生命周期
class FakeClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  add(...names) { names.forEach(n => n && this.set.add(n)); this.sync(); }
  remove(...names) { names.forEach(n => this.set.delete(n)); this.sync(); }
  contains(name) { return this.set.has(name); }
  sync() { this.el._attrs.set("class", [...this.set].join(" ")); }
}

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this._attrs = new Map();
    this.classList = new FakeClassList(this);
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.dataset = {};
    this.hidden = false;
    this.open = false;
    this.scrollLeft = 0;
    this.scrollTop = 0;
    this.clientWidth = 1000;
    this.clientHeight = 700;
    this._textContent = "";
    this._listeners = new Map();
    this._captured = new Set();
  }
  get isConnected() {
    let cur = this;
    while (cur) { if (cur === document.body || cur === document.documentElement) return true; cur = cur.parentElement; }
    return false;
  }
  get className() { return this._attrs.get("class") || ""; }
  set className(val) {
    this.classList.set = new Set(String(val || "").split(/\s+/).filter(Boolean));
    this.classList.sync();
  }
  get id() { return this._attrs.get("id") || ""; }
  set id(val) { this.setAttribute("id", val); }
  get title() { return this._attrs.get("title") || ""; }
  set title(val) { this.setAttribute("title", val); }
  get attributes() {
    return [...this._attrs.entries()].map(([name, value]) => ({ name, value }));
  }
  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent).join("");
    return this._textContent;
  }
  set textContent(val) { this.children = []; this._textContent = String(val ?? ""); }
  setAttribute(name, value) {
    const str = String(value);
    this._attrs.set(name, str);
    if (name === "class") this.classList.set = new Set(str.split(/\s+/).filter(Boolean));
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = str;
    }
  }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }
  removeAttribute(name) { this._attrs.delete(name); }
  appendChild(child) {
    if (child.parentElement) child.remove();
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  prepend(child) {
    if (child.parentElement) child.remove();
    child.parentElement = this;
    this.children.unshift(child);
  }
  remove() {
    if (!this.parentElement) return;
    const siblings = this.parentElement.children;
    const idx = siblings.indexOf(this);
    if (idx >= 0) siblings.splice(idx, 1);
    this.parentElement = null;
  }
  cloneNode(deep = false) {
    const copy = new FakeElement(this.tagName);
    for (const [k, v] of this._attrs.entries()) copy.setAttribute(k, v);
    Object.assign(copy.style, this.style);
    copy._textContent = this._textContent;
    if (deep) {
      for (const ch of this.children) copy.appendChild(ch.cloneNode(true));
    }
    return copy;
  }
  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  dispatchEvent(event) {
    event.target = event.target || this;
    event.preventDefault = event.preventDefault || (() => { event.defaultPrevented = true; });
    let cur = this;
    while (cur) {
      for (const fn of (cur._listeners.get(event.type) || [])) fn(event);
      cur = event.bubbles === false ? null : cur.parentElement;
    }
    if (event.bubbles !== false) {
      for (const fn of (document._listeners.get(event.type) || [])) fn(event);
    }
  }
  setPointerCapture(id) { this._captured.add(id); }
  hasPointerCapture(id) { return this._captured.has(id); }
  releasePointerCapture(id) { this._captured.delete(id); }
  getBoundingClientRect() {
    const w = parseFloat(this.style.width) || this.clientWidth || 400;
    const h = parseFloat(this.style.height) || this.clientHeight || 200;
    return { left: 20 - this.scrollLeft, top: 60 - this.scrollTop, width: w, height: h };
  }
  showModal() { this.open = true; }
  close() {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent({ type: "close", bubbles: false });
  }
  matches(selector) {
    return selector.split(",").some(part => matchSimple(this, part.trim()));
  }
  closest(selector) {
    let cur = this;
    while (cur) {
      if (cur.matches(selector)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    if (selector === "*") {
      const walk = node => { for (const c of node.children) { out.push(c); walk(c); } };
      walk(this);
      return out;
    }
    if (selector.startsWith(":scope > ")) {
      const rest = selector.slice(9).trim();
      const parts = rest.split(/\s+/);
      for (const c of this.children) {
        if (matchSimple(c, parts[0])) {
          if (parts.length === 1) out.push(c);
          else out.push(...c.querySelectorAll(parts.slice(1).join(" ")));
        }
      }
      return out;
    }
    const walk = node => {
      for (const c of node.children) {
        if (c.matches(selector)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  set innerHTML(html) {
    this.children = [];
    this._textContent = "";
    const text = String(html || "").trim();
    if (!text) return;
    parseFragment(text, this);
  }
}

function matchSimple(el, sel) {
  if (!sel || sel === "*") return true;
  let rest = sel;
  const tagMatch = rest.match(/^[a-zA-Z][\w-]*/);
  if (tagMatch) {
    if (el.tagName !== tagMatch[0].toUpperCase()) return false;
    rest = rest.slice(tagMatch[0].length);
  }
  const idMatch = rest.match(/#([a-zA-Z0-9_-]+)/);
  if (idMatch) {
    if (el.id !== idMatch[1]) return false;
    rest = rest.replace(idMatch[0], "");
  }
  const attrRe = /\[([^\]=]+)(?:="([^"]*)")?\]/g;
  let m;
  while ((m = attrRe.exec(rest))) {
    const val = el.getAttribute(m[1]);
    if (m[2] === undefined ? val === null : val !== m[2]) return false;
  }
  rest = rest.replace(attrRe, "");
  const clsRe = /\.([a-zA-Z0-9_-]+)/g;
  while ((m = clsRe.exec(rest))) {
    if (!el.classList.contains(m[1])) return false;
  }
  return true;
}

// 极简 HTML/SVG 片段解析器,足以解析测试中 Mermaid 输出的 SVG 与弹窗模板
function parseFragment(html, root) {
  const stack = [root];
  const tokenRe = /<(\/?)([a-zA-Z][\w:-]*)([^>]*?)(\/?)>|([^<]+)/g;
  let match;
  while ((match = tokenRe.exec(html))) {
    const parent = stack[stack.length - 1];
    if (match[5] !== undefined) {
      if (parent.children.length === 0) parent._textContent += match[5];
      continue;
    }
    const [, closing, tag, attrStr, selfClose] = match;
    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = new FakeElement(tag);
    const attrRe = /([a-zA-Z_:][\w:.-]*)(?:\s*=\s*"([^"]*)")?/g;
    let am;
    while ((am = attrRe.exec(attrStr))) {
      el.setAttribute(am[1], am[2] ?? "");
      if (am[1] === "style" && am[2]) {
        for (const decl of am[2].split(";")) {
          const [k, v] = decl.split(":").map(s => s && s.trim());
          if (k && v) {
            const camel = k.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
            el.style[camel] = v;
          }
        }
      }
    }
    parent.appendChild(el);
    if (!selfClose && !/^(img|br|hr|input)$/i.test(tag)) stack.push(el);
  }
}

const document = {
  documentElement: new FakeElement("html"),
  head: new FakeElement("head"),
  body: new FakeElement("body"),
  _listeners: new Map(),
  createElement: tag => new FakeElement(tag),
  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  },
  querySelectorAll: sel => document.body.querySelectorAll(sel),
  querySelector: sel => document.body.querySelector(sel),
};
document.documentElement.appendChild(document.body);

let copiedText = "";
let selectionState = { isCollapsed: true };
const diagramNode = document.createElement("div");
diagramNode.className = "markdown-diagram";
diagramNode.setAttribute("data-diagram", "mermaid");
const pre = document.createElement("pre");
pre.className = "markdown-diagram-source";
pre.textContent = "flowchart LR\n  A --> B";
diagramNode.appendChild(pre);
document.body.appendChild(diagramNode);

const context = {
  Element: FakeElement,
  document,
  isDarkTheme: () => false,
  matchMedia: () => ({ addEventListener: () => {} }),
  MutationObserver: class { observe() {} },
  getSelection: () => selectionState,
  copyTextToClipboard: async text => { copiedText = text; return true; },
  toast: () => {},
  setTimeout,
  clearTimeout,
  mermaid: {
    initialize: () => {},
    render: async (id, src) => {
      if (src.includes("BROKEN")) throw new Error("Parse error");
      return {
        svg: `<svg id="${id}" width="100%" viewBox="0 0 800 400" style="max-width: 800px">` +
          `<style>#${id}{color:#fff}#${id} .edge{marker-end:url(#${id}_arrow)}</style>` +
          `<defs><marker id="${id}_arrow"><path d="M0,0 L10,5 L0,10"/></marker></defs>` +
          `<path class="edge" marker-end="url(#${id}_arrow)" d="M0,0 L100,0"/></svg>`,
      };
    },
  },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context);

(async () => {
  await new Promise(r => setTimeout(r, 20));
  assert.strictEqual(diagramNode.dataset.state, "done");
  const canvas = diagramNode.querySelector(":scope > .markdown-diagram-canvas");
  const zoomBtn = diagramNode.querySelector(":scope > .markdown-diagram-zoom");
  assert(canvas && canvas.title === "点击放大查看");
  assert(zoomBtn && zoomBtn.title === "放大查看");

  // 划词选区未折叠时不弹出放大预览
  selectionState = { isCollapsed: false };
  canvas.dispatchEvent({ type: "click" });
  assert.strictEqual(document.querySelector("#diagram-dlg"), null);

  // 正常点击画布打开放大弹窗
  selectionState = { isCollapsed: true };
  canvas.dispatchEvent({ type: "click" });
  const dialog = document.querySelector("dialog");
  assert(dialog && dialog.id === "diagram-dlg" && dialog.open === true);

  // 克隆的 SVG 去掉 max-width 并重命名 id 与全部引用,避免与正文原图冲突,同时保留十六进制颜色
  const inlineSvg = canvas.querySelector("svg");
  const modalSvg = dialog.querySelector(".diagram-lightbox-canvas").querySelector("svg");
  assert(modalSvg);
  assert.notStrictEqual(modalSvg.getAttribute("id"), inlineSvg.getAttribute("id"));
  assert.strictEqual(modalSvg.style.maxWidth, "none");
  const modalMarker = modalSvg.querySelector("marker");
  assert(modalMarker.getAttribute("id").startsWith("diagram-preview-"));
  const modalStyle = modalSvg.querySelector("style").textContent;
  assert(modalStyle.includes(`#${modalSvg.getAttribute("id")}{color:#fff}`));
  assert(modalStyle.includes(`url(#${modalMarker.getAttribute("id")})`));
  const modalEdge = modalSvg.querySelector(".edge");
  assert.strictEqual(modalEdge.getAttribute("marker-end"), `url(#${modalMarker.getAttribute("id")})`);

  // 初始缩放适应窗口(stage 1000x700 减去 padding 56 => 944x644, 800x400 适应比例 944/800 = 118%)
  const scaleLabel = dialog.querySelector(".diagram-lightbox-scale");
  assert.strictEqual(scaleLabel.textContent, "118%");

  // 放大、1:1、适应按钮生效
  dialog.querySelector('[data-action="zoom-in"]').dispatchEvent({ type: "click" });
  assert.strictEqual(scaleLabel.textContent, "148%");
  dialog.querySelector('[data-action="reset"]').dispatchEvent({ type: "click" });
  assert.strictEqual(scaleLabel.textContent, "100%");
  assert.strictEqual(modalSvg.style.width, "800px");
  assert.strictEqual(modalSvg.style.height, "400px");

  // 滚轮缩放生效
  const stage = dialog.querySelector(".diagram-lightbox-stage");
  stage.dispatchEvent({ type: "wheel", deltaY: -100, clientX: 500, clientY: 300 });
  assert.strictEqual(scaleLabel.textContent, "115%");

  // 快捷键 0 恢复适应窗口, + 放大
  dialog.dispatchEvent({ type: "keydown", key: "0" });
  assert.strictEqual(scaleLabel.textContent, "118%");
  dialog.dispatchEvent({ type: "keydown", key: "+" });
  assert.strictEqual(scaleLabel.textContent, "148%");

  // 复制源码按钮复制原始 Mermaid 源码
  dialog.querySelector('[data-action="copy"]').dispatchEvent({ type: "click" });
  await new Promise(r => setTimeout(r, 10));
  assert.strictEqual(copiedText, "flowchart LR\n  A --> B");

  // 拖动平移后若浏览器把 click 落到 dialog 自身,不应误关弹窗
  const startLeft = stage.scrollLeft;
  const startTop = stage.scrollTop;
  stage.dispatchEvent({ type: "pointerdown", button: 0, pointerId: 1, clientX: 200, clientY: 200 });
  stage.dispatchEvent({ type: "pointermove", pointerId: 1, clientX: 150, clientY: 160 });
  assert.strictEqual(stage.scrollLeft, startLeft + 50);
  assert.strictEqual(stage.scrollTop, startTop + 40);
  stage.dispatchEvent({ type: "pointerup", pointerId: 1 });
  dialog.dispatchEvent({ type: "click", target: dialog, bubbles: false });
  assert.strictEqual(dialog.open, true);

  // 等待拖动抑制解除后,点击遮罩(dialog 自身)可关闭弹窗
  await new Promise(r => setTimeout(r, 10));
  dialog.dispatchEvent({ type: "click", target: dialog, bubbles: false });
  assert.strictEqual(dialog.open, false);

  // 点击角上放大按钮同样打开弹窗,点击关闭按钮关闭
  zoomBtn.dispatchEvent({ type: "click" });
  assert.strictEqual(dialog.open, true);
  dialog.querySelector('[data-action="close"]').dispatchEvent({ type: "click" });
  assert.strictEqual(dialog.open, false);
})().catch(err => { console.error(err); process.exit(1); });
"""
    result = subprocess.run(
        [NODE, "-e", script, str(diagrams_js)],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
