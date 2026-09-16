"""Antigravity CLI 的检测、模型发现与升级声明;执行在原生 provider 中。"""
import re
import subprocess

from .spec import CliSpec


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
    for line in proc.stdout.splitlines():
        slug, separator, label = line.strip().partition("\t")
        if separator and label.strip() and re.fullmatch(r"[\w./:-]+", slug):
            if slug not in models:
                models.append(slug)
    return models, {}


SPEC = CliSpec(
    adapter="antigravity",
    binary="agy",
    capabilities=("coding", "reasoning"),
    cost_per_run=4.0,
    update={"self_update": ["agy", "update"]},
    discover_models=discover_models,
)
