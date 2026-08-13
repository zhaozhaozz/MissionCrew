"""opencode CLI 的静态声明与模型发现。"""
import subprocess
from pathlib import Path

from .spec import CliSpec


def _parse_models(raw: str) -> list[str]:
    """`opencode models` 每行一个 provider/model id;过滤日志噪声行。"""
    return [line.strip() for line in raw.splitlines()
            if line.strip() and "/" in line and " " not in line.strip()]


def discover_models(backend, timeout: int = 25):
    binary = Path(backend.binary_path).name if backend.binary_path else "opencode"
    try:
        proc = subprocess.run([binary, "models"],
                              capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return [], {}
    return _parse_models(proc.stdout), {}


def prepare_env(env: dict, external_dirs: list[str]) -> dict:
    """经 OPENCODE_CONFIG_CONTENT 注入精确目录授权规则。"""
    import json

    config = {}
    try:
        parsed = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        if isinstance(parsed, dict):
            config = parsed
    except json.JSONDecodeError:
        # 无效的既有 inline 配置本就无法被 OpenCode 使用;本次生成最小有效配置。
        pass
    permission = config.get("permission")
    if isinstance(permission, dict):
        permission = dict(permission)
    elif isinstance(permission, str):
        permission = {"*": permission}
    else:
        permission = {}

    # MissionCrew 项目资源是受信任的读写工作区。OpenCode 自带的 *.env
    # 读取询问规则会覆盖普通 read=allow;在无头模式下询问会被自动拒绝,
    # 因此必须为已授权资源显式补上 read/edit 规则。外部路径仍由下方的
    # external_directory 精确白名单约束。
    permission.setdefault("read", "allow")
    permission.setdefault("edit", "allow")

    current = permission.get("external_directory")
    if isinstance(current, dict):
        rules = dict(current)
    elif isinstance(current, str):
        rules = {"*": current}
    else:
        rules = {}
    for path in external_dirs:
        rules[f"{path.rstrip('/')}/**"] = "allow"
    permission["external_directory"] = rules
    config["permission"] = permission
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
    return env


def session_args(cmd: list[str], session_id: str,
                 reused: bool) -> tuple[list[str], bool]:
    args = ["--format", "json", "--thinking"]
    if reused:
        args += ["--session", session_id]
    return [*cmd[:-1], *args, cmd[-1]], True


SPEC = CliSpec(
    adapter="opencode",
    binary="opencode",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    command=("opencode", "run", "--dir", "{workdir}",
             "--model", "{model}", "{prompt}"),
    session_id="captured",
    update={"npm": "opencode-ai", "self_update": ["opencode", "upgrade"]},
    discover_models=discover_models,
    prepare_env=prepare_env,
    session_args=session_args,
)
