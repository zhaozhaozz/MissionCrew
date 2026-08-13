"""Kimi Code CLI(ACP stdio)的静态声明。"""
from .spec import CliSpec


def account_usage_probe(backend, timeout: int = 15):
    from ..usage import probe_kimi_usage
    return probe_kimi_usage(backend, timeout)


SPEC = CliSpec(
    adapter="kimi",
    binary="kimi",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    acp_serve=("kimi", "--add-dir", "{allowed_dirs}", "acp"),
    # kimi 的 PyPI 同名包与其独立安装版版本序列对不上(疑似不同产品),
    # 因此只提供自更新按钮,不做最新版比对
    update={"self_update": ["kimi", "upgrade"]},
    account_usage_probe=account_usage_probe,
)
