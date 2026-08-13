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
)
