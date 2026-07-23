"""纯文本差异工具：版本比较共用的解码、unified diff 与行级操作。"""
from __future__ import annotations

import difflib
import unicodedata


class _NonTextDocumentError(Exception):
    pass


def _decode_pure_text(content: bytes) -> str:
    """严格识别可比较文本，拒绝非 UTF-8 和二进制控制字符。"""
    text = content.decode("utf-8")
    if any(unicodedata.category(char) == "Cc" and char not in "\t\n\r"
           for char in text):
        raise _NonTextDocumentError
    return text


def build_text_diff(before: str, after: str, label_a: str, label_b: str) -> dict:
    """构建 unified diff，并统计增删行；纯换行差异有兜底说明。"""
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=label_a, tofile=label_b,
        lineterm="",
    ))
    identical = before == after
    if not identical and not lines:
        # splitlines() 会忽略文件末换行和 CRLF/LF 差异，但这些仍是文本变更。
        lines = [
            f"--- {label_a}",
            f"+++ {label_b}",
            "@@ line endings @@",
            "-A 版本的换行编码或文件末换行状态",
            "+B 版本的换行编码或文件末换行状态",
        ]
    additions = sum(line.startswith("+") and not line.startswith("+++")
                    for line in lines)
    deletions = sum(line.startswith("-") and not line.startswith("---")
                    for line in lines)
    return {
        "additions": additions,
        "deletions": deletions,
        "identical": identical,
        "diff": "\n".join(lines),
    }


def build_diff_ops(before: str, after: str) -> list[dict]:
    """按行返回 SequenceMatcher 操作序列，供前端做并排差异渲染。"""
    a_lines = before.splitlines()
    b_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(None, a_lines, b_lines)
    return [{"tag": tag, "a": a_lines[a1:a2], "b": b_lines[b1:b2]}
            for tag, a1, a2, b1, b2 in matcher.get_opcodes()]
