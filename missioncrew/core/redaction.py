"""凭据值脱敏:平台知道每份凭据的明文,写入消息、过程日志等记录前按原文替换。

目标只是挡住无意泄漏(Agent 把值打印进命令输出、写进回复或文档);被拆开或
重新编码过的值认不出来,这不是针对恶意 Agent 的防线。
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import quote

# 太短的值(如 4 位 PIN)在普通文本里撞车的概率高,替换会误伤正文,不做脱敏
MIN_REDACT_LENGTH = 8


def credential_placeholder(name: str) -> str:
    return f"[credential:{name}]"


def redaction_pairs(credentials: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """(凭据名, 值) -> 按原文长度降序的 (原文, 占位符) 列表。

    同时覆盖 URL 编码形式:值被拼进 URL(如 https://user:<token>@host)时常被编码。
    长的先替换,避免一个值是另一个值的子串时只替换掉一半。
    """
    pairs: dict[str, str] = {}
    for name, value in credentials:
        if len(value) < MIN_REDACT_LENGTH:
            continue
        for variant in (value, quote(value, safe="")):
            pairs.setdefault(variant, credential_placeholder(name))
    return sorted(pairs.items(), key=lambda item: -len(item[0]))


def redact_text(text: str, pairs: list[tuple[str, str]]) -> str:
    for secret, placeholder in pairs:
        if secret in text:
            text = text.replace(secret, placeholder)
    return text
