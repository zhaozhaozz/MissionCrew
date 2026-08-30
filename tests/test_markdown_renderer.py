import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_JS = ROOT / "missioncrew" / "web" / "js" / "markdown.js"
NODE = shutil.which("node")


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
    assert "<code>results.json</code>" in html
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
        "<td>有序条目 <code>[{name, kind: blob|tree, hash, size, mode?}]</code>，表示目录</td>"
        in html
    )
    assert "<td>git tree / Bazel <code>Directory</code></td>" in html


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

    assert '<pre><code class="language-py">x = 1</code></pre></li>' in html
