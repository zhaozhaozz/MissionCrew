"""事件过滤脚本:角色登记事件回调时附带的一小段脚本,只回答"这个事件算不算命中"。

平台对每个候选事件运行一次脚本:事件 JSON(与自动化脚本读到的事件文件同形)从标准
输入给出,也写在 `$MISSIONCREW_WEBHOOK_EVENT_FILE`;退出码 0 = 命中,1 = 忽略,其他
退出码、超时或起不来都按忽略处理并记为一次出错。典型写法是一行
`jq -e '.payload.action == "merged" and .payload.number == 12'`。

脚本只能做判断,不能产生别的效果,靠两层保证:
- 不给能力:不签发 Agent Tool 令牌,环境变量里没有平台地址、凭据目录和 GitHub 身份,
  所以它调不了任何平台动作,也拿不到任何密钥。
- 关进沙箱(本机有可用的 bubblewrap 时):没有网络,整个文件系统只读,主目录与平台
  数据目录不可见,只有一个用完即删的临时目录可写。没有 bubblewrap 的机器上只有第一层,
  `filter_sandbox()` 会如实报告,登记回调的回执里也带着。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.config import mc_home

FILTER_MAX_CHARS = 8000
FILTER_TIMEOUT_SECONDS = 10
FILTER_OUTPUT_TAIL = 400         # 出错时保留的 stderr 末尾,给角色看脚本错在哪
EVENT_FILE = "event.json"
SCRIPT_FILE = "filter"
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass
class FilterResult:
    matched: bool
    exit_code: Optional[int]     # None = 超时或没能启动
    error: str = ""              # 非空 = 这次不是正常的"命中/忽略",而是脚本出了问题


_SANDBOX: Optional[str] = None
_SANDBOX_GUARD = threading.Lock()


def _bwrap_prefix(workdir: Path) -> list[str]:
    """bubblewrap 参数:根只读,/tmp、主目录、数据目录换成空的 tmpfs,不共享网络等任何
    命名空间;最后才把工作目录绑回来,它因此是唯一可写、也是唯一看得见的平台路径。"""
    hidden = ["/tmp", str(Path.home()), str(mc_home().resolve())]
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for path in dict.fromkeys(hidden):
        if Path(path).is_dir():
            args += ["--tmpfs", path]
    return args + ["--bind", str(workdir), str(workdir), "--unshare-all",
                   "--die-with-parent", "--chdir", str(workdir), "--"]


def filter_sandbox() -> str:
    """本机用哪种隔离运行过滤脚本:`bwrap`(沙箱)或 `none`(只是不给能力)。

    有 bwrap 可执行文件不等于能用(内核可能禁了非特权用户命名空间),所以实际跑
    一次 `true` 来确认,结果在进程内缓存。"""
    global _SANDBOX
    with _SANDBOX_GUARD:
        if _SANDBOX is None:
            _SANDBOX = "none"
            if shutil.which("bwrap"):
                probe = _work_root() / f"probe-{uuid.uuid4().hex}"
                probe.mkdir(parents=True, exist_ok=True)
                try:
                    done = subprocess.run(
                        [*_bwrap_prefix(probe), "true"], capture_output=True, timeout=10)
                    if done.returncode == 0:
                        _SANDBOX = "bwrap"
                except (OSError, subprocess.SubprocessError):
                    pass
                finally:
                    shutil.rmtree(probe, ignore_errors=True)
        return _SANDBOX


def _work_root() -> Path:
    return mc_home().resolve() / "tmp" / "event-filters"


def validate_filter(script) -> str:
    if not isinstance(script, str):
        raise ValueError("filter 必须是脚本文本")
    if len(script) > FILTER_MAX_CHARS:
        raise ValueError(f"filter 最多 {FILTER_MAX_CHARS} 个字符")
    return script if script.strip() else ""


def run_event_filter(script: str, event: dict) -> FilterResult:
    """对一个事件运行过滤脚本,返回是否命中。任何异常都不抛出:回调轮询不能因为
    一段写坏的脚本停下来。"""
    workdir = _work_root() / uuid.uuid4().hex
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        data = json.dumps(event, ensure_ascii=False)
        (workdir / EVENT_FILE).write_text(data, encoding="utf-8")
        script_path = workdir / SCRIPT_FILE
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o700)
        # 有 shebang 时按可执行文件运行(支持 python 等),否则交给 bash,同自动化脚本。
        # 可执行文件也经 bash 的 exec 启动:解释器不存在时 bash 以 126/127 退出,不会和
        # 沙箱程序自身的退出码 1(= 忽略)混在一起
        command = (["bash", "-c", 'exec "$0"', str(script_path)] if script.startswith("#!")
                   else ["bash", str(script_path)])
        if filter_sandbox() == "bwrap":
            command = [*_bwrap_prefix(workdir), *command]
        # 环境从零搭起,不继承服务进程的:里面没有平台地址、令牌、凭据目录或 GitHub 身份
        env = {
            "PATH": os.environ.get("PATH") or DEFAULT_PATH,
            "HOME": str(workdir), "TMPDIR": str(workdir),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "MISSIONCREW_WEBHOOK_EVENT_FILE": str(workdir / EVENT_FILE),
            "MISSIONCREW_WEBHOOK_SOURCE": str(event.get("source") or ""),
            "MISSIONCREW_WEBHOOK_PROVIDER": str(event.get("provider") or ""),
            "MISSIONCREW_WEBHOOK_EVENT": str(event.get("event") or ""),
            "MISSIONCREW_WEBHOOK_DELIVERY": str(event.get("delivery") or ""),
        }
        process = subprocess.Popen(
            command, cwd=workdir, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        try:
            stderr = process.communicate(data, timeout=FILTER_TIMEOUT_SECONDS)[1]
        except subprocess.TimeoutExpired:
            # 连同它起的子进程一起结束
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            return FilterResult(False, None, f"过滤脚本超过 {FILTER_TIMEOUT_SECONDS} 秒未结束")
        if process.returncode in (0, 1):
            return FilterResult(process.returncode == 0, process.returncode)
        tail = " ".join((stderr or "").split())[-FILTER_OUTPUT_TAIL:]
        return FilterResult(False, process.returncode,
                            f"过滤脚本退出码 {process.returncode}" + (f": {tail}" if tail else ""))
    except Exception as exc:
        return FilterResult(False, None, f"过滤脚本无法运行: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
