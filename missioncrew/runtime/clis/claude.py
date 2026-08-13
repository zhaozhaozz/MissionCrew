"""Claude Code CLI 的静态声明。

执行走原生 provider(runtime/claude.py 的 ClaudeRuntimeProvider,effort 档位
也在那里声明);这里负责检测、print 模式回退模板、模型清单与升级渠道。
"""
from .spec import CliSpec

# claude CLI 无模型枚举命令;此目录对齐 Multica 的 claudeStaticModels,列出
# `claude --model` 接受的具体型号(按系列与新旧排列)。稳定别名不在这里——
# 它们在 SPEC.models 的工具自带清单里,两份合并后才是角色可选的全集。
MODEL_CATALOG = [
    "claude-fable-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
]

def discover_models(backend, timeout: int = 25):
    """claude CLI 无枚举命令:模型发现返回静态型号目录。"""
    return list(MODEL_CATALOG), {}


def apply_permissions(command: list[str], filesystem: str) -> list[str]:
    """只读文件系统映射为 plan 模式(只分析不落盘)。"""
    from ..adapters import _remove_command_option
    if filesystem != "read-only":
        return command
    command = _remove_command_option(command, "--permission-mode", has_value=True)
    return [*command, "--permission-mode", "plan"]


def session_args(cmd: list[str], session_id: str,
                 reused: bool) -> tuple[list[str], bool]:
    return [*cmd, "--resume" if reused else "--session-id", session_id], False


SPEC = CliSpec(
    adapter="claude_code",
    binary="claude",
    detect_id="claude",
    capabilities=("coding", "reasoning", "review", "security", "multimodal",
                  "web_search", "sub_agents"),
    tier="standard",
    cost_per_run=5.0,
    # 用 stream-json 输出:逐事件拿到思考/工具调用/文本,实时上报运行过程;
    # 最终回复取 result 事件(CliAdapter 检测到 stream-json 才解析)
    command=("claude", "-p", "{prompt}", "--model", "{model}",
             "--effort", "{effort}",
             "--output-format", "stream-json", "--verbose",
             "--permission-mode", "acceptEdits", "--add-dir", "{allowed_dirs}"),
    session_id="fixed",
    # 别名由 CLI 解析到当前最新版,不会过期;带版本号的型号走 MODEL_CATALOG
    models=("", "haiku", "sonnet", "opus", "fable"),
    update={"npm": "@anthropic-ai/claude-code",
            "self_update": ["claude", "update"]},
    discover_models=discover_models,
    apply_permissions=apply_permissions,
    session_args=session_args,
)
