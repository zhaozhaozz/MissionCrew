"""聊天协作引擎。

协作模型:
- 人类在频道里 @角色 布置工作;角色是"人格 + 领域上下文 + 路由约束",
  实际执行后端仍由路由器按能力/成本/安全动态选择;
- Agent 的回复原样发布到频道,回复中 @其他角色 即发起协作,平台自动级联触发;
- 所有消息(包括 Agent 之间的)对人类完全可见,全程审计。

防失控:级联深度上限 + 单条协作链的执行总数上限 + 不响应自己 @ 自己。
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Optional

from . import adapters, router
from .config import mc_home
from .models import (TIER_ORDER, Channel, ExecutionConfig, Project, Role,
                     Task, TaskStage)
from .store import Store

MENTION_RE = re.compile(r"@([\w-]+)")
MAX_DEPTH = 4          # 级联深度:人类消息为 0,Agent 回复逐层 +1
                       # 4 层可容纳 主管->执行->评审->主管汇总 的完整闭环
MAX_CHAIN_RUNS = 10    # 单条协作链(同一条人类消息引发)的执行总数上限
HISTORY_WINDOW = 20    # 装配进 Prompt 的最近消息条数
CHAT_TIMEOUT = 900     # 单次聊天执行超时(秒)

# 转交语义:@ 前若紧跟顺序词("完成后请 @reviewer"),说明是让前序角色
# 完成后转交,不立即触发;前序角色回复中 @ 到时才真正执行
DEFER_MARKERS = ("完成后", "然后", "之后", "接着", "随后")
DEFER_WINDOW = 8       # 顺序词与 @ 之间允许的最大字符距离

# 提示词结构约定:人格(role_desc)是"选人用的专长画像",不是任务;
# 任务只来自触发消息(由发起者——人类或调度角色——撰写的简报)
CHAT_PROMPT = """\
# 聊天协作请求
你是角色 @{role_id}({role_name})。
角色定位(这是你的专长画像,供协作方选人参考;它不是任务,不要据此自行发挥):
{role_desc}
{project_section}\
工作目录就是当前目录,直接在其中读写文件、运行命令完成工作。

# 最近对话
{history}

# 触发消息(你的任务简报,由发起者撰写)
{trigger}

# 回复要求
- 只完成触发消息交代的工作;信息不足时在回复中提出,不要臆测扩大范围。
- 你的最终回复会被原样发布到聊天频道,人类和其他角色都能看到。
- 回复用中文,先说结论,再简述做了什么;不要贴大段日志。
- 需要其他角色接手时,在回复中提及 @角色名,并为对方写清楚任务简报:
  背景、要求、验收标准(对方看得到最近对话,但不要假设对方了解全部细节)。
- 角色名册(各自定位供选人参考):
{roster}
- 不需要协作就不要 @ 任何角色;不要 @ 你自己;不要编造不存在的角色。
"""


class ChatEngine:
    def __init__(self, store: Store, max_workers: int = 4):
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="chat-run")
        self._futures: list[Future] = []
        self._futures_lock = threading.Lock()

    # ---- 对外入口 ----
    def post(self, channel_id: str, author: str, content: str,
             author_type: str = "human", reply_to: Optional[int] = None,
             root_id: Optional[int] = None, depth: int = 0) -> int:
        """发布一条消息,并异步触发其中 @ 到的角色。返回消息 id。"""
        channel = self.store.get_channel(channel_id)
        if channel is None:
            raise ValueError(f"频道不存在: {channel_id}")
        # @ 提及只在频道所属项目的角色中生效:项目之间互不相干
        mentions = self._valid_mentions(content, channel.project_id or "",
                                        exclude=author if author_type == "agent" else None)
        msg_id = self.store.add_message(channel_id, author, author_type, content,
                                        mentions, reply_to, root_id, depth)
        root = root_id if root_id is not None else msg_id
        for role_id in mentions:
            self._trigger(channel, role_id, msg_id, root, depth)
        return msg_id

    def wait_idle(self) -> None:
        """等待当前所有聊天执行(含级联)结束,供 CLI 同步模式与测试使用。"""
        while True:
            with self._futures_lock:
                pending = [f for f in self._futures if not f.done()]
                self._futures = pending
            if not pending:
                return
            for f in pending:
                f.result()

    # ---- 内部:触发与执行 ----
    def _valid_mentions(self, content: str, project_id: str,
                        exclude: Optional[str]) -> list[str]:
        known = {r.id for r in self.store.list_roles(project_id)}
        seen = []
        for m in MENTION_RE.finditer(content):
            rid = m.group(1)
            if rid not in known or rid == exclude or rid in seen:
                continue
            window = content[max(0, m.start() - DEFER_WINDOW):m.start()]
            if any(k in window for k in DEFER_MARKERS):
                continue  # 转交指令,等前序角色完成后在回复中 @ 才触发
            seen.append(rid)
        return seen

    def _trigger(self, channel: Channel, role_id: str, msg_id: int,
                 root_id: int, depth: int) -> None:
        if depth >= MAX_DEPTH:
            self.store.add_message(channel.id, "platform", "platform",
                                   f"已达级联深度上限({MAX_DEPTH}),不再触发 @{role_id}。",
                                   [], msg_id, root_id, depth)
            return
        if self.store.count_chain_runs(root_id) >= MAX_CHAIN_RUNS:
            self.store.add_message(channel.id, "platform", "platform",
                                   f"本条协作链执行数已达上限({MAX_CHAIN_RUNS}),"
                                   f"不再触发 @{role_id}。", [], msg_id, root_id, depth)
            return
        run_id = self.store.add_chat_run(channel.id, role_id, msg_id, root_id, depth)
        future = self._pool.submit(self._execute, run_id, channel, role_id,
                                   msg_id, root_id, depth)
        with self._futures_lock:
            self._futures.append(future)

    def _execute(self, run_id: int, channel: Channel, role_id: str,
                 msg_id: int, root_id: int, depth: int) -> None:
        try:
            self._execute_inner(run_id, channel, role_id, msg_id, root_id, depth)
        except Exception as e:  # 后台线程的异常必须落到频道里,不能无声丢失
            self.store.update_chat_run(run_id, "failed", error=str(e))
            self.store.add_message(channel.id, "platform", "platform",
                                   f"@{role_id} 执行出错: {e}", [], msg_id, root_id, depth)

    def _execute_inner(self, run_id: int, channel: Channel, role_id: str,
                       msg_id: int, root_id: int, depth: int) -> None:
        role = self.store.get_role(channel.project_id or "", role_id)
        if role is None:
            self.store.update_chat_run(run_id, "failed", error="角色不存在")
            return

        backend, trace = self._pick_backend(channel, role)
        if backend is None:
            self.store.update_chat_run(run_id, "failed", error=trace)
            self.store.add_message(channel.id, "platform", "platform",
                                   f"@{role_id} 无可用后端: {trace}", [], msg_id, root_id, depth)
            return

        self.store.update_chat_run(run_id, "running", backend_id=backend.id)
        self.store.audit("platform", "chat_dispatch",
                         detail=f"channel={channel.id} role={role_id} "
                                f"backend={backend.id} depth={depth} {trace}")

        cfg = self._assemble(channel, role, backend, msg_id)
        result = adapters.get_adapter(backend.adapter).run(cfg)

        # 配额扣减在工具级记账:重取注册表记录,避免模型副本覆盖工具条目
        stored = self.store.get_backend(backend.id)
        if stored is not None and stored.quota is not None:
            stored.quota = max(0.0, stored.quota - backend.cost_per_run)
            self.store.put_backend(stored)
        self.store.stats_record(backend.id, channel.project_id or "_chat", "chat",
                                result.success)

        if not result.success:
            self.store.update_chat_run(run_id, "failed", backend_id=backend.id,
                                       error=result.summary)
            self.store.add_message(channel.id, "platform", "platform",
                                   f"@{role_id}(后端 {backend.id})执行失败: {result.summary}",
                                   [], msg_id, root_id, depth)
            return

        reply = (result.output or result.summary or "(无输出)").strip()
        self.store.update_chat_run(run_id, "done", backend_id=backend.id)
        # Agent 回复作为该角色的消息发布;其中的 @ 会继续级联(深度 +1)
        self.post(channel.id, role_id, reply, author_type="agent",
                  reply_to=msg_id, root_id=root_id, depth=depth + 1)

    # ---- 内部:路由与装配 ----
    def _pick_backend(self, channel: Channel, role: Role):
        """固定组合(后端+模型)直用;否则按角色约束+偏好复用任务路由器。"""
        if role.pinned_backend:
            b = self.store.get_backend(role.pinned_backend)
            if b is None or not b.enabled:
                return None, f"角色固定的后端 {role.pinned_backend} 不可用"
            if role.pinned_model is not None:
                # 模型在工具阶梯里则带上对应档位/成本;只影响本次执行,不写回注册表
                spec = next((m for m in b.models if m.get("name") == role.pinned_model), None)
                if spec:
                    b = replace(b, model=role.pinned_model, tier=spec.get("tier", b.tier),
                                cost_per_run=float(spec.get("cost", b.cost_per_run)))
                else:
                    b = replace(b, model=role.pinned_model)
            return b, f"角色固定组合 {b.id}" + (f"+{b.model}" if b.model else "")

        project = None
        if channel.project_id:
            project = self.store.get_project(channel.project_id)
        if project is None:
            project = Project(id="_chat", name="聊天")

        # 显式约束 + 偏好标签 => 能力要求与档位窗口;
        # 用 attempts 表达最低档位,复用路由器的窗口逻辑
        caps, min_tier, max_tier = role.effective_constraints()
        floor = TIER_ORDER.index(min_tier) if min_tier in TIER_ORDER else 0
        task = Task(id="_chat", project_id=project.id, title="chat",
                    task_type="chat", max_tier=max_tier)
        stage = TaskStage(name="chat", required_capabilities=caps, attempts=floor)
        decision = router.route(self.store, task, stage, project)
        return decision.backend, decision.reason

    def _assemble(self, channel: Channel, role: Role, backend, msg_id: int) -> ExecutionConfig:
        workdir = Path(channel.workdir) if channel.workdir \
            else mc_home() / "channels" / channel.id
        workdir.mkdir(parents=True, exist_ok=True)

        project_section = ""
        if channel.project_id:
            project = self.store.get_project(channel.project_id)
            if project:
                project_section = (f"频道绑定项目「{project.name}」。项目准则: "
                                   f"{project.charter}\n开发准则: {project.dev_guidelines}\n")

        history_lines = []
        trigger = ""
        for m in self.store.recent_messages(channel.id, HISTORY_WINDOW):
            line = f"[{m['author']}] {m['content']}"
            if m["id"] == msg_id:
                trigger = line
            else:
                history_lines.append(line)

        # 名册带偏好标签与人格定位:人格的用途正是让调度方判断该找谁;
        # 只列本项目的角色,项目之间互不可见
        def _tag(r):
            labels = "/".join(r.trait_labels())
            head = f"@{r.id}({r.name}" + (f"|{labels}" if labels else "") + ")"
            desc = " ".join((r.description or "").split())
            return f"  - {head}: {desc}" if desc else f"  - {head}"
        roster = "\n".join(_tag(r) for r in self.store.list_roles(channel.project_id or "")
                           if r.id != role.id)
        prompt = CHAT_PROMPT.format(
            role_id=role.id, role_name=role.name, role_desc=role.description,
            project_section=project_section,
            history="\n".join(history_lines) or "(无)",
            trigger=trigger, roster=roster or "(无其他角色)",
        )
        return ExecutionConfig(
            task_id=f"chat_{channel.id}", stage_name="chat", backend=backend,
            prompt=prompt, workdir=str(workdir), timeout=CHAT_TIMEOUT,
        )
