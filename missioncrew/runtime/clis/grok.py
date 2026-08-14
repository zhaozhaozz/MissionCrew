"""Grok Build CLI(ACP stdio)的静态声明与厂商扩展解析。

Grok 的 print 模式按无换行 token flush,通用逐行读取器无法实时消费;原生 ACP
同时提供正文、思考、工具、权限和可复用 session 生命周期,因此走 acp_serve。
"""
from .spec import CliSpec


def parse_model_efforts(block: object) -> dict[str, list[str]]:
    """从 availableModels 解析每个模型自报的推理力度档位(xAI 私有扩展)。

    grok 在每个模型的 `_meta` 里给出 `supportsReasoningEffort` 与
    `reasoningEfforts:[{value|id, label, ...}]`;只有显式声明支持的模型才入表,
    没声明的模型不入表 = 调用方回退到静态兜底档位。档位顺序按协议原样返回
    (grok 是高到低),规范化排序由 adapters 统一负责。
    """
    if not isinstance(block, list):
        return {}
    efforts: dict[str, list[str]] = {}
    for item in block:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("modelId") or item.get("model_id")
                       or item.get("id") or item.get("value") or "")
        meta = item.get("_meta")
        if not model_id or not isinstance(meta, dict):
            continue
        supports = meta.get("supportsReasoningEffort",
                            meta.get("supports_reasoning_effort"))
        levels_raw = meta.get("reasoningEfforts") or meta.get("reasoning_efforts")
        if supports is False or not isinstance(levels_raw, list):
            continue
        levels = []
        for level in levels_raw:
            value = (str(level.get("value") or level.get("id") or "")
                     if isinstance(level, dict) else str(level))
            if value and value not in levels:
                levels.append(value)
        if levels:
            efforts[model_id] = levels
    return efforts


def account_usage_probe(backend, timeout: int = 15):
    from ..usage import probe_grok_usage
    return probe_grok_usage(backend, [backend.binary_path or "grok"], timeout)


def discover_models(backend, timeout: int = 25):
    """经 ACP 发现模型；首次为空或命中单模型兜底时只重试一次。"""
    # 延迟导入避免 clis -> adapters -> clis 的模块初始化环。这里不读取
    # auth.json；第二次独立启动 CLI，让 Grok 自己完成可能的静默续期。
    from ..adapters import _list_acp_model_catalog

    first = _list_acp_model_catalog(
        backend, timeout, parse_efforts=parse_model_efforts)
    if first[0] and first[0] != ["grok-4.5"]:
        return first
    return _list_acp_model_catalog(
        backend, timeout, parse_efforts=parse_model_efforts)


SPEC = CliSpec(
    adapter="grok_build",
    binary="grok",
    detect_id="grok",
    capabilities=("coding", "reasoning", "review", "web_search", "sub_agents"),
    tier="standard",
    cost_per_run=5.0,
    # effort 进了 serve 命令,改档位会改变 acp.py 的 client signature,
    # 长驻会话按新命令重启,不会沿用旧档位
    acp_serve=("grok", "--cwd", "{workdir}", "agent",
               "--reasoning-effort", "{effort}",
               "--always-approve", "--no-leader", "stdio"),
    # 静态兜底档位是 grok-4.6 的全量;实际档位按模型动态发现
    # (list_runtime_model_catalog),因为低档模型只认子集(grok-4.5 无 xhigh),
    # 而 `grok agent` 不校验档位——越界静默回落到模型默认(xhigh + grok-4.5
    # 实测落到 high),不像 codex 那样报错,不要指望错误回流
    efforts=("low", "medium", "high", "xhigh"),
    update={"self_update": ["grok", "update"]},
    # session/load 带 noReplay:恢复会话时不回放历史,避免把旧输出当本轮
    load_session_meta={"noReplay": True},
    discover_models=discover_models,
    parse_model_efforts=parse_model_efforts,
    account_usage_probe=account_usage_probe,
)
