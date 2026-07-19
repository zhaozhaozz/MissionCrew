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
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from . import acp
from ..taskflow.assembler import MANIFEST
from ..core.models import Backend, ExecutionConfig, RunResult, TIER_ORDER

# 各适配器的默认命令模板,{prompt}/{model} 在运行时替换;
# model 为空时 {model} 及其前面的 --model/-m 标志会被移除。
DEFAULT_COMMANDS = {
    "claude_code": ["claude", "-p", "{prompt}", "--model", "{model}",
                    "--permission-mode", "acceptEdits", "--add-dir", "{documents_dir}"],
    "codex": ["codex", "exec", "--sandbox", "workspace-write", "--add-dir",
              "{documents_dir}", "-m", "{model}", "{prompt}"],
    "grok_build": ["grok", "-p", "{prompt}", "--model", "{model}",
                   "--always-approve", "--no-auto-update"],
    "opencode": ["opencode", "run", "--model", "{model}", "{prompt}"],
    "copilot": ["copilot", "-p", "{prompt}", "--model", "{model}", "--allow-all-tools"],
    "cursor": ["cursor-agent", "-p", "{prompt}", "--model", "{model}"],
    "codebuddy": ["codebuddy", "-p", "{prompt}", "--model", "{model}"],
    "pi": ["pi", "-p", "{prompt}", "--model", "{model}"],
}

# ACP 协议工具的 serve 命令(来自 Multica 各后端的实际调用参数);
# Backend.command 可整体覆盖(ACP 命令没有 {prompt} 占位符,prompt 走协议)
ACP_SERVE_COMMANDS = {
    "kimi": ["kimi", "acp"],
    "kiro": ["kiro-cli", "acp", "--trust-all-tools"],
    "qoder": ["qodercli", "--yolo", "--acp"],
    "trae": ["traecli", "acp", "serve", "--yolo"],
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
                   documents_dir: str = "") -> list[str]:
    """渲染命令模板；空模型/文档目录会连同紧邻的参数标志一起移除。"""
    cmd: list[str] = []
    for tok in template:
        if "{model}" in tok:
            if not model:
                if cmd and cmd[-1] in ("--model", "-m"):
                    cmd.pop()
                continue
            tok = tok.replace("{model}", model)
        if "{documents_dir}" in tok:
            if not documents_dir:
                if cmd and cmd[-1] == "--add-dir":
                    cmd.pop()
                continue
            tok = tok.replace("{documents_dir}", documents_dir)
        cmd.append(tok.replace("{prompt}", prompt))
    return cmd


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
    聊天协作:触发消息中出现"请 @某角色"时,回复会 @该角色发起协作,
    模拟真实 Agent 遵循协作指令的行为。
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
        # 遵循触发消息中的协作指令:"请 @x ..." -> 回复中 @x 发起协作
        asked = [m for m in re.findall(r"请\s*@([\w-]+)", trigger) if m != me]
        reply = (f"收到。我已在工作区完成相关处理(模拟执行,by {cfg.backend.id}/"
                 f"{cfg.backend.tier})。")
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
        return RunResult(True, reply[:120], output=reply)


class AcpAdapter:
    """ACP stdio 协议适配器:CLI 作为 JSON-RPC 服务运行,prompt 走协议传递。"""

    def __init__(self, adapter_name: str):
        self.adapter_name = adapter_name

    def run(self, cfg: ExecutionConfig) -> RunResult:
        cmd = cfg.backend.command or ACP_SERVE_COMMANDS.get(self.adapter_name)
        if not cmd:
            return RunResult(False, f"适配器 {self.adapter_name} 未配置 ACP serve 命令")
        ok, text = acp.run_prompt(
            cmd, cfg.prompt, cfg.workdir, {**os.environ, **cfg.env},
            model=cfg.backend.model, timeout=cfg.timeout,
        )
        try:
            Path(cfg.workdir, f".mc_last_output_{self.adapter_name}.log").write_text(text)
        except OSError:
            pass
        return RunResult(ok, text[-300:], output=text[-4000:])


class CliAdapter:
    """通用 CLI 适配器:按命令模板在工作目录内启动真实本地 Agent。"""

    def __init__(self, adapter_name: str):
        self.adapter_name = adapter_name

    def run(self, cfg: ExecutionConfig) -> RunResult:
        template = cfg.backend.command or DEFAULT_COMMANDS.get(self.adapter_name)
        if not template:
            return RunResult(False, f"适配器 {self.adapter_name} 未配置命令模板"
                                    f"(ACP 类 CLI 请在 Backend.command 中配置)")
        cmd = render_command(
            template, cfg.prompt, cfg.backend.model,
            cfg.env.get("MISSIONCREW_DOCUMENTS_DIR", ""),
        )
        try:
            proc = subprocess.run(
                cmd, cwd=cfg.workdir, env={**os.environ, **cfg.env},
                capture_output=True, text=True, timeout=cfg.timeout,
            )
        except FileNotFoundError:
            return RunResult(False, f"命令不存在: {template[0]}(后端 {cfg.backend.id})")
        except subprocess.TimeoutExpired:
            return RunResult(False, f"执行超时({cfg.timeout}s)")
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        # 完整输出落盘工作区,便于回查;聊天回复取输出尾部
        try:
            Path(cfg.workdir, f".mc_last_output_{self.adapter_name}.log").write_text(out)
        except OSError:
            pass
        return RunResult(proc.returncode == 0, out[-300:], output=out[-4000:])


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
    return m.group(1) if m else ""


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
        cmd = backend.command or ACP_SERVE_COMMANDS[adapter]
        return acp.list_models(cmd, timeout=timeout)
    return []
