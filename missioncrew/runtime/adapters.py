"""执行后端适配器。

适配器只负责"启动一次执行并等待结束":任务阶段的证据由引擎从工作区 manifest
读取;聊天协作的回复取自适配器输出。

本地 Agent CLI 支持矩阵(参考 Multica 的本地 agent 列表):
- 打印模式:claude、codex、grok、opencode、copilot、cursor-agent、codebuddy、pi
  (命令行直接传 prompt,{prompt}/{model} 占位符渲染)
- ACP stdio 协议:kimi、kiro、qoder、trae(CLI 作为 JSON-RPC 服务挂在
  stdio 上,见 acp.py;Backend.command 可覆盖默认的 serve 命令)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Optional

from . import acp
from ..taskflow.assembler import MANIFEST
from ..core.models import Backend, ExecutionConfig, RunResult, TIER_ORDER

# 各适配器的默认命令模板；除 prompt/model/effort 外，workdir 与
# allowed_dirs 由平台按本次项目动态渲染。
DEFAULT_COMMANDS = {
    # claude 用 stream-json 输出:逐事件拿到思考/工具调用/文本,实时上报
    # 运行过程;最终回复取 result 事件(CliAdapter 检测到 stream-json 才解析)
    "claude_code": ["claude", "-p", "{prompt}", "--model", "{model}",
                    "--effort", "{effort}",
                    "--output-format", "stream-json", "--verbose",
                    "--permission-mode", "acceptEdits", "--add-dir", "{allowed_dirs}"],
    "codex": ["codex", "exec", "--sandbox", "workspace-write", "--add-dir",
              "{allowed_dirs}", "-m", "{model}",
              "-c", "model_reasoning_effort={effort}", "{prompt}"],
    "grok_build": ["grok", "-p", "{prompt}", "--model", "{model}",
                   "--cwd", "{workdir}", "--always-approve", "--no-auto-update"],
    "opencode": ["opencode", "run", "--dir", "{workdir}",
                 "--model", "{model}", "{prompt}"],
    "copilot": ["copilot", "-p", "{prompt}", "--model", "{model}",
                "--allow-all-tools", "--add-dir={allowed_dirs}"],
    # Cursor print 模式需 --force 才会实际落盘；该 CLI 没有多根目录参数，
    # 额外资源通过绝对路径上下文访问，进程本身不设文件系统沙箱。
    "cursor": ["cursor-agent", "-p", "--force", "{prompt}", "--model", "{model}"],
    "codebuddy": ["codebuddy", "-p", "{prompt}", "--model", "{model}",
                  "--permission-mode", "acceptEdits", "--add-dir", "{allowed_dirs}"],
    "pi": ["pi", "-p", "{prompt}", "--model", "{model}"],
}

# ACP 协议工具的 serve 命令(来自 Multica 各后端的实际调用参数);
# Backend.command 可整体覆盖(ACP 命令没有 {prompt} 占位符,prompt 走协议)
ACP_SERVE_COMMANDS = {
    "kimi": ["kimi", "--add-dir", "{allowed_dirs}", "acp"],
    "kiro": ["kiro-cli", "acp", "--trust-all-tools"],
    "qoder": ["qodercli", "--add-dir", "{allowed_dirs}", "--yolo", "--acp"],
    "trae": ["traecli", "--add-dir", "{allowed_dirs}",
             "acp", "serve", "--yolo"],
}

# 本地 CLI 检测表:binary -> (adapter, 默认能力, 默认档位, 成本估算)
_CLAUDE_CAPS = ["coding", "reasoning", "review", "security", "multimodal",
                "web_search", "sub_agents"]
KNOWN_CLIS = [
    ("claude", "claude_code", _CLAUDE_CAPS, "standard", 5.0),
    ("codex", "codex", ["coding", "reasoning", "review", "security"], "standard", 5.0),
    ("grok", "grok_build", ["coding", "reasoning", "review", "web_search", "sub_agents"],
     "standard", 5.0),
    ("opencode", "opencode", ["coding", "reasoning"], "standard", 4.0),
    ("copilot", "copilot", ["coding"], "economy", 2.0),
    ("cursor-agent", "cursor", ["coding", "reasoning"], "standard", 4.0),
    ("codebuddy", "codebuddy", ["coding"], "economy", 2.0),
    ("pi", "pi", ["coding", "reasoning"], "standard", 4.0),
    # ACP stdio 协议工具
    ("kimi", "kimi", ["coding", "reasoning"], "standard", 4.0),
    ("kiro-cli", "kiro", ["coding", "reasoning"], "standard", 4.0),
    ("qodercli", "qoder", ["coding", "reasoning"], "standard", 4.0),
    ("traecli", "trae", ["coding", "reasoning"], "standard", 4.0),
]


# 各工具已知的模型阶梯(检测时自动填充,可在全局设置中编辑);
# name="" 表示 CLI 默认模型。模型属于工具,路由按 工具×模型 展开执行单元。
KNOWN_MODELS: dict[str, list[dict]] = {
    "claude_code": [
        {"name": "haiku", "tier": "economy", "cost": 1.0},
        {"name": "", "tier": "standard", "cost": 5.0},
        {"name": "opus", "tier": "expert", "cost": 20.0},
    ],
}


# 各适配器的 effort(推理力度)支持:adapter -> 允许的档位(从低到高)。
# 只有列出的适配器可在角色上配置 effort,注入方式见 DEFAULT_COMMANDS 的 {effort}:
# - claude:原生 `--effort` 标志(档位来自 `claude --help`);
# - codex:配置覆盖 `-c model_reasoning_effort=<档位>`(具体模型未必支持全部档位,
#   越界时 CLI 自行报错,错误照常回流到频道);
# - mock:仅供测试/演示走通配置链路。
EFFORT_SUPPORT: dict[str, list[str]] = {
    "claude_code": ["low", "medium", "high", "xhigh", "max"],
    "codex": ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
    "mock": ["low", "medium", "high"],
}


# 各工具的更新规格:
#   self_update — 工具自带的更新子命令(优先使用,自更新器了解自己的安装方式);
#   npm         — npm 包名,用于查询最新版本;仅当二进制确实由 npm 管理时才允许
#                 `npm install -g` 更新(copilot 常由 VS Code 扩展托管,不能乱动)。
# kimi 的 PyPI 同名包与其独立安装版版本序列对不上(疑似不同产品),
# 因此 kimi/trae 只提供自更新按钮,不做最新版比对。
UPDATE_SPECS: dict[str, dict] = {
    "claude_code": {"npm": "@anthropic-ai/claude-code", "self_update": ["claude", "update"]},
    "codex":       {"npm": "@openai/codex"},
    "grok_build":  {"self_update": ["grok", "update"]},
    "opencode":    {"npm": "opencode-ai", "self_update": ["opencode", "upgrade"]},
    "copilot":     {"npm": "@github/copilot"},
    "cursor":      {"self_update": ["cursor-agent", "update"]},
    "codebuddy":   {"npm": "@tencent-ai/codebuddy-code"},
    "kimi":        {"self_update": ["kimi", "upgrade"]},
    "trae":        {"self_update": ["traecli", "update"]},
}

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
        path = shutil.which(binary) or ""
        report.append({
            "binary": binary, "adapter": adapter,
            "id": {"claude_code": "claude", "grok_build": "grok"}.get(adapter, adapter),
            "installed": bool(path), "path": path,
            "version": _cli_version(binary) if path and with_version else "",
        })
    return report


def detect_backends(report: Optional[list[dict]] = None) -> list[Backend]:
    """按检测报告生成注册项:一个工具一条记录,模型阶梯自动挂在 models 下。

    档位/成本/能力是结构化任务的路由属性(自动填充,不在工具页配置);
    聊天角色在创建时固定 runtime/model,执行时不使用这些属性重新路由。
    """
    by_adapter = {a: (caps, tier, cost) for _, a, caps, tier, cost in KNOWN_CLIS}
    found = []
    for item in (report if report is not None else detect_report()):
        if not item["installed"]:
            continue
        caps, tier, cost = by_adapter[item["adapter"]]
        found.append(Backend(
            id=item["id"], name=f"{item['binary']} (本地)", adapter=item["adapter"],
            model="", tier=tier, capabilities=caps, cost_per_run=cost,
            models=list(KNOWN_MODELS.get(item["adapter"], [])),
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
                if cmd and cmd[-1] in ("--effort", "-c", "--config"):
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
    env = {**os.environ, **cfg.env, "PWD": workdir}
    env["MISSIONCREW_ALLOWED_DIRS"] = json.dumps(cfg.allowed_dirs, ensure_ascii=False)
    if adapter_name != "opencode":
        return env

    external = _additional_allowed_dirs(workdir, cfg.allowed_dirs)
    if not external:
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
    permission = dict(permission) if isinstance(permission, dict) else {}
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


def _append_manifest(workdir: str, entries: list[dict]) -> None:
    p = Path(workdir) / MANIFEST
    p.parent.mkdir(parents=True, exist_ok=True)
    data = []
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            data = []
    data.extend(entries)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


class MockAdapter:
    """确定性模拟后端:任务阶段产出证据文件;聊天协作生成可级联的回复。

    模拟能力边界(用于演示路由升级):
    - 任务带 hard 标签时,economy 档执行失败;
    - 任务带 very-hard 标签时,非 expert 档执行失败。
    聊天协作:触发消息中出现"请 @某角色"时,回复会包含该 @；是否触发由
    ChatEngine 按"只有项目主控可以调度"的规则决定。
    """

    def run(self, cfg: ExecutionConfig) -> RunResult:
        if "# 聊天协作请求" in cfg.prompt:
            return self._chat(cfg)
        return self._task_stage(cfg)

    def _task_stage(self, cfg: ExecutionConfig) -> RunResult:
        labels = _labels_from_prompt(cfg.prompt)
        tier_idx = TIER_ORDER.index(cfg.backend.tier)
        if "very-hard" in labels and tier_idx < TIER_ORDER.index("expert"):
            return RunResult(False, f"{cfg.backend.id} 能力不足,未能完成阶段 {cfg.stage_name}")
        if "hard" in labels and tier_idx < TIER_ORDER.index("standard"):
            return RunResult(False, f"{cfg.backend.id} 能力不足,未能完成阶段 {cfg.stage_name}")

        produces = _produces_from_prompt(cfg.prompt)
        entries = []
        for ev_type in produces:
            rel = f"evidence/{cfg.stage_name}_{ev_type}.md"
            Path(cfg.workdir, rel).write_text(
                f"# {ev_type}\n\n[mock] 阶段 {cfg.stage_name} 由 {cfg.backend.id} "
                f"(tier={cfg.backend.tier}) 产出的模拟证据。\n"
            )
            entries.append({"type": ev_type, "path": rel,
                            "summary": f"mock 生成的 {ev_type}"})
        if entries:
            _append_manifest(cfg.workdir, entries)
        return RunResult(True, f"{cfg.backend.id} 完成阶段 {cfg.stage_name},"
                               f"产出证据: {', '.join(produces) or '无'}")

    def _chat(self, cfg: ExecutionConfig) -> RunResult:
        me = _role_from_prompt(cfg.prompt)
        trigger = _trigger_from_prompt(cfg.prompt)
        # 模拟运行过程事件,让事件管道可测试/可演示
        emit = cfg.emit or (lambda kind, text: None)
        emit("thinking", f"[mock] 理解触发消息({len(trigger)} 字符),对照角色定位准备回复。\n")
        emit("tool", "workspace.inspect .\n")
        # 遵循触发消息中的协作指令:"请 @x ..." -> 回复中 @x 发起协作
        asked = [m for m in re.findall(r"请\s*@([\w-]+)", trigger) if m != me]
        combo = cfg.backend.tier + (f"/effort={cfg.effort}" if cfg.effort else "")
        reply = (f"收到。我已在工作区完成相关处理(模拟执行,by {cfg.backend.id}/"
                 f"{combo})。")
        for r in dict.fromkeys(asked):
            reply += f"\n@{r} 上面的工作已完成,交给你继续。"
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
        return RunResult(True, reply[:120], output=reply)


class AcpAdapter:
    """ACP stdio 协议适配器:CLI 作为 JSON-RPC 服务运行,prompt 走协议传递。"""

    def __init__(self, adapter_name: str):
        self.adapter_name = adapter_name

    def run(self, cfg: ExecutionConfig) -> RunResult:
        template = cfg.backend.command or ACP_SERVE_COMMANDS.get(self.adapter_name)
        if not template:
            return RunResult(False, f"适配器 {self.adapter_name} 未配置 ACP serve 命令")
        workdir = str(Path(cfg.workdir).expanduser().resolve())
        cmd = render_command(
            template, "", cfg.backend.model,
            cfg.env.get("MISSIONCREW_DOCUMENTS_DIR", ""), cfg.effort,
            allowed_dirs=_additional_allowed_dirs(workdir, cfg.allowed_dirs),
            workdir=workdir,
        )
        _emit_execution_start(cfg.emit, cmd, cfg.prompt)
        ok, text = acp.run_prompt(
            cmd, cfg.prompt, workdir, _runtime_env(cfg, self.adapter_name),
            model=cfg.backend.model, timeout=cfg.timeout, emit=cfg.emit,
        )
        try:
            Path(cfg.workdir, f".mc_last_output_{self.adapter_name}.log").write_text(text)
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


def _claude_stream_event(line: str, emit) -> Optional[str]:
    """解析 claude stream-json 的一行事件并上报运行过程。

    事件形态(实测 claude 2.x):assistant 事件的 message.content 里是
    thinking/text/tool_use 块;user 事件携带 tool_result;result 事件的
    result 字段是最终回复文本。返回最终回复,其余情况返回 None。
    hook/thinking_tokens/rate_limit 等 system 子事件不进过程流。
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


class CliAdapter:
    """通用 CLI 适配器:按命令模板在工作目录内启动真实本地 Agent。

    执行期间逐行读取输出并经 cfg.emit 实时上报:模板含 stream-json 的
    (claude)按事件解析出思考/工具/文本;其余 CLI 按原始行透传。
    """

    def __init__(self, adapter_name: str):
        self.adapter_name = adapter_name

    def run(self, cfg: ExecutionConfig) -> RunResult:
        template = cfg.backend.command or DEFAULT_COMMANDS.get(self.adapter_name)
        if not template:
            return RunResult(False, f"适配器 {self.adapter_name} 未配置命令模板"
                                    f"(ACP 类 CLI 请在 Backend.command 中配置)")
        workdir = str(Path(cfg.workdir).expanduser().resolve())
        extra_dirs = _additional_allowed_dirs(workdir, cfg.allowed_dirs)
        cmd = render_command(
            template, cfg.prompt, cfg.backend.model,
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
        raw_emit = cfg.emit or (lambda kind, text: None)

        def emit(kind: str, text: str) -> None:
            # 过程上报失败(落库异常等)只丢事件,绝不能拖死读线程:
            # 读线程死亡会让子进程写满管道缓冲后阻塞,整次执行假死到超时
            try:
                raw_emit(kind, text)
            except Exception:
                pass

        stream_json = any("stream-json" in tok for tok in cmd)
        _emit_execution_start(raw_emit, command_preview, cfg.prompt)
        try:
            # start_new_session:CLI 可能派生孙进程并继承管道,结束时按
            # 进程组整体清理;errors=replace 防非法字节炸死读线程
            proc = subprocess.Popen(
                cmd, cwd=workdir, env=_runtime_env(cfg, self.adapter_name),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", bufsize=1,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except FileNotFoundError:
            return RunResult(False, f"命令不存在: {template[0]}(后端 {cfg.backend.id})")
        out_tail: deque = deque(maxlen=400)   # 原始输出尾部(诊断日志)
        err_tail: deque = deque(maxlen=200)
        out_full: list[str] = []               # 最终回复必须完整，不能只保留尾部
        err_full: list[str] = []               # stdout 为空时的完整错误通道兜底
        final_box: list[str] = []             # stream-json 的 result 最终回复
        text_acc: list[str] = []              # stream-json 的文本块(无 result 时兜底)

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
                (out_tail if kind == "stdout" else err_tail).append(line)
                if kind == "stdout" and not stream_json:
                    out_full.append(line)
                elif kind == "stderr":
                    err_full.append(line)
                if kind == "stdout" and stream_json:
                    final = _claude_stream_event(line, parse_emit)
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
                Path(cfg.workdir, f".mc_last_output_{self.adapter_name}.log").write_text(
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
        if not out and stream_json:
            out = "\n".join(text_acc).strip() or "\n".join(err_full).strip()
        out = out or raw_out
        return RunResult(proc.returncode == 0, out[-300:], output=out)


def get_adapter(name: str):
    if name == "mock":
        return MockAdapter()
    if name in ACP_SERVE_COMMANDS:
        return AcpAdapter(name)
    return CliAdapter(name)


# ---- Mock 从装配后的 Prompt 中还原上下文(保持与真实后端相同的接口) ----

def _labels_from_prompt(prompt: str) -> list[str]:
    for line in prompt.splitlines():
        if line.startswith("类型: ") and "标签: " in line:
            return [x.strip() for x in line.split("标签: ")[1].split(",")]
    return []


def _produces_from_prompt(prompt: str) -> list[str]:
    for line in prompt.splitlines():
        if line.startswith("本阶段必须产出的证据类型: "):
            raw = line.split(": ", 1)[1]
            if raw == "无强制要求":
                return []
            return [x.strip() for x in raw.split(",")]
    return []


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

# claude CLI 无模型枚举命令:用稳定别名目录(仿 claudeStaticModels 的静态策略)
# claude CLI 无模型枚举命令;此目录对齐 Multica 的 claudeStaticModels,
# 反映 `claude --model` 实际接受的值:别名(自动跟随最新版)在前,具体型号在后。
CLAUDE_MODEL_CATALOG = [
    "haiku", "sonnet", "opus",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-6",
    "claude-sonnet-4-5",
]


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


def list_runtime_models(backend: Backend, timeout: int = 25) -> list[str]:
    """向 runtime 本体查询可用模型;查不到返回空(调用方回退到配置的阶梯)。

    - codex:`codex debug models --bundled`(JSON 目录)
    - opencode:`opencode models`(行式目录)
    - ACP 工具(kimi/kiro/qoder/trae):一次性会话,session/new 返回目录
    - claude:CLI 无枚举命令,返回静态目录(别名 + 具体型号,对齐 Multica);
      mock:返回配置阶梯(测试/演示)
    """
    adapter = backend.adapter
    if adapter == "mock":
        return [str(m.get("name", "")) for m in backend.models if m.get("name")]
    if adapter == "claude_code":
        return list(CLAUDE_MODEL_CATALOG)
    binary = Path(backend.binary_path).name if backend.binary_path else None
    try:
        if adapter == "codex":
            proc = subprocess.run([binary or "codex", "debug", "models", "--bundled"],
                                  capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
            return _parse_codex_models(proc.stdout)
        if adapter == "opencode":
            proc = subprocess.run([binary or "opencode", "models"],
                                  capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
            return _parse_opencode_models(proc.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if adapter in ACP_SERVE_COMMANDS:
        template = backend.command or ACP_SERVE_COMMANDS[adapter]
        cmd = render_command(template, "", backend.model, allowed_dirs=[])
        return acp.list_models(cmd, timeout=timeout)
    return []
