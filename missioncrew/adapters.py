"""执行后端适配器。

适配器只负责"启动一次执行并等待结束":任务阶段的证据由引擎从工作区 manifest
读取;聊天协作的回复取自适配器输出。

本地 Agent CLI 支持矩阵(参考 Multica 的本地 agent 列表):
- 打印模式直接支持:claude、codex、opencode、copilot、cursor-agent、codebuddy、pi
- kimi / kiro / qoder / trae 等走 ACP stdio 协议的 CLI,可在 Backend.command
  中配置自定义命令接入,v1 不内置 ACP 客户端。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .assembler import MANIFEST
from .models import Backend, ExecutionConfig, RunResult, TIER_ORDER

# 各适配器的默认命令模板,{prompt}/{model} 在运行时替换;
# model 为空时 {model} 及其前面的 --model/-m 标志会被移除。
DEFAULT_COMMANDS = {
    "claude_code": ["claude", "-p", "{prompt}", "--model", "{model}",
                    "--permission-mode", "acceptEdits"],
    "codex": ["codex", "exec", "--sandbox", "workspace-write", "-m", "{model}", "{prompt}"],
    "opencode": ["opencode", "run", "--model", "{model}", "{prompt}"],
    "copilot": ["copilot", "-p", "{prompt}", "--model", "{model}", "--allow-all-tools"],
    "cursor": ["cursor-agent", "-p", "{prompt}", "--model", "{model}"],
    "codebuddy": ["codebuddy", "-p", "{prompt}", "--model", "{model}"],
    "pi": ["pi", "-p", "{prompt}", "--model", "{model}"],
}

# 本地 CLI 检测表:binary -> (adapter, 默认能力, 默认档位, 成本估算)
_CLAUDE_CAPS = ["coding", "reasoning", "review", "security", "multimodal",
                "web_search", "sub_agents"]
KNOWN_CLIS = [
    ("claude", "claude_code", _CLAUDE_CAPS, "standard", 5.0),
    ("codex", "codex", ["coding", "reasoning", "review", "security"], "standard", 5.0),
    ("opencode", "opencode", ["coding", "reasoning"], "standard", 4.0),
    ("copilot", "copilot", ["coding"], "economy", 2.0),
    ("cursor-agent", "cursor", ["coding", "reasoning"], "standard", 4.0),
    ("codebuddy", "codebuddy", ["coding"], "economy", 2.0),
    ("pi", "pi", ["coding", "reasoning"], "standard", 4.0),
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


_VERSION_RE = re.compile(r"v?\d+\.\d+[\.\d]*")


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
            "id": "claude" if adapter == "claude_code" else adapter,
            "installed": bool(path), "path": path,
            "version": _cli_version(binary) if path and with_version else "",
        })
    return report


def detect_backends(report: Optional[list[dict]] = None) -> list[Backend]:
    """按检测报告生成注册项:一个工具一条记录,模型阶梯自动挂在 models 下。

    档位/成本/能力是内部路由属性(自动填充,不在工具页配置);
    角色层再做偏好、成本、能力、档位的选择。
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


def render_command(template: list[str], prompt: str, model: str) -> list[str]:
    """渲染命令模板;model 为空时移除 {model} 与其紧邻的 --model/-m 标志。"""
    cmd: list[str] = []
    for tok in template:
        if "{model}" in tok:
            if not model:
                if cmd and cmd[-1] in ("--model", "-m"):
                    cmd.pop()
                continue
            tok = tok.replace("{model}", model)
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
        return RunResult(True, reply[:120], output=reply)


class CliAdapter:
    """通用 CLI 适配器:按命令模板在工作目录内启动真实本地 Agent。"""

    def __init__(self, adapter_name: str):
        self.adapter_name = adapter_name

    def run(self, cfg: ExecutionConfig) -> RunResult:
        template = cfg.backend.command or DEFAULT_COMMANDS.get(self.adapter_name)
        if not template:
            return RunResult(False, f"适配器 {self.adapter_name} 未配置命令模板"
                                    f"(ACP 类 CLI 请在 Backend.command 中配置)")
        cmd = render_command(template, cfg.prompt, cfg.backend.model)
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
