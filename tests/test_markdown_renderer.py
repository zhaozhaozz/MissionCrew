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
    assert ".markdown-code { position: relative" in css


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
    vendor = client.get("/assets/vendor/mermaid.min.js")
    assert "/assets/js/diagrams.js" in html
    assert 'const LIBRARY_URL = "/assets/vendor/mermaid.min.js"' in diagrams
    assert 'securityLevel: "strict"' in diagrams
    assert vendor.status_code == 200 and 'globalThis["mermaid"]' in vendor.text
