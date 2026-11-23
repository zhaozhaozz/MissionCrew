"""Trae CLI(ACP stdio)的静态声明。"""
import re
import subprocess

from .spec import CliSpec

_LATEST_RE = re.compile(r"^Latest\b.*?version:\s*v?(\d+(?:\.\d+)+)", re.I | re.M)


def latest_version(timeout: int = 8) -> str:
    """`traecli update check` 向官方更新服务查询本渠道最新版本(只有文本输出)。"""
    try:
        proc = subprocess.run(["traecli", "update", "check"], capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = _LATEST_RE.search(proc.stdout or "")
    return match.group(1) if match else ""


def parse_prompt_usage(response: dict) -> dict:
    """session/prompt 响应 `_meta._trae/tokenUsage.sessionTotal` 是会话累计用量
    (字段同 ACP 标准 usage);本轮用量与上下文由协议层从标准字段取。"""
    from ..acp import ACP_USAGE_FIELDS
    from ..native import usage_section
    meta = response.get("_meta") if isinstance(response, dict) else None
    info = meta.get("_trae/tokenUsage") if isinstance(meta, dict) else None
    if not isinstance(info, dict):
        return {}
    return {"total": usage_section(info.get("sessionTotal"), ACP_USAGE_FIELDS)}


def private_dirs() -> list[str]:
    """Trae CLI 的用户级配置目录。"""
    from pathlib import Path
    return [str(Path.home() / ".trae")]


SPEC = CliSpec(
    adapter="trae",
    binary="traecli",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    # 0.209 起顶层 --add-dir 对 `acp serve` 不生效(会话 workspace_roots 只有
    # cwd);额外目录改由协议层随 session/new|load 的 additionalDirectories 传入。
    # --yolo 让会话进入 bypass_permissions 模式,不再发权限请求。
    acp_serve=("traecli", "acp", "serve", "--yolo"),
    # 推理力度不走命令行:session/new|load 的 configOptions 里有
    # category=thought_level 的 `reasoning_effort`,协议层每轮
    # session/set_config_option 切换;档位按模型不同(多数 low/high/xhigh,
    # GLM-5.2 只有 high/xhigh),部分模型(Doubao-Seed-Code、MiniMax、MiMo)
    # 没有该选项,设档位会报 -32602 Unsupported reasoning effort。按模型的
    # 清单由通用 ACP 探测逐模型读回;不放静态兜底,免得给不支持的模型
    # 也列出档位
    update={"self_update": ["traecli", "update"]},
    latest_version=latest_version,
    parse_prompt_usage=parse_prompt_usage,
    private_dirs=private_dirs,
)
