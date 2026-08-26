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
    # 推理力度不走命令行:`kimi acp` 在 session/new|load 应答的 configOptions
    # 里给出 category=thought_level 的 `thinking` 选项,协议层每轮经
    # session/set_config_option 在存活会话内切换,改档位不重启长驻进程;越界值
    # kimi 直接报错(-32602 Unknown thinking value)并作为本轮失败回流到频道。
    # 档位随模型变化(K3/K3-256k: low/high/max,K2.7 系列: on/high),按模型的
    # 清单由通用 ACP 探测在模型目录同一次会话里逐模型读回;这里只放 K3 的
    # 静态兜底,模型留空(CLI 默认模型)时使用
    efforts=("low", "high", "max"),
    # kimi 的 PyPI 同名包与其独立安装版版本序列对不上(疑似不同产品),
    # 因此只提供自更新按钮,不做最新版比对
    update={"self_update": ["kimi", "upgrade"]},
    account_usage_probe=account_usage_probe,
)
