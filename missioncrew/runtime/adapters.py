"""执行后端适配器。

适配器只负责启动一次 Channel 协作执行并等待结束，回复取自 Runtime 输出。

本地 Agent CLI 支持矩阵(参考 Multica 的本地 agent 列表):
- 打印模式:claude、codex、opencode、copilot、cursor-agent、codebuddy、pi
  (命令行直接传 prompt,{prompt}/{model} 占位符渲染)
- ACP stdio 协议:grok、kimi、kiro、qoder、trae(CLI 作为 JSON-RPC 服务挂在
  stdio 上,见 acp.py)

各工具的静态声明(检测、命令模板、能力、模型、升级渠道)按工具拆在 clis/
子包里,一个工具一个模块;本模块把声明汇总成下方的注册表并提供统一执行器。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import acp
from . import clis as _clis
from .base import RuntimeInstance, host_isolated_environ
from .clis.claude import MODEL_CATALOG as CLAUDE_MODEL_CATALOG
from ..core.config import pi_vendor_bin, pi_vendor_prefix
from ..core.models import Backend, ExecutionConfig, RunResult

_CLI_SESSION_LOCKS: dict[str, threading.Lock] = {}
_CLI_SESSION_LOCKS_GUARD = threading.Lock()
@dataclass
class _ActiveProcess:
    backend_id: str
    adapter: str
    session_key: str
    process: subprocess.Popen
    workdir: str
    task_id: str
    stage_name: str
    project_id: str
    role_id: str
    model: str
    executable: str
    started_at: float


_ACTIVE_PROCESSES: dict[int, _ActiveProcess] = {}
_ACTIVE_PROCESSES_GUARD = threading.Lock()

# 能够可靠恢复原生会话的打印模式 Runtime(pi 走原生 RPC provider,不在此列)。
_FIXED_ID_SESSIONS = {s.adapter for s in _clis.SPECS if s.session_id == "fixed"}
_CAPTURED_ID_SESSIONS = {s.adapter for s in _clis.SPECS
                         if s.session_id == "captured"}
_CLI_SESSION_ADAPTERS = _FIXED_ID_SESSIONS | _CAPTURED_ID_SESSIONS


def _named_session_lock(key: str) -> threading.Lock:
    with _CLI_SESSION_LOCKS_GUARD:
        return _CLI_SESSION_LOCKS.setdefault(key, threading.Lock())


def _track_process(cfg: ExecutionConfig, proc: subprocess.Popen,
                   command: Optional[list[str]] = None) -> None:
    with _ACTIVE_PROCESSES_GUARD:
        executable = Path((command or [cfg.backend.binary_path or
                                       cfg.backend.adapter])[0]).name
        _ACTIVE_PROCESSES[id(proc)] = _ActiveProcess(
            backend_id=cfg.backend.id, adapter=cfg.backend.adapter,
            session_key=cfg.session_key, process=proc,
            workdir=str(Path(cfg.workdir).expanduser().resolve()),
            task_id=cfg.task_id, stage_name=cfg.stage_name,
            project_id=cfg.project_id, role_id=cfg.role_id,
            model=cfg.backend.model, executable=executable,
            started_at=time.time(),
        )


def _untrack_process(proc: subprocess.Popen) -> None:
    with _ACTIVE_PROCESSES_GUARD:
        _ACTIVE_PROCESSES.pop(id(proc), None)


def active_execution_instances(backend_id: str = "") -> list[RuntimeInstance]:
    """返回当前仍在运行的一次性 CLI 子进程快照。"""
    with _ACTIVE_PROCESSES_GUARD:
        for key, active in list(_ACTIVE_PROCESSES.items()):
            if active.process.poll() is not None:
                _ACTIVE_PROCESSES.pop(key, None)
        active_items = list(_ACTIVE_PROCESSES.values())
    return [RuntimeInstance(
        instance_id=f"cli:{active.process.pid}",
        backend_id=active.backend_id, adapter=active.adapter,
        mode="one_shot", transport="cli-command", state="running",
        pid=active.process.pid, session_key=active.session_key,
        workdir=active.workdir, task_id=active.task_id,
        stage_name=active.stage_name, model=active.model,
        project_id=active.project_id, role_id=active.role_id,
        executable=active.executable, started_at=active.started_at,
        last_activity=active.started_at,
    ) for active in active_items
        if not backend_id or active.backend_id == backend_id]


def stop_active_executions(backend_id: str, session_key: str = "") -> int:
    """停止指定 Runtime 的活动 CLI 进程；由统一 Runtime manager 调用。"""
    targets: list[subprocess.Popen] = []
    with _ACTIVE_PROCESSES_GUARD:
        for key, active in list(_ACTIVE_PROCESSES.items()):
            if active.process.poll() is not None:
                _ACTIVE_PROCESSES.pop(key, None)
                continue
            if active.backend_id == backend_id and (
                    not session_key or active.session_key == session_key):
                targets.append(active.process)
                _ACTIVE_PROCESSES.pop(key, None)
    for proc in targets:
        _kill_process_group(proc)
    return len(targets)


def close_active_executions() -> None:
    """服务退出时清理仍在运行的打印模式子进程。"""
    with _ACTIVE_PROCESSES_GUARD:
        targets = [active.process for active in _ACTIVE_PROCESSES.values()
                   if active.process.poll() is None]
        _ACTIVE_PROCESSES.clear()
    for proc in targets:
        _kill_process_group(proc)


atexit.register(close_active_executions)


# 注入模式定义见 core.models.INJECTION_FULL_MODES;这里是用户可见标签。
_INJECTION_LABELS = {
    "first": "完整注入(新会话)",
    "recovery": "完整注入(新建/恢复,附最近对话)",
    "update": "完整注入(版本更新)",
    "reinject": "完整注入(压缩或计数触发重注入)",
    "lean": "增量回合(版本未变,沿用会话内公共上下文)",
}

LEAN_TURN_TEMPLATE = (
    "# MissionCrew 增量回合\n"
    "持久公共上下文版本 {context_version} 未变化,本轮不重发;会话中已注入的"
    "公共上下文(角色、项目、权限、协作规则)继续有效。\n\n"
)

_REINJECT_BANNER = (
    "# MissionCrew 公共上下文重注入\n"
    "距上次完整注入较久或会话历史发生过压缩,现重发完整公共上下文;"
    "以本区块为准刷新角色、项目、权限与协作规则。\n\n"
)

_UPDATE_BANNER = (
    "# MissionCrew 公共上下文更新\n"
    "本轮公共上下文版本已经变化。立即以新版本完整替换会话中的旧版本，"
    "不要继续引用旧项目设置。\n\n"
)


def _injection_mode(cfg: ExecutionConfig, recovery: bool) -> str:
    if not cfg.common_prompt:
        return "raw"
    if recovery:
        return "recovery"
    if not cfg.session_id:
        return "first"
    if cfg.context_changed:
        return "update"
    if cfg.reinject_due:
        return "reinject"
    return "lean"


def _session_input(cfg: ExecutionConfig, recovery: bool,
                   announce: bool = True) -> tuple[str, str]:
    """按注入模式组装本轮输入,返回 (输入文本, 模式)。

    完整模式重发 common_prompt;lean 增量回合只发版本引用头 + turn_prompt,
    依赖原生会话中已注入且未被压缩的公共上下文(压缩由各 Runtime 事件或
    计数兜底触发重注入)。announce=False 供调用方推迟到实际发送时再上报。
    """
    mode = _injection_mode(cfg, recovery)
    if mode == "raw":
        return cfg.prompt, mode
    if announce and cfg.emit is not None:
        try:
            cfg.emit("status", f"公共上下文:{_INJECTION_LABELS[mode]}\n")
        except Exception:
            pass
    dynamic = cfg.recovery_prompt if recovery else cfg.turn_prompt
    if mode == "lean":
        return (LEAN_TURN_TEMPLATE.format(context_version=cfg.context_version)
                + dynamic), mode
    banner = {"update": _UPDATE_BANNER, "reinject": _REINJECT_BANNER}.get(mode, "")
    return cfg.common_prompt + "\n" + banner + dynamic, mode


def _turn_bytes(*texts: str) -> int:
    """粗略统计一轮输入/输出体积(utf-8 字节),供重注入计数,不求精确。"""
    return sum(len(t.encode("utf-8", "ignore")) for t in texts if t)


def _mark_compact(cfg: ExecutionConfig) -> None:
    """Runtime 报告上下文压缩:立即持久化重注入标记,下一轮重发完整上下文。"""
    cfg.compact_detected = True
    if cfg.mark_reinject:
        try:
            cfg.mark_reinject()
        except Exception:
            pass


def _save_session(cfg: ExecutionConfig, session_id: str,
                  turn_mode: str = "", turn_bytes: int = 0) -> None:
    """持久化会话 id;轮末调用附带注入模式与体积以维护重注入计数。

    turn_mode 为空表示轮内的 id 刷新(如连接事件),不影响计数。
    """
    if cfg.save_session:
        try:
            cfg.save_session(session_id, cfg.context_version,
                             turn_mode=turn_mode, turn_bytes=turn_bytes,
                             compact_seen=cfg.compact_detected)
        except Exception:
            pass


def _clear_session(cfg: ExecutionConfig) -> None:
    if cfg.save_session:
        try:
            cfg.save_session("", "")
        except Exception:
            pass


def _refresh_session(cfg: ExecutionConfig) -> None:
    """会话锁内重读持久状态，覆盖线程池排队期间形成的陈旧快照。"""
    if not cfg.load_session:
        return
    try:
        session_id, accepted_context, reinject_due = cfg.load_session()
    except Exception:
        return
    cfg.session_id = session_id
    cfg.context_changed = bool(
        session_id and accepted_context != cfg.context_version)
    cfg.reinject_due = bool(session_id) and reinject_due


def _session_missing(text: str) -> bool:
    return bool(re.search(
        r"(?:(?:session|conversation|thread).{0,40}"
        r"(?:not found|不存在|invalid|unknown)|"
        r"(?:not found|不存在|invalid|unknown).{0,40}"
        r"(?:session|conversation|thread))",
        text, re.I | re.S))


def supports_native_session(backend: Backend) -> bool:
    """返回内置 Runtime 是否能可靠复用原生会话。"""
    if backend.adapter == "mock":
        return True
    return (backend.adapter in _CLI_SESSION_ADAPTERS
            or backend.adapter in ACP_SERVE_COMMANDS)

# ---- 以下注册表全部由 clis/ 子包的按工具声明汇总而来 ----
# 工具级注释(命令模板取舍、升级渠道限制、effort 兜底口径等)见各声明模块。

# 各适配器的默认命令模板;除 prompt/model/effort 外,workdir 与
# allowed_dirs 由平台按本次项目动态渲染。
DEFAULT_COMMANDS: dict[str, list[str]] = {
    s.adapter: list(s.command) for s in _clis.SPECS if s.command}

# ACP 协议工具的固定 serve 命令(ACP 没有 {prompt} 占位符,prompt 走协议)。
ACP_SERVE_COMMANDS: dict[str, list[str]] = {
    s.adapter: list(s.acp_serve) for s in _clis.SPECS if s.acp_serve}

# 本地 CLI 检测表:binary -> (adapter, 默认能力, 默认档位, 成本估算)
KNOWN_CLIS: list[tuple] = [
    (s.binary, s.adapter, list(s.capabilities), s.tier, s.cost_per_run)
    for s in _clis.SPECS if s.binary]

# 各工具已知的模型清单(检测时自动填充,不可编辑);"" = CLI 默认模型,排在最前。
# 只记模型名:平台不跟踪单个模型的档位与成本,配额一律按工具级 cost_per_run 扣减。
KNOWN_MODELS: dict[str, list[str]] = {
    s.adapter: list(s.models) for s in _clis.SPECS if s.models}

# 内置执行器负责的适配器的 effort(推理力度)静态兜底:adapter -> 档位(低到高)。
# 只覆盖没有原生 provider 的 adapter;claude/codex/pi 的档位由各自 provider 类
# 的 effort_catalog() 声明。注入方式见命令模板的 {effort} 占位符。
EFFORT_SUPPORT: dict[str, list[str]] = {
    s.adapter: list(s.efforts) for s in _clis.SPECS if s.efforts}

# 全平台档位的规范顺序(低到高)。runtime 自报的档位顺序各家不一(grok 按高到低
# 返回),统一按这里排序后再进下拉,保证同一个下拉里方向一致;没见过的档位按
# 原顺序排在已知档位之后,不丢弃。
EFFORT_ORDER = ["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def sort_efforts(levels: list[str]) -> list[str]:
    """把 runtime 自报的档位按 EFFORT_ORDER 规范排序。"""
    known = [level for level in EFFORT_ORDER if level in levels]
    return known + [level for level in levels if level not in EFFORT_ORDER]


# 各工具的更新规格:
#   self_update — 工具自带的更新子命令(优先使用,自更新器了解自己的安装方式);
#   npm         — npm 包名,用于查询最新版本;仅当二进制确实由 npm 管理时才允许
#                 `npm install -g` 更新。各工具的渠道限制见其声明模块。
UPDATE_SPECS: dict[str, dict] = {
    s.adapter: dict(s.update) for s in _clis.SPECS if s.update}

_VERSION_RE = re.compile(r"v?\d+\.\d+[\.\d]*")


def version_tuple(v: str) -> tuple:
    """'0.144.5' -> (0, 144, 5),用于版本比较;非数字段忽略。"""
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def is_newer(latest: str, installed: str) -> bool:
    if not latest or not installed:
        return False
    return version_tuple(latest) > version_tuple(installed)


def fetch_latest_version(adapter: str, timeout: int = 8) -> str:
    """查询工具的最新发布版本(目前支持 npm registry);查不到返回空。"""
    import urllib.parse
    import urllib.request
    pkg = (UPDATE_SPECS.get(adapter) or {}).get("npm")
    if not pkg:
        return ""
    url = f"https://registry.npmjs.org/{urllib.parse.quote(pkg, safe='')}/latest"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return str(json.loads(resp.read()).get("version", ""))
    except (OSError, ValueError):
        return ""


def _npm_managed(binary_path: str) -> bool:
    """二进制是否由 npm 全局安装管理(realpath 落在 node_modules 下)。"""
    if not binary_path:
        return False
    try:
        rp = str(Path(binary_path).resolve())
    except OSError:
        rp = binary_path
    return "/node_modules/" in rp


def update_plan(backend: Backend):
    """解析该工具的更新方式:("npm", cmd) / ("self", cmd) / None(不支持)。

    npm 托管的安装优先走 npm——检查(registry 版本)与更新走同一渠道,
    避免自更新器把新版本装到别处、npm 里的旧副本继续占着 PATH;
    非 npm 安装(原生安装器/独立脚本)用工具自带的更新命令。
    """
    spec = UPDATE_SPECS.get(backend.adapter) or {}
    pkg = spec.get("npm")
    if backend.adapter == "pi":
        # vendored 安装:更新只写平台自有 vendor 目录,绝不 -g 污染全局。
        return ("npm", ["npm", "install", "--prefix", str(pi_vendor_prefix()),
                        "--no-fund", "--no-audit", f"{pkg}@latest"])
    if pkg and _npm_managed(backend.binary_path):
        return ("npm", ["npm", "install", "-g", f"{pkg}@latest"])
    if spec.get("self_update"):
        return ("self", list(spec["self_update"]))
    return None


def run_update(backend: Backend, timeout: int = 600) -> tuple[bool, str]:
    """执行更新,返回 (成功, 输出尾部)。更新命令是固定白名单,不含用户输入。"""
    plan = update_plan(backend)
    if plan is None:
        return False, "该工具不支持自动更新(安装方式未知或由宿主程序托管)"
    _, cmd = plan
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return False, f"更新命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"更新超时({timeout}s)"
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode == 0, out[-2000:]


def _cli_version(binary: str) -> str:
    """探测 CLI 版本(仿 Multica daemon 上报 cliVersion):取 --version 输出中的
    语义版本;匹配不到就留空(有些 shim 会输出安装提示等无关文本)。"""
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True,
                              text=True, timeout=8, stdin=subprocess.DEVNULL)
        m = _VERSION_RE.search((proc.stdout or proc.stderr).strip()[:200])
        return m.group(0) if m else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def detect_report(with_version: bool = True) -> list[dict]:
    """支持的工具矩阵 + 本机可用状态(仿 Multica Runtime 页):
    每项 {binary, adapter, id, installed, path, version}。
    """
    report = []
    for binary, adapter, _caps, _tier, _cost in KNOWN_CLIS:
        if adapter == "pi":
            # pi 只用平台 vendored 安装(MC_HOME/pi/vendor),不检测系统级 pi。
            vendored = pi_vendor_bin()
            path = str(vendored) if vendored.is_file() else ""
        else:
            path = shutil.which(binary) or ""
        spec = _clis.BY_ADAPTER[adapter]
        report.append({
            "binary": binary, "adapter": adapter,
            "id": spec.detect_id or adapter,
            "installed": bool(path), "path": path,
            "version": _cli_version(path) if path and with_version else "",
        })
    return report


def detect_backends(report: Optional[list[dict]] = None) -> list[Backend]:
    """按检测报告生成注册项:一个工具一条记录,自带模型清单挂在 models 下。

    档位/成本/能力作为工具级 Runtime 元数据自动填充,不按模型细分；角色在
    创建时固定 runtime/model，执行时不再进行 Task 阶段路由。
    """
    by_adapter = {a: (caps, tier, cost) for _, a, caps, tier, cost in KNOWN_CLIS}
    found = []
    for item in (report if report is not None else detect_report()):
        if not item["installed"]:
            continue
        caps, tier, cost = by_adapter[item["adapter"]]
        if item["adapter"] == "pi":
            # pi 的执行单元与平台自有 models.json 同源(provider/model)。
            from .pi import read_pi_models
            models = read_pi_models()
        else:
            models = list(KNOWN_MODELS.get(item["adapter"], []))
        found.append(Backend(
            id=item["id"], name=f"{item['binary']} (本地)", adapter=item["adapter"],
            model="", tier=tier, capabilities=caps, cost_per_run=cost,
            models=models,
            binary_path=item["path"], version=item["version"],
        ))
    return found


def render_command(template: list[str], prompt: str, model: str,
                   documents_dir: str = "", effort: str = "",
                   allowed_dirs: Optional[list[str]] = None,
                   workdir: str = "") -> list[str]:
    """渲染命令模板，支持把一个 allowed_dirs 占位符展开为重复参数。"""
    dirs = ([documents_dir] if allowed_dirs is None and documents_dir else
            list(allowed_dirs or []))
    cmd: list[str] = []
    for tok in template:
        if "{model}" in tok:
            if not model:
                if cmd and cmd[-1] in ("--model", "-m"):
                    cmd.pop()
                continue
            tok = tok.replace("{model}", model)
        if "{effort}" in tok:
            if not effort:
                if cmd and cmd[-1] in ("--effort", "--reasoning-effort",
                                       "-c", "--config"):
                    cmd.pop()
                continue
            tok = tok.replace("{effort}", effort)
        if "{documents_dir}" in tok:
            if not documents_dir:
                if cmd and cmd[-1] == "--add-dir":
                    cmd.pop()
                continue
            tok = tok.replace("{documents_dir}", documents_dir)
        if "{workdir}" in tok:
            if not workdir:
                if cmd and cmd[-1] in ("--cwd", "--dir", "-C", "--cd"):
                    cmd.pop()
                continue
            tok = tok.replace("{workdir}", workdir)
        if "{allowed_dirs}" in tok:
            if not dirs:
                if cmd and cmd[-1] == "--add-dir":
                    cmd.pop()
                continue
            if tok == "{allowed_dirs}":
                # `--add-dir {allowed_dirs}` 展开为每个目录一组参数，兼容
                # 只接受重复 flag、不接受一个 flag 后跟多个值的 CLI。
                flag = cmd.pop() if cmd and cmd[-1] == "--add-dir" else ""
                for path in dirs:
                    if flag:
                        cmd.extend([flag, path])
                    else:
                        cmd.append(path)
                continue
            for path in dirs:
                cmd.append(tok.replace("{allowed_dirs}", path))
            continue
        cmd.append(tok.replace("{prompt}", prompt))
    return cmd


def _remove_command_option(command: list[str], flag: str,
                           *, has_value: bool = False) -> list[str]:
    result: list[str] = []
    skip = False
    for token in command:
        if skip:
            skip = False
            continue
        if token == flag:
            skip = has_value
            continue
        result.append(token)
    return result


def _apply_permission_policy(command: list[str], adapter_name: str,
                             cfg: ExecutionConfig) -> list[str]:
    """把统一权限意图翻译为已知 Runtime 的原生命令参数。"""
    permissions = cfg.runtime_policy.permissions
    result = list(command)
    if permissions.approval != "auto":
        for flag, has_value in (
                ("--permission-mode", True), ("--always-approve", False),
                ("--allow-all-tools", False), ("--force", False),
                ("--yolo", False), ("--trust-all-tools", False)):
            result = _remove_command_option(result, flag, has_value=has_value)

    if adapter_name == "codex" and "--sandbox" in result:
        index = result.index("--sandbox") + 1
        if index < len(result):
            result[index] = {
                "read-only": "read-only",
                "workspace-write": "workspace-write",
                "full-access": "danger-full-access",
            }[permissions.filesystem]
    elif (permissions.filesystem == "read-only"
          and adapter_name in {"claude_code", "codebuddy"}):
        result = _remove_command_option(result, "--permission-mode", has_value=True)
        result.extend(["--permission-mode", "plan"])
    return result


def _additional_allowed_dirs(workdir: str, allowed_dirs: list[str]) -> list[str]:
    """去掉已位于主工作根内的目录，其余目录需要 Runtime 显式授权。"""
    root = Path(workdir).expanduser().resolve()
    found = []
    seen = set()
    for raw in allowed_dirs:
        path = Path(raw).expanduser().resolve()
        try:
            path.relative_to(root)
            continue
        except ValueError:
            pass
        value = str(path)
        if value not in seen:
            seen.add(value)
            found.append(value)
    return found


def _runtime_env(cfg: ExecutionConfig, adapter_name: str) -> dict:
    """构造子进程环境，并为需要配置式多目录授权的 Runtime 注入策略。"""
    workdir = str(Path(cfg.workdir).expanduser().resolve())
    env = {**host_isolated_environ(), **cfg.env, "PWD": workdir}
    env["MISSIONCREW_ALLOWED_DIRS"] = json.dumps(cfg.allowed_dirs, ensure_ascii=False)
    policy = cfg.runtime_policy
    env.setdefault("MISSIONCREW_READABLE_DIRS", json.dumps(
        policy.readable_paths, ensure_ascii=False))
    env.setdefault("MISSIONCREW_WRITABLE_DIRS", json.dumps(
        policy.writable_paths, ensure_ascii=False))
    env.setdefault("MISSIONCREW_SKILL_DIRS", json.dumps(
        policy.skill_paths, ensure_ascii=False))
    env.setdefault("MISSIONCREW_RUNTIME_PERMISSIONS", json.dumps(
        asdict(policy.permissions), ensure_ascii=False))
    if adapter_name != "opencode":
        return env

    config = {}
    try:
        parsed = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        if isinstance(parsed, dict):
            config = parsed
    except json.JSONDecodeError:
        # 无效的既有 inline 配置本就无法被 OpenCode 使用；本次生成最小有效配置。
        pass
    permission = config.get("permission")
    if isinstance(permission, dict):
        permission = dict(permission)
    elif isinstance(permission, str):
        permission = {"*": permission}
    else:
        permission = {}

    # MissionCrew 项目资源是受信任的读写工作区。OpenCode 自带的 *.env
    # 读取询问规则会覆盖普通 read=allow；在无头模式下询问会被自动拒绝，
    # 因此必须为已授权资源显式补上 read/edit 规则。外部路径仍由下方的
    # external_directory 精确白名单约束。
    permission.setdefault("read", "allow")
    permission.setdefault("edit", "allow")

    external = _additional_allowed_dirs(workdir, cfg.allowed_dirs)
    current = permission.get("external_directory")
    if isinstance(current, dict):
        rules = dict(current)
    elif isinstance(current, str):
        rules = {"*": current}
    else:
        rules = {}
    for path in external:
        rules[f"{path.rstrip('/')}/**"] = "allow"
    permission["external_directory"] = rules
    config["permission"] = permission
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
    return env


def _emit_execution_start(emit, command: list[str], prompt: str) -> None:
    """上报启动参数预览与输入；命令中的长 prompt 用占位符避免重复展示。"""
    if emit is None:
        return
    for kind, text in (("command", "$ " + shlex.join(command) + "\n"),
                       ("input", prompt)):
        try:
            emit(kind, text)
        except Exception:
            # 展示元数据失败不能阻止 runtime 启动。
            pass


def _diagnostic_log_path(cfg: ExecutionConfig, adapter_name: str) -> Path:
    """把 Runtime 诊断输出放进 harness 工作区，避免污染业务代码仓。"""
    root = Path(cfg.env.get("MISSIONCREW_WORKSPACE")
                or Path(cfg.workdir) / ".missioncrew")
    directory = root / "runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"last-output-{adapter_name}.log"


class MockAdapter:
    """确定性模拟后端：为 Channel 协作生成可级联的回复。

    触发消息中出现"请 @某角色"时，通过 cfg.agent_action 执行显式命令
    message.publish（mentions 参数）发起协作——与真实主控同一条派发通道，
    权限与预算仍由平台校验；回复正文里的 @ 只是普通文字，不会触发执行。
    """

    def run(self, cfg: ExecutionConfig) -> RunResult:
        if cfg.session_key:
            with _named_session_lock(cfg.session_key):
                if cfg.cancellation_requested():
                    return RunResult(False, "执行已停止")
                _refresh_session(cfg)
                return self._chat(cfg)
        if cfg.cancellation_requested():
            return RunResult(False, "执行已停止")
        return self._chat(cfg)

    def _chat(self, cfg: ExecutionConfig) -> RunResult:
        reused = bool(cfg.session_id)
        prompt, injection_mode = _session_input(cfg, recovery=not reused)
        session_id = cfg.session_id or f"mock:{cfg.session_key}"
        # 增量回合的输入不含角色区块,回退到公共上下文解析
        me = _role_from_prompt(prompt) or _role_from_prompt(cfg.common_prompt)
        trigger = _trigger_from_prompt(prompt)
        # 模拟运行过程事件,让事件管道可测试/可演示
        emit = cfg.emit or (lambda kind, text: None)
        emit("input", prompt)
        emit("thinking", f"[mock] 理解触发消息({len(trigger)} 字符),对照角色定位准备回复。\n")
        emit("tool", "workspace.inspect .\n")
        # 遵循触发消息中的协作指令:"请 @x ..." -> 执行显式命令派发 @x。
        # 平台照常校验权限(非主控会被拒)与协作链预算,和真实 Runtime 一致。
        asked = [m for m in re.findall(r"请\s*@([\w-]+)", trigger) if m != me]
        combo = cfg.backend.tier + (f"/effort={cfg.effort}" if cfg.effort else "")
        reply = (f"收到。我已在工作区完成相关处理(模拟执行,by {cfg.backend.id}/"
                 f"{combo})。")
        for r in dict.fromkeys(asked):
            if cfg.agent_action is None:
                reply += f"\n(无 Agent Tool 句柄,未派发 @{r})"
                continue
            try:
                cfg.agent_action("message.publish", {
                    "channel": cfg.env.get("MISSIONCREW_CHANNEL_ID", ""),
                    "content": "上面的工作已完成,交给你继续。",
                    "mentions": [r],
                })
                emit("tool", f"message.publish mentions=[{r}]\n")
                reply += f"\n已通过 message.publish 调度 @{r}。"
            except Exception as exc:
                reply += f"\n调度 @{r} 未执行: {exc}"
        # 回显触发消息中的平台控制动作块,模拟真实 Agent 按指示发出动作
        for block in re.findall(r"<missioncrew-action>.*?</missioncrew-action>",
                                trigger, re.S):
            reply += "\n" + block
        # 触发消息带 [写文档] 指令时,在执行期间写入项目文档库
        docs_dir = cfg.env.get("MISSIONCREW_DOCUMENTS_DIR")
        if docs_dir and "[写文档]" in trigger:
            Path(docs_dir, "mock-note.md").write_text(f"由 @{me} 在执行中写入。\n")
            reply += "\n已写入文档库 mock-note.md。"
        emit("text", reply)
        if cfg.session_key:
            _save_session(cfg, session_id, injection_mode,
                          _turn_bytes(prompt, reply))
        return RunResult(True, reply[:120], output=reply)


def _load_session_meta(adapter_name: str) -> Optional[dict]:
    """工具声明的 session/load 额外 _meta(如 grok 的 noReplay)。"""
    spec = _clis.BY_ADAPTER.get(adapter_name)
    return dict(spec.load_session_meta) if spec and spec.load_session_meta else None


def supports_account_usage(adapter: str) -> bool:
    """该 adapter 是否声明了账户限额探测(usage.py 有对应 probe)。"""
    spec = _clis.BY_ADAPTER.get(adapter)
    return bool(spec and spec.account_usage)


class AcpAdapter:
    """ACP stdio 协议适配器:CLI 作为 JSON-RPC 服务运行,prompt 走协议传递。"""

    def __init__(self, adapter_name: str,
                 command: Optional[list[str]] = None):
        self.adapter_name = adapter_name
        self.command = command

    def run(self, cfg: ExecutionConfig) -> RunResult:
        template = self.command or ACP_SERVE_COMMANDS.get(self.adapter_name)
        if not template:
            return RunResult(False, f"适配器 {self.adapter_name} 未配置 ACP serve 命令")
        workdir = str(Path(cfg.workdir).expanduser().resolve())
        cmd = render_command(
            template, "", cfg.backend.model,
            cfg.env.get("MISSIONCREW_DOCUMENTS_DIR", ""), cfg.effort,
            allowed_dirs=_additional_allowed_dirs(workdir, cfg.allowed_dirs),
            workdir=workdir,
        )
        if self.command is None:
            cmd = _apply_permission_policy(cmd, self.adapter_name, cfg)
        # ACP 的真实输入由协议层在确定“复用 / load / 新建恢复”后上报；这里
        # 只打印 serve 命令，避免先展示一个最终没有发送的 Prompt。
        if cfg.emit is not None:
            try:
                cfg.emit("command", "$ " + shlex.join(cmd) + "\n")
            except Exception:
                pass
        _refresh_session(cfg)
        # 实际用哪份输入(复用 or 恢复)由协议层决定,这里推迟到发送时再上报模式
        current_prompt, current_mode = _session_input(
            cfg, recovery=False, announce=False)
        recovery_prompt, recovery_mode = _session_input(
            cfg, recovery=True, announce=False)
        ok, text = acp.run_prompt(
            cmd, current_prompt, workdir, _runtime_env(cfg, self.adapter_name),
            model=cfg.backend.model, timeout=cfg.timeout, emit=cfg.emit,
            session_key=cfg.session_key, session_id=cfg.session_id,
            recovery_prompt=recovery_prompt, save_session=cfg.save_session,
            prompt_mode=current_mode, recovery_mode=recovery_mode,
            mode_labels=_INJECTION_LABELS,
            context_version=cfg.context_version, runtime_id=cfg.backend.id,
            task_id=cfg.task_id, stage_name=cfg.stage_name,
            project_id=cfg.project_id, role_id=cfg.role_id,
            cancelled=cfg.cancelled,
            load_session_meta=_load_session_meta(self.adapter_name),
            trigger_message_id=cfg.trigger_message_id,
        )
        try:
            _diagnostic_log_path(cfg, self.adapter_name).write_text(text)
        except OSError:
            pass
        return RunResult(ok, text[-300:], output=text)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """结束整个进程组并收尸(start_new_session 后组 id == 子进程 pid)。"""
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _summarize_tool_result(content) -> str:
    """tool_result 的 content 可能是字符串或内容块列表,压成一行摘要。"""
    if isinstance(content, list):
        content = " ".join(str(b.get("content") or b.get("text") or "")
                           if isinstance(b, dict) else str(b) for b in content)
    return " ".join(str(content).split())[:400]


def _claude_stream_event(line: str, emit,
                         on_compact: Optional[Callable[[], None]] = None
                         ) -> Optional[str]:
    """解析 claude stream-json 的一行事件并上报运行过程。

    事件形态(实测 claude 2.x):assistant 事件的 message.content 里是
    thinking/text/tool_use 块;user 事件携带 tool_result;result 事件的
    result 字段是最终回复文本。返回最终回复,其余情况返回 None。
    compact_boundary/microcompact_boundary 表示上下文已压缩,回调 on_compact
    触发下一轮公共上下文重注入;hook/thinking_tokens/rate_limit 等其余
    system 子事件不进过程流。
    """
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        emit("stdout", line + "\n")
        return None
    t = d.get("type")
    if t == "assistant":
        for block in (d.get("message") or {}).get("content") or []:
            bt = block.get("type")
            if bt == "thinking":
                emit("thinking", block.get("thinking", ""))
            elif bt == "text":
                emit("text", block.get("text", ""))
            elif bt == "tool_use":
                args = json.dumps(block.get("input") or {}, ensure_ascii=False)
                emit("tool", f"{block.get('name', '?')} {args[:300]}\n")
    elif t == "user":
        for block in (d.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                mark = "✗ " if block.get("is_error") else ""
                emit("tool_result",
                     f"{mark}{_summarize_tool_result(block.get('content'))}\n")
    elif t == "result":
        return str(d.get("result") or "")
    elif t == "system" and d.get("subtype") == "init":
        emit("status", f"会话启动 model={d.get('model', '')}\n")
    elif t == "system" and d.get("subtype") in ("compact_boundary",
                                                "microcompact_boundary"):
        if on_compact is not None:
            on_compact()
        emit("status", "检测到上下文压缩;下一轮将重新注入完整公共上下文\n")
    return None


class _CodexStderrParser:
    """codex exec 的 stderr 过程日志分节解析(实测 0.144 格式)。

    codex 把全部过程写 stderr、stdout 只留最终回复。stderr 依次是:
    配置头部块(banner + workdir/model/... 到第二个 "--------"),然后
    裸标记行分节:user(完整提示词回显)、thinking、exec …、codex
    (回复回显)、tokens used(数字在下一行)。分类上报:头部按状态、
    提示词回显跳过(完整输入已由 input 事件展示)、回复回显跳过(stdout
    已展示)、token 统计并入状态;识别不了的行兜底 stderr。
    """

    _MARKERS = {"user", "thinking", "codex", "tokens used"}

    def __init__(self, emit):
        self.emit = emit
        self.section = "header"

    def feed(self, line: str) -> None:
        s = line.strip()
        if s in self._MARKERS:
            self.section = s
            return
        if s.startswith("exec ") or s.startswith("tool "):
            self.section = "exec"
            self.emit("tool", s + "\n")
            return
        if self.section == "header":
            if s == "--------" or s.lower().startswith("reading additional input"):
                return
            self.emit("status", line + "\n")
        elif self.section == "user":
            pass
        elif self.section == "codex":
            pass
        elif self.section == "tokens used":
            if s:
                self.emit("status", f"tokens used: {s}\n")
                self.section = "log"
        elif self.section == "thinking":
            self.emit("thinking", line + "\n")
        elif self.section == "exec":
            self.emit("tool_result", line + "\n")
        else:
            self.emit("stderr", line + "\n")

    def close(self) -> None:
        pass


_SESSION_ID_KEYS = {
    "session_id", "sessionId", "sessionID", "thread_id", "threadId", "chatId",
}


@dataclass
class _GenericJsonState:
    """OpenCode/Cursor JSON 流的终态线索。

    OpenCode 退出码为 0 只表示 CLI 正常退出；最后一步仍为 tool-calls 时，
    必须保留工具错误并拒绝把更早的进度文本冒充最终回复。
    """

    event_index: int = 0
    last_step_reason: str = ""
    last_tool_index: int = -1
    last_tool_name: str = ""
    last_tool_target: str = ""
    last_tool_status: str = ""
    last_tool_error: str = ""
    terminal_texts: list[str] = field(default_factory=list)


def _extract_session_id(line: str) -> str:
    """从 CLI 的 JSON 事件或可读头部提取原生会话 id。"""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        match = re.search(
            r"(?:session|thread|conversation)(?:\s+id)?\s*[:=]\s*"
            r"([A-Za-z0-9][A-Za-z0-9._:-]{5,})", line, re.I)
        return match.group(1) if match else ""

    def _walk(value) -> str:
        if isinstance(value, dict):
            for key in _SESSION_ID_KEYS:
                found = value.get(key)
                if isinstance(found, str) and found:
                    return found
            for nested in value.values():
                found = _walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = _walk(nested)
                if found:
                    return found
        return ""

    return _walk(data)


def _generic_json_event(line: str, emit,
                        state: Optional[_GenericJsonState] = None) -> Optional[str]:
    """解析 OpenCode/Cursor JSON，并上报阶段级过程与明确最终回复。"""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        emit("stdout", line + "\n")
        return None
    if state is not None:
        state.event_index += 1
    kind = str(data.get("type") or data.get("event") or "")
    part = data.get("part") if isinstance(data.get("part"), dict) else {}
    part_type = str(part.get("type") or "")

    is_tool = kind in ("tool_use", "tool-use") or part_type == "tool"
    if is_tool:
        tool_name = str(part.get("tool") or data.get("tool") or "?")
        tool_state = part.get("state")
        tool_state = tool_state if isinstance(tool_state, dict) else {}
        tool_status = str(tool_state.get("status") or "")
        tool_error = str(tool_state.get("error") or "")
        tool_input = tool_state.get("input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if state is not None:
            state.last_tool_index = state.event_index
            state.terminal_texts.clear()
            state.last_tool_name = tool_name
            state.last_tool_status = tool_status
            state.last_tool_error = tool_error
            state.last_tool_target = str(
                tool_input.get("filePath") or tool_input.get("path")
                or tool_input.get("url") or "")

        args = json.dumps(tool_input, ensure_ascii=False)
        emit("tool", f"{tool_name} {args[:300]}\n")
        output = tool_error or tool_state.get("output")
        if output not in (None, ""):
            mark = "✗ " if tool_error or tool_status == "error" else ""
            emit("tool_result", f"{mark}{_summarize_tool_result(output)}\n")

    if kind in ("step_start", "step-start") or part_type in (
            "step_start", "step-start"):
        emit("status", "OpenCode 步骤开始\n")

    if kind in ("step_finish", "step-finish") or part_type in (
            "step_finish", "step-finish"):
        reason = str(part.get("reason") or data.get("reason") or "")
        if state is not None:
            state.last_step_reason = reason
        suffix = f" reason={reason}" if reason else ""
        emit("status", f"OpenCode 步骤结束{suffix}\n")

    if kind == "reasoning" or part_type == "reasoning":
        reasoning = part.get("text")
        if not isinstance(reasoning, str):
            reasoning = data.get("text") if isinstance(data.get("text"), str) else ""
        if reasoning:
            emit("thinking", reasoning + ("" if reasoning.endswith("\n") else "\n"))

    final = data.get("result") or data.get("output")
    if isinstance(final, str) and (not kind or kind in ("result", "final", "completed")):
        return final
    text = data.get("text")
    if not isinstance(text, str):
        text = part.get("text") if isinstance(part.get("text"), str) else ""
    if not text and isinstance(data.get("message"), dict):
        content = data["message"].get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                str(block.get("text", "")) for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    if text and (kind in ("text", "message", "assistant", "assistant_message")
                 or part.get("type") == "text"):
        if state is not None:
            state.terminal_texts.append(text)
        emit("text", text)
    return None


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _authorized_roots(workdir: str, allowed_dirs: list[str]) -> list[str]:
    """返回可直接交给 Runtime/主控的去重授权根目录。"""
    found: list[str] = []
    seen: set[str] = set()
    for raw in [workdir, *allowed_dirs]:
        if not raw:
            continue
        value = str(Path(raw).expanduser().resolve())
        if value not in seen:
            seen.add(value)
            found.append(value)
    return found


def _opencode_incomplete_error(
        state: _GenericJsonState, stderr_lines: list[str], *,
        workdir: str = "", allowed_dirs: Optional[list[str]] = None) -> str:
    """把 OpenCode 的非终态正常退出转换为可交给主控的失败原因。"""
    if state.last_step_reason != "tool-calls" and state.terminal_texts:
        return ""

    if state.last_step_reason == "tool-calls":
        reason = ("OpenCode 未生成最终答复：最后执行阶段仍停在工具调用"
                  "（step_finish.reason=tool-calls）")
    else:
        reason = "OpenCode 未生成可回传的最终文本"

    details: list[str] = []
    if state.last_tool_name:
        tool = f"最后工具 {state.last_tool_name}"
        if state.last_tool_target:
            tool += f"（{state.last_tool_target}）"
        if state.last_tool_error:
            tool += f"失败：{state.last_tool_error}"
        elif state.last_tool_status:
            tool += f"状态：{state.last_tool_status}"
        details.append(tool)

    permission_error = False
    for raw in reversed(stderr_lines):
        clean = _ANSI_ESCAPE_RE.sub("", raw).strip()
        if "permission requested:" in clean or "auto-rejecting" in clean:
            details.append(f"权限信息：{clean}")
            permission_error = True
            break
    if permission_error:
        roots = _authorized_roots(workdir, allowed_dirs or [])
        if roots:
            details.append("本轮已授权根目录：" + "、".join(roots))
        details.append(
            "恢复建议：仅在上述根目录中使用精确路径重试；不得改为搜索共同父目录。"
            "若材料确实位于边界外，请主控或人类把材料移入授权目录后重新派发"
        )
    return "；".join([reason, *details])


def _new_session_id() -> str:
    return str(uuid.uuid4())


def _prepare_cli_session(adapter_name: str,
                         cfg: ExecutionConfig) -> tuple[str, str, bool, str]:
    """返回 (本轮输入,原生会话 id,是否恢复既有会话,注入模式)。"""
    supported = bool(cfg.session_key and adapter_name in _CLI_SESSION_ADAPTERS)
    if not supported:
        return cfg.prompt, "", False, "raw"
    reused = bool(cfg.session_id)
    if adapter_name in _FIXED_ID_SESSIONS:
        session_id = cfg.session_id or _new_session_id()
    else:
        session_id = cfg.session_id
    prompt, mode = _session_input(cfg, recovery=not reused)
    return prompt, session_id, reused, mode


def _apply_cli_session_args(adapter_name: str, cmd: list[str], session_id: str,
                            reused: bool) -> tuple[list[str], bool]:
    """把各 CLI 不同的 create/resume 参数翻译到已渲染命令。"""
    if adapter_name == "claude_code":
        return [*cmd, "--resume" if reused else "--session-id", session_id], False
    if adapter_name == "codebuddy":
        return [*cmd, "--resume" if reused else "--session-id", session_id], False
    if adapter_name == "copilot":
        return [*cmd, "--session-id", session_id], False
    if adapter_name == "codex":
        if not reused:
            return cmd, False
        # resume 子命令不接受 exec 的局部参数；把安全/目录/模型选项放到
        # codex 全局参数区，再调用 `exec resume ID PROMPT`。
        return [cmd[0], *cmd[2:-1], "exec", "resume", session_id, cmd[-1]], False
    if adapter_name == "opencode":
        args = ["--format", "json", "--thinking"]
        if reused:
            args += ["--session", session_id]
        return [*cmd[:-1], *args, cmd[-1]], True
    if adapter_name == "cursor":
        args = ["--output-format", "json"]
        if reused:
            args += ["--resume", session_id]
        return [*cmd, *args], True
    return cmd, False


class CliAdapter:
    """通用 CLI 适配器:按命令模板在工作目录内启动真实本地 Agent。

    执行期间逐行读取输出并经 cfg.emit 实时上报:模板含 stream-json 的
    (claude)按事件解析出思考/工具/文本;其余 CLI 按原始行透传。
    """

    def __init__(self, adapter_name: str,
                 command: Optional[list[str]] = None):
        self.adapter_name = adapter_name
        self.command = command

    def run(self, cfg: ExecutionConfig) -> RunResult:
        if cfg.session_key and self.adapter_name in _CLI_SESSION_ADAPTERS:
            with _named_session_lock(cfg.session_key):
                if cfg.cancellation_requested():
                    return RunResult(False, "执行已停止")
                _refresh_session(cfg)
                return self._run(cfg)
        if cfg.cancellation_requested():
            return RunResult(False, "执行已停止")
        return self._run(cfg)

    def _run(self, cfg: ExecutionConfig) -> RunResult:
        template = self.command or DEFAULT_COMMANDS.get(self.adapter_name)
        if not template:
            return RunResult(False, f"适配器 {self.adapter_name} 未配置命令模板")
        workdir = str(Path(cfg.workdir).expanduser().resolve())
        extra_dirs = _additional_allowed_dirs(workdir, cfg.allowed_dirs)
        input_prompt, native_session_id, reused, injection_mode = \
            _prepare_cli_session(self.adapter_name, cfg)
        cmd = render_command(
            template, input_prompt, cfg.backend.model,
            cfg.env.get("MISSIONCREW_DOCUMENTS_DIR", ""), cfg.effort,
            allowed_dirs=extra_dirs, workdir=workdir,
        )
        # prompt 在打印模式下通常是一个命令行参数；命令预览用「<输入>」
        # 代替正文，正文紧随其后单独展示，避免同一份长上下文打印两遍。
        command_preview = render_command(
            template, "<输入>", cfg.backend.model,
            cfg.env.get("MISSIONCREW_DOCUMENTS_DIR", ""), cfg.effort,
            allowed_dirs=extra_dirs, workdir=workdir,
        )
        if self.command is None:
            cmd = _apply_permission_policy(cmd, self.adapter_name, cfg)
            command_preview = _apply_permission_policy(
                command_preview, self.adapter_name, cfg)
        structured_json = False
        if cfg.session_key and self.adapter_name in _CLI_SESSION_ADAPTERS:
            cmd, structured_json = _apply_cli_session_args(
                self.adapter_name, cmd, native_session_id, reused)
            command_preview, _ = _apply_cli_session_args(
                self.adapter_name, command_preview, native_session_id, reused)
        raw_emit = cfg.emit or (lambda kind, text: None)

        def emit(kind: str, text: str) -> None:
            # 过程上报失败(落库异常等)只丢事件,绝不能拖死读线程:
            # 读线程死亡会让子进程写满管道缓冲后阻塞,整次执行假死到超时
            try:
                raw_emit(kind, text)
            except Exception:
                pass

        stream_json = any("stream-json" in tok for tok in cmd)
        if cfg.cancellation_requested():
            return RunResult(False, "执行已停止")
        _emit_execution_start(raw_emit, command_preview, input_prompt)
        try:
            # start_new_session:CLI 可能派生孙进程并继承管道,结束时按
            # 进程组整体清理;errors=replace 防非法字节炸死读线程
            proc = subprocess.Popen(
                cmd, cwd=workdir, env=_runtime_env(cfg, self.adapter_name),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", bufsize=1,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            _track_process(cfg, proc, cmd)
            if cfg.cancellation_requested():
                _kill_process_group(proc)
                _untrack_process(proc)
                return RunResult(False, "执行已停止")
        except FileNotFoundError:
            return RunResult(False, f"命令不存在: {template[0]}(后端 {cfg.backend.id})")
        out_tail: deque = deque(maxlen=400)   # 原始输出尾部(诊断日志)
        err_tail: deque = deque(maxlen=200)
        out_full: list[str] = []               # 最终回复必须完整，不能只保留尾部
        err_full: list[str] = []               # stdout 为空时的完整错误通道兜底
        final_box: list[str] = []             # stream-json 的 result 最终回复
        text_acc: list[str] = []              # stream-json 的文本块(无 result 时兜底)
        generic_state = _GenericJsonState()
        captured_sessions: list[str] = []

        def parse_emit(kind, text):
            if kind == "text":
                text_acc.append(text)
            emit(kind, text)

        # codex 的 stderr 是分节的过程日志,解析后再上报;其余 CLI 原样透传
        codex_err = _CodexStderrParser(emit) if self.adapter_name == "codex" else None

        def _drain(pipe, kind):
            for raw in pipe:
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                found_session = _extract_session_id(line)
                if found_session:
                    captured_sessions.append(found_session)
                (out_tail if kind == "stdout" else err_tail).append(line)
                if kind == "stdout" and not stream_json and not structured_json:
                    out_full.append(line)
                elif kind == "stderr":
                    err_full.append(line)
                if kind == "stdout" and stream_json:
                    final = _claude_stream_event(
                        line, parse_emit, on_compact=lambda: _mark_compact(cfg))
                    if final is not None:
                        final_box.append(final)
                elif kind == "stdout" and structured_json:
                    final = _generic_json_event(line, parse_emit, generic_state)
                    if final is not None:
                        final_box.append(final)
                elif kind == "stderr" and codex_err:
                    codex_err.feed(line)
                else:
                    emit(kind, line + "\n")
            if kind == "stderr" and codex_err:
                codex_err.close()

        def _write_log():
            try:
                _diagnostic_log_path(cfg, self.adapter_name).write_text(
                    "\n".join(out_tail) + ("\n--- stderr ---\n" + "\n".join(err_tail)
                                          if err_tail else ""))
            except OSError:
                pass

        readers = [threading.Thread(target=_drain, args=(proc.stdout, "stdout"), daemon=True),
                   threading.Thread(target=_drain, args=(proc.stderr, "stderr"), daemon=True)]
        for r in readers:
            r.start()
        try:
            proc.wait(timeout=cfg.timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            _untrack_process(proc)
            for r in readers:
                r.join(timeout=3)
            _write_log()
            return RunResult(False, f"执行超时({cfg.timeout}s)")
        for r in readers:
            r.join(timeout=5)
        if any(r.is_alive() for r in readers):
            # 子进程已退出但孙进程仍握着管道:结束进程组促成 EOF,
            # 避免回复被后续输出污染、事件在 run 结束后继续增长
            _kill_process_group(proc)
            for r in readers:
                r.join(timeout=3)
        raw_out = "\n".join(out_full).strip() or "\n".join(err_full).strip()
        _write_log()
        # stream-json:回复取 result 事件;异常中断没等到 result 时退回已解析
        # 的文本块或 stderr,不把原始 JSONL 发进频道
        out = (final_box[-1].strip() if final_box else "")
        if not out and structured_json and self.adapter_name == "opencode":
            # OpenCode 可能先输出开工说明、执行多轮工具，再输出最终答复；
            # 只取最后一个工具事件之后的文本，避免旧进度污染结果。
            out = "\n".join(generic_state.terminal_texts).strip()
        elif not out and (stream_json or structured_json):
            out = "\n".join(text_acc).strip() or "\n".join(err_full).strip()
        out = out or raw_out
        completion_error = ""
        if (structured_json and self.adapter_name == "opencode"
                and not final_box):
            completion_error = _opencode_incomplete_error(
                generic_state, err_full, workdir=cfg.workdir,
                allowed_dirs=cfg.allowed_dirs)
            if completion_error:
                out = completion_error
        if proc.returncode == 0 and cfg.session_key:
            saved_id = native_session_id or (
                captured_sessions[-1] if captured_sessions else "")
            if saved_id:
                _save_session(cfg, saved_id, injection_mode,
                              _turn_bytes(input_prompt, out))
            else:
                emit("status", "Runtime 未返回可恢复的会话 id；下一轮将使用恢复上下文新建会话。\n")
        elif reused and _session_missing(out + "\n" + "\n".join(err_full)):
            _clear_session(cfg)
        _untrack_process(proc)
        success = proc.returncode == 0 and not completion_error
        summary = completion_error or out[-300:]
        return RunResult(success, summary, output=out)


def get_adapter(name: str):
    if name == "mock":
        return MockAdapter()
    if name in ACP_SERVE_COMMANDS:
        return AcpAdapter(name)
    return CliAdapter(name)


# ---- Mock 从装配后的 Prompt 中还原聊天上下文 ----


def _role_from_prompt(prompt: str) -> str:
    m = re.search(r"你是角色 @([\w-]+)", prompt)
    return m.group(1) if m else ""


def _trigger_from_prompt(prompt: str) -> str:
    m = re.search(r"# 触发消息[^\n]*\n(.*?)(?:\n# |\Z)", prompt, re.S)
    if not m:
        return ""
    raw = m.group(1).strip()
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return raw


# ---- 按 runtime 动态发现可用模型(仿 Multica 的 per-provider ListModels) ----
# claude 的静态型号目录在 clis/claude.py(CLAUDE_MODEL_CATALOG 由顶部导入)。


def _parse_codex_models(raw: str) -> list[str]:
    """`codex debug models --bundled` 输出 JSON,取 visibility=list 的 slug。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(m.get("slug", "")) for m in data.get("models", [])
            if m.get("slug") and m.get("visibility") != "hide"]


def _parse_opencode_models(raw: str) -> list[str]:
    """`opencode models` 每行一个 provider/model id;过滤日志噪声行。"""
    return [line.strip() for line in raw.splitlines()
            if line.strip() and "/" in line and " " not in line.strip()]


def list_runtime_model_catalog(
        backend: Backend, timeout: int = 25) -> tuple[list[str], dict[str, list[str]]]:
    """向 runtime 本体查询模型目录，以及每个模型自报的推理力度档位。

    - codex:`codex debug models --bundled`(JSON 目录)
    - opencode:`opencode models`(行式目录)
    - ACP 工具(grok/kimi/kiro/qoder/trae):一次性会话,session/new 返回目录
    - claude:CLI 无枚举命令,返回静态型号目录(别名见 KNOWN_MODELS);
      mock:返回工具自带清单(测试/演示)

    第二个返回值只有 ACP 工具会自报(目前只有 grok):模型 -> 档位(低到高)。
    为空表示该 runtime 说不出按模型的差异,调用方回退到 provider 声明的
    静态档位(effort_support)。查不到一律返回空目录与空档位表。
    """
    adapter = backend.adapter
    if adapter == "mock":
        return [name for name in backend.models if name], {}
    if adapter == "claude_code":
        return list(CLAUDE_MODEL_CATALOG), {}
    binary = Path(backend.binary_path).name if backend.binary_path else None
    try:
        if adapter == "codex":
            proc = subprocess.run([binary or "codex", "debug", "models", "--bundled"],
                                  capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
            return _parse_codex_models(proc.stdout), {}
        if adapter == "opencode":
            proc = subprocess.run([binary or "opencode", "models"],
                                  capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
            return _parse_opencode_models(proc.stdout), {}
    except (OSError, subprocess.TimeoutExpired):
        return [], {}
    if adapter in ACP_SERVE_COMMANDS:
        template = ACP_SERVE_COMMANDS[adapter]
        cmd = render_command(template, "", backend.model, allowed_dirs=[])
        models, efforts = acp.list_model_catalog(
            cmd, timeout=timeout, runtime_id=backend.id)
        return models, {model: sort_efforts(levels)
                        for model, levels in efforts.items()}
    return [], {}


def list_runtime_models(backend: Backend, timeout: int = 25) -> list[str]:
    """只取模型目录;需要按模型的推理力度时用 :func:`list_runtime_model_catalog`。"""
    return list_runtime_model_catalog(backend, timeout=timeout)[0]
