"""Antigravity CLI 的检测、模型发现与升级声明;执行在原生 provider 中。"""
import re
import subprocess

from .spec import CliSpec

EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


def embedded_effort(model: str) -> str:
    """识别模型 slug 尾部已内嵌的推理力度(如 gemini-4-neon-xhigh -> xhigh)。

    agy 对带力度后缀的 --model 同时传不同的 --effort 会直接报错冲突。
    """
    base, separator, tail = (model or "").strip().lower().rpartition("-")
    if separator and base and tail in EFFORT_LEVELS:
        return tail
    return ""


def discover_models(backend, timeout: int = 25):
    from ..base import host_isolated_environ

    try:
        proc = subprocess.run(
            [backend.binary_path or "agy", "models"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, env=host_isolated_environ())
    except (OSError, subprocess.TimeoutExpired):
        return [], {}
    if proc.returncode:
        return [], {}
    # agy models 输出 slug<TAB>显示名称;不把进度提示或报错当成模型。
    models = []
    efforts: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        slug, separator, label = line.strip().partition("\t")
        if separator and label.strip() and re.fullmatch(r"[\w./:-]+", slug):
            if slug not in models:
                models.append(slug)
            level = embedded_effort(slug)
            if level:
                efforts[slug] = [level]
    return models, efforts


def private_dirs() -> list[str]:
    """Antigravity / Gemini CLI 的用户级目录(配置、skills、记忆等)。"""
    import os
    from pathlib import Path
    dirs = []
    if os.environ.get("GEMINI_CLI_HOME"):
        dirs.append(os.environ["GEMINI_CLI_HOME"])
    dirs.append(str(Path.home() / ".gemini"))
    return dirs


SPEC = CliSpec(
    adapter="antigravity",
    binary="agy",
    capabilities=("coding", "reasoning"),
    cost_per_run=4.0,
    update={"self_update": ["agy", "update"]},
    discover_models=discover_models,
    private_dirs=private_dirs,
)
