"""Codex CLI 的静态声明。

执行走原生 provider(runtime/codex.py 的 CodexRuntimeProvider,effort 档位
也在那里声明);这里负责检测、print 模式回退模板、模型发现与升级渠道。
"""
import json
import subprocess
from pathlib import Path

from .spec import CliSpec


def _parse_models(raw: str) -> list[str]:
    """`codex debug models --bundled` 输出 JSON,取 visibility=list 的 slug。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(m.get("slug", "")) for m in data.get("models", [])
            if m.get("slug") and m.get("visibility") != "hide"]


def discover_models(backend, timeout: int = 25):
    """枚举子命令走 CLI 自带的 bundled 目录;失败返回空。"""
    binary = Path(backend.binary_path).name if backend.binary_path else "codex"
    try:
        proc = subprocess.run([binary, "debug", "models", "--bundled"],
                              capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return [], {}
    return _parse_models(proc.stdout), {}


def apply_permissions(command: list[str], filesystem: str) -> list[str]:
    """统一文件系统权限映射到 --sandbox 档位。"""
    if "--sandbox" not in command:
        return command
    result = list(command)
    index = result.index("--sandbox") + 1
    if index < len(result):
        result[index] = {"read-only": "read-only",
                         "workspace-write": "workspace-write",
                         "full-access": "danger-full-access"}[filesystem]
    return result


def session_args(cmd: list[str], session_id: str,
                 reused: bool) -> tuple[list[str], bool]:
    if not reused:
        return cmd, False
    # resume 子命令不接受 exec 的局部参数;把安全/目录/模型选项放到
    # codex 全局参数区,再调用 `exec resume ID PROMPT`。
    return [cmd[0], *cmd[2:-1], "exec", "resume", session_id, cmd[-1]], False


SPEC = CliSpec(
    adapter="codex",
    binary="codex",
    capabilities=("coding", "reasoning", "review", "security"),
    tier="standard",
    cost_per_run=5.0,
    command=("codex", "exec", "--sandbox", "workspace-write", "--add-dir",
             "{allowed_dirs}", "-m", "{model}",
             "-c", "model_reasoning_effort={effort}", "{prompt}"),
    session_id="captured",
    update={"npm": "@openai/codex"},
    discover_models=discover_models,
    apply_permissions=apply_permissions,
    session_args=session_args,
)
