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
