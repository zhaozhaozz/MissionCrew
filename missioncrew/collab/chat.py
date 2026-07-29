"""聊天协作引擎。

协作模型:
- 人类在频道里通过角色选择器建立结构化提及;主控派发角色只能执行显式命令
  message.publish 并传 mentions 参数;Agent 消息正文里的任何 @ 都是普通文字,
  角色由固定 runtime/model 执行,定位、能力与偏好用于协作方选人,不参与执行时路由;
- 人类只选择一个角色时直接执行；同一消息选择多个角色时只启动主控，
  由主控根据保留在触发消息中的完整提及名单统一协调;
- 只有项目主控能发起工作,且只能通过 message.publish 的 mentions 显式派发;
  执行角色看不到其他角色名册,主控调度的结果自动交回主控，
  人类直接调度的结果只留在频道等待后续消息;
- 所有主控调度与执行结果都对人类完全可见,全程审计。

防失控:项目可配置的单条协作链执行总数上限 + 不响应自己 @ 自己。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from ..runtime import runtime_manager
from ..core.config import (chat_max_workers, context_reinject_bytes,
                           context_reinject_turns, mc_home)
from .agent_tools import (AgentActionService, AgentIdentity, AgentToolError,
                          DispatchInactiveError, default_agent_tool_url)
from .documents import (document_resource_url, library_for,
                        normalize_document_resource_urls)
from .resource_urls import (channel_resource_url, dashboard_resource_url,
                            guideline_resource_url, missioncrew_project_url,
                            skill_resource_url)
from .workspace import (chat_workspace_dir, platform_history_dir,
                        prepare_agent_workspace, write_task_files)
from ..core.models import (DEFAULT_MAX_CHAIN_RUNS, INJECTION_FULL_MODES,
                           Channel, ExecutionConfig, Role, RuntimePolicy)
from .project_context import project_allowed_dirs, render_project_context
from ..core.store import Store

MENTION_RE = re.compile(r"@([\w-]+)")
EXPLICIT_MENTION_RE = re.compile(r"@\[([\w-]+)\]")
HISTORY_WINDOW = 20    # 装配进 Prompt 的最近消息条数
ACTION_RE = re.compile(r"<missioncrew-action>(.*?)</missioncrew-action>", re.S)


def _reinject_due(session_row: Optional[dict]) -> bool:
    """会话是否该重注入完整公共上下文:压缩标记或增量回合计数达到阈值。"""
    if not session_row:
        return False
    if session_row.get("needs_reinject"):
        return True
    turns_limit = context_reinject_turns()
    if turns_limit > 0 and int(session_row.get("lean_turns") or 0) >= turns_limit:
        return True
    bytes_limit = context_reinject_bytes()
    return bool(bytes_limit > 0
                and int(session_row.get("lean_bytes") or 0) >= bytes_limit)

@dataclass
class _PendingInteraction:
    run_id: int
    backend_id: str
    kind: str
    event_id: int
    payload: dict
    ready: threading.Event = field(default_factory=threading.Event)
    response: Optional[dict] = None
    stopped: bool = False

# 提示词结构约定:人格(role_desc)是"选人用的专长画像",不是任务;
# 任务只来自触发消息(由发起者——人类或调度角色——撰写的简报)
CHAT_COMMON_BODY = """\
# 聊天协作请求
你是角色 @{role_id}({role_name})。
角色定位(这是你的专长画像,供协作方选人参考;它不是任务,不要据此自行发挥):
{role_desc}
角色能力:{role_capabilities}
角色偏好:{role_traits}
固定执行组合:{role_runtime}/{role_model}{role_effort}
当前频道:#{channel_name}
频道用途/讨论边界:{channel_purpose}
{project_section}
{tool_section}
{orchestrator_section}\
工作目录就是当前目录,直接在其中读写文件、运行命令完成工作。

# 频道历史记录(JSON)
完整频道历史文件:{channel_history_path}
也可以通过环境变量 MISSIONCREW_CHANNEL_HISTORY 获取该路径。仅在最近对话不足以完成任务时按需读取。

# 回复要求
- 只完成触发消息交代的工作;信息不足时在回复中提出,不要臆测扩大范围。
- 你的最终回复会被完整、原样发布到聊天频道,人类可以看到。
- 引用 MissionCrew 项目文档时使用项目上下文给出的 `/resources/...` URL；
  不要把内部 `.missioncrew` 路径、绝对文件路径或 `file://` 链接发布到频道。
- 回复用中文,先说结论,再简述做了什么;不要贴大段日志。
{collaboration_section}
"""

DURABLE_CONTEXT_TEMPLATE = """\
# MissionCrew 持久公共上下文
上下文版本:{context_version}

# 会话压缩规则
- 本区块是 MissionCrew 提供的权威公共输入，包含角色、项目、权限、工作目录和协作规则。
- Runtime 执行 compact/上下文压缩时，只压缩普通对话、工具过程和任务细节；必须完整保留本区块，不得摘要、删减或改写。
- 同一会话后续收到版本不同的本区块时，后收到的版本完整取代旧版本；不得继续沿用或合并旧项目设置。

{common_body}
"""

TURN_PROMPT = """\
# MissionCrew Tool 本轮上下文
{tool_context}

# 触发消息(JSON,你的任务简报由发起者撰写)
{trigger}
"""

RECOVERY_PROMPT = """\
# 最近对话(JSON,按消息边界格式化)
{history}

{turn_prompt}
"""

ORCHESTRATOR_TEMPLATE = """\
# 项目主控权限
你是本项目唯一主控，负责理解项目目标、拆解工作并调度其他角色。所有 MissionCrew
写操作必须使用上方显式 Agent Tool；不要在最终回复中生成 missioncrew-action 文本块。
要点：
- `channel.create` 的 workdir 只能是项目代码仓路径（见下方仓库清单）或其子目录；
  不填时若项目只配了一个代码仓则自动使用它。新频道创建后是空的，
  用 `message.publish` 把任务简报发进去，并通过 `mentions` 数组显式选择执行者开工。
- `dashboard.save` 不携带 layout 字段时保留现有布局；携带则全量替换。
  layout 每项含 id、type、title、x、y、width、height、content。
  type 是通用展示原语（领域含义来自数据，不是类型）：
    markdown（content.markdown）、table（columns+rows）、card（metrics 数值卡）、
    chart（kind=bar|line|pie + data + x_key/y_key）、list（items）、
    log（lines）、code（code+language）。
  卡片可用 content.source 绑定平台实时数据，渲染时自动取数：
    {{"from":"tasks","status":["open"],"labels":[]}}（项目任务→表格行）
    {{"from":"document","path":"specs/x.md"}}（文档库文件→markdown）
    {{"from":"audit","actions":[],"limit":30}}（审计事件→列表）
    {{"from":"messages","channel":"general","limit":20}}（频道消息→列表）
  例:需求管理面板 = table 卡片(静态 columns/rows 由你维护) + tasks 源的
  实时任务表;测试记录面板 = table + list;日志分析 = list/log + markdown 结论。
- `guideline.save` 接收完整 markdown，文件必须以只含 name、description 的 YAML
  frontmatter 开头；后端直接读取这两个属性，不使用 id/title/summary，也不做字段转换。
  修改并重命名现有准则时传 original_name。`skill.save` 按 id 新建或覆盖，markdown 是
  完整 SKILL.md 原文：frontmatter 至少含 name、description，附加属性原样保留。
  不要建立文件、Runtime 或角色绑定列表；需要关联项目文档时，在正文中写标准相对
  Markdown 链接。description 应简洁说明适用场景；所有执行者只会收到已启用准则的
  description，并在相关时从对应准则 Markdown 文件读取完整正文。
  Skill 仍结合当前任务自行判断是否适用、是否需要读取链接文件。
- 验证、审查、安全和审批等项目要求也统一写入准则 Markdown，由 Agent 根据任务
  判断是否适用；平台不再维护或机械执行独立的验证规则。`document.publish` 写入项目
  版本化文档库并立即生成 Git 版本，覆盖已有路径时必须显式传 `overwrite: true`。
  删除文档、准则或 Skill 时分别使用 `document.delete`、`guideline.delete`、`skill.delete`；
  删除仅限主控，所有资源进入项目统一回收站。使用 `recycle.list` 查看，
  `recycle.restore` 恢复；只有用户明确要求永久删除时才使用 `recycle.purge`。
- 配置页面协作消息会明确给出当前页面、当前条目、未保存草稿，以及用户选中的
  字段、行号和原文。只提问或讨论时直接回答，不要改配置；明确要求创建或修改时，
  必须使用对应的 `guideline.save` / `skill.save` / `document.publish` 动作实际落库。
- 每个角色的 runtime/模型在项目定义角色时已经固定，你不能也不需要调整；
  调度就是在角色名册中选人：结合角色定位、能力与偏好(风格/领域)挑选
  最合适的角色，执行 `message.publish` 并把角色 id 写进 `mentions` 数组、
  content 写清任务简报。这个显式命令是**唯一**的派发方式；你发出的任何消息
  正文（包括最终回复）里的 @角色ID、@[角色ID] 都只是普通文字，永不触发执行，
  可放心用于描述已完成工作或引用其他角色。
- `message.publish` 返回非空 `dispatched` 时，当前 turn 的派发职责已经完成。
  不要使用 `sleep`，不要轮询频道历史、工作树或运行状态来等待执行角色，也不要
  代替执行角色继续其任务。也不要向仍在执行的角色再次 `message.publish` 追问
  中间状态或重复派发；同一频道同一角色的持久会话无法中途插入新 turn，这类请求
  只会排在原任务后面，不能提供实时进度。
  立即用简短消息说明已派发并结束当前 turn。执行角色完成或失败后，平台会
  自动启动新的主控 turn 并交回完整结果，
  届时再验收、继续调度或汇总。只有 `dispatched` 为空，或仍有不依赖已派发角色
  的即时工作时，才继续当前 turn。
- 当人类在同一条触发消息中选择多个角色时，平台只启动你，不会直接启动这些
  角色。触发消息 JSON 的 `mentions` 和 `mention_spans` 保留了用户选择的完整名单；
  请理解整体目标后决定并行、顺序或调整人选，再用 `message.publish` 的
  `mentions` 分别写清任务并派发。
- 协作链预算：本项目单条协作链最多 {max_runs} 次 Agent 执行。这只是防止失控循环的
  总次数兜底，不限制调度层级；请在预算内自主拆解、分派、验收并推进任务。

## 项目代码仓
{repos}

## 现有频道
{channels}

## 现有面板
{boards}

## 现有准则文档
{guidelines}

## 现有 Skills
{skills}

## 现有 Runtime
{runtimes}
"""


class ChatEngine:
    def __init__(self, store: Store, max_workers: Optional[int] = None):
        self.store = store
        # 并发上限默认读全局配置(env MISSIONCREW_CHAT_MAX_WORKERS);
        # 显式传参供测试与特殊调用方使用。
        self._pool = ThreadPoolExecutor(
            max_workers=chat_max_workers() if max_workers is None else max_workers,
            thread_name_prefix="chat-run")
        self._futures: list[Future] = []
        self._futures_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._chain_run_lock = threading.Lock()
        # 频道停止与运行发布共用同一把锁：停止先赢时禁止迟到回复和后续调度，
        # 回复先赢时停止操作会连同刚产生的后续运行一起捕获。
        self._run_state_lock = threading.RLock()
        self._stopping_channels: set[str] = set()
        self._interaction_lock = threading.Lock()
        self._interactions: dict[str, _PendingInteraction] = {}
        self.agent_tools = AgentActionService(store, self.post)
        # 正在更新的 runtime 集合(由 server 注入共享):更新期间不派发执行,
        # 避免 Agent 跑在半更新的二进制上
        self.updating_backends: set[str] = set()

    @staticmethod
    def _session_key(channel: Channel, role_id: str) -> str:
        base = f"{channel.id}::{role_id}"
        if channel.context_start_message_id:
            return f"{base}::context-{channel.context_start_message_id}"
        return base

    def respond_interaction(self, run_id: int, request_id: str,
                            response: dict) -> None:
        """由聊天 API 回答一个仍在等待的原生 Runtime 请求。"""
        with self._interaction_lock:
            pending = self._interactions.get(request_id)
            if not pending or pending.run_id != run_id:
                raise ValueError("交互请求不存在、已经处理或所属运行不匹配")
            decision = str(response.get("decision") or "")
            allowed = ({"submit", "cancel"} if pending.kind == "user_input_request"
                       else {"approve", "approve_session", "deny", "cancel"})
            if decision not in allowed:
                raise ValueError(f"交互 decision 必须是 {sorted(allowed)} 之一")
            answers = response.get("answers") or {}
            if not isinstance(answers, dict):
                raise ValueError("交互 answers 必须是对象")
            pending.response = {
                "decision": decision,
                "answers": answers,
                "reason": str(response.get("reason") or ""),
            }
            pending.ready.set()

    # ---- 对外入口 ----
    def post(self, channel_id: str, author: str, content: str,
             author_type: str = "human", reply_to: Optional[int] = None,
             root_id: Optional[int] = None, depth: int = 0,
             runtime_id: Optional[str] = None, model: Optional[str] = None,
             effort: Optional[str] = None,
             mention_spans: Optional[list[dict]] = None,
             context: Optional[dict] = None,
             origin_run_id: Optional[int] = None) -> int:
        """发布消息，并只按可信的结构化提及异步触发角色。

        Web 人类消息必须传选择器生成的 ``mention_spans``；省略该参数的
        人类 CLI 调用可用 ``@[role]``。主控 Agent 的派发只能来自
        message.publish 显式命令构造的结构化范围;Agent 消息正文里的
        任何 ``@``（含 ``@[role]``）都只是普通文字，永不触发执行。

        ``origin_run_id`` 是运行中 Agent 发布消息时的发起 run:在停止互斥
        临界区内复查其活跃状态，保证"停止后不再发布回复、不再调度"。
        """
        channel = self.store.get_channel(channel_id)
        if channel is None:
            raise ValueError(f"频道不存在: {channel_id}")
        if channel.archived:
            if author_type != "agent":
                raise ValueError("频道已归档，请先恢复后再发送消息")
            channel.archived = False
            channel.archived_at = 0.0
            self.store.put_channel(channel)
            self.store.audit(
                author, "channel_reactivated",
                detail=f"project={channel.project_id or ''} channel={channel.id}",
            )
        project = self.store.get_project(channel.project_id or "")
        orchestrator = project.orchestrator_role_id if project else ""
        role = (self.store.get_role(channel.project_id or "", author)
                if author_type == "agent" else None)

        legal_spans: list[dict] = []
        # Agent 派发只认 message.publish 显式命令传入的结构化范围;正文里的
        # 任何 @（含 @[role] 旧语法）都是普通文字。执行角色无论写什么，
        # 都只把完整结果交回主控，避免横向看见或调用名册。
        if author_type == "agent":
            if author == orchestrator and mention_spans:
                mentions, legal_spans = self._validate_mention_spans(
                    content, mention_spans, channel.project_id or "")
                if author in mentions:
                    mentions = [item for item in mentions if item != author]
                    legal_spans = [span for span in legal_spans
                                   if span["role_id"] != author]
            elif author != orchestrator and orchestrator and self.store.get_role(
                    channel.project_id or "", orchestrator
            ) and not self._is_direct_human_dispatch(reply_to, author):
                mentions = [orchestrator]
            else:
                mentions = []
            if role:
                runtime_id = role.runtime_id if runtime_id is None else runtime_id
                model = role.model if model is None else model
                effort = role.effort if effort is None else effort
        else:
            # Web 明确传空数组时，正文里的 @xxx 仍是普通文本；人类 CLI 调用
            # 若省略结构化范围，可使用更明确的 @[role] 语法。
            if mention_spans is None:
                content, mentions, legal_spans = self._parse_explicit_mentions(
                    content, channel.project_id or "")
            else:
                mentions, legal_spans = self._validate_mention_spans(
                    content, mention_spans, channel.project_id or "")
        if not mentions and author_type == "human" and channel.project_id:
            if (orchestrator and author != orchestrator
                    and self.store.get_role(channel.project_id, orchestrator)):
                mentions = [orchestrator]
        dispatch_targets = list(mentions)
        if (author_type == "human" and len(mentions) > 1
                and orchestrator and author != orchestrator
                and self.store.get_role(channel.project_id or "", orchestrator)):
            # 保留原始 mentions/mention_spans 供主控理解用户指定的角色，
            # 但多人协作只启动主控，由主控决定顺序、并行方式和具体简报。
            dispatch_targets = [orchestrator]
        with self._run_state_lock:
            # 停止与发布在同一临界区互斥:运行中 Agent 的消息(含派发)必须
            # 复查发起 run 仍活跃,否则"停止"完成后迟到的 message.publish
            # 会让协作链复活;这里插入的新 run 反之会被停止操作一并捕获。
            if channel_id in self._stopping_channels:
                if origin_run_id is not None:
                    raise DispatchInactiveError("频道正在停止 Agent,消息未发布")
                raise ValueError("频道正在停止 Agent，请等待停止完成后再发送消息")
            if (origin_run_id is not None
                    and not self.store.chat_run_is_active(origin_run_id)):
                raise DispatchInactiveError("发起运行已停止,消息未发布")
            msg_id = self.store.add_message(
                channel_id, author, author_type, content, mentions, reply_to,
                root_id, depth, runtime_id or "", model or "", effort or "",
                mention_spans=legal_spans, context=context)
            if (author_type == "agent" and author == orchestrator
                    and not legal_spans):
                # 旧契约的存量会话可能仍在正文里写 @[角色] 试图派发;
                # 静默不触发会让协作链无声死亡,补一条平台提示。
                known = {r.id for r in
                         self.store.list_roles(channel.project_id or "")}
                stale = [m.group(1) for m in EXPLICIT_MENTION_RE.finditer(content)
                         if m.group(1) in known]
                if stale:
                    self.store.add_message(
                        channel_id, "platform", "platform",
                        f"正文中的 @[{'] @['.join(dict.fromkeys(stale))}] 是旧派发"
                        "语法,已不再触发执行;派发请使用 message.publish 的"
                        " mentions 参数。", [], msg_id,
                        root_id if root_id is not None else msg_id, depth)
            self._write_channel_history(channel)
            root = root_id if root_id is not None else msg_id
            for role_id in dispatch_targets:
                self._trigger(channel, role_id, msg_id, root, depth)
        return msg_id

    def _is_direct_human_dispatch(
            self, trigger_message_id: Optional[int], role_id: str) -> bool:
        """判断角色是否由人类在触发消息中直接选择。

        只看可信的落库 mentions，不解析正文中的普通 ``@role``。这样主控派发
        的执行结果仍会自动回传，而人类直接点名角色后的结果只保留在频道中。
        """
        if trigger_message_id is None:
            return False
        trigger = self.store.get_message(trigger_message_id)
        return bool(
            trigger
            and trigger.get("author_type") == "human"
            and role_id in self._decoded_mentions(trigger)
        )

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

    def clear_context(self, channel_id: str) -> dict:
        """结束频道的持久会话，并以可见分隔消息建立新的上下文边界。"""
        channel = self.store.get_channel(channel_id)
        if channel is None:
            raise ValueError(f"频道不存在: {channel_id}")
        if self.store.active_chat_runs(channel_id):
            raise ValueError("频道仍有 Agent 正在运行，请先停止或等待本轮结束后再清除上下文")

        stopped = self.stop_channel_sessions(channel_id)
        cleared_sessions = self.store.clear_chat_sessions(channel_id)
        marker_id = self.store.add_message(
            channel_id, "platform", "platform",
            "上下文已清除 · 后续消息将启动全新的 Runtime 会话",
            [], kind="context_boundary",
        )
        channel.context_start_message_id = marker_id
        self.store.put_channel(channel)
        self._write_channel_history(channel)
        self.store.audit(
            "human", "chat_context_cleared",
            detail=(f"channel={channel_id} sessions={cleared_sessions} "
                    f"runtimes={stopped} marker={marker_id}"),
        )
        return {
            "ok": True, "marker_id": marker_id,
            "cleared_sessions": cleared_sessions, "stopped_runtimes": stopped,
        }

    def stop_channel_sessions(self, channel_id: str) -> int:
        """停止频道的持久 Runtime 实例，但保留原生 session 以便恢复。"""
        channel = self.store.get_channel(channel_id)
        if channel is None:
            raise ValueError(f"频道不存在: {channel_id}")
        targets: dict[tuple[str, str], object] = {}
        for session in self.store.chat_sessions_for_channel(channel_id):
            backend = self.store.get_backend(session["backend_id"])
            if backend is not None:
                targets[(backend.id, session["session_key"])] = backend
        for role in self.store.list_roles(channel.project_id or ""):
            backend = self.store.get_backend(role.runtime_id)
            if backend is not None:
                targets[(backend.id, self._session_key(channel, role.id))] = backend

        stopped = 0
        for (_backend_id, session_key), backend in targets.items():
            stopped += runtime_manager.stop(backend, session_key)
        return stopped

    def stop_channel_agents(self, channel_id: str) -> dict:
        """停止频道内全部活动 Agent，不清除可复用的原生会话。"""
        channel = self.store.get_channel(channel_id)
        if channel is None:
            raise ValueError(f"频道不存在: {channel_id}")

        with self._run_state_lock:
            runs = self.store.stop_active_chat_runs(channel_id)
            if not runs:
                return {
                    "ok": True, "stopped_runs": 0, "interrupted_runtimes": 0,
                    "stopped_runtimes": 0, "runtime_errors": 0,
                }
            self._stopping_channels.add(channel_id)
            run_ids = {run["id"] for run in runs}
            for run in runs:
                self.store.append_run_event(
                    run["id"], "status", "用户已停止当前频道中的 Agent 运行")

            # 等待权限或用户输入的回调必须先释放，否则 Runtime 的当前 turn
            # 即使收到 interrupt，也可能继续阻塞在 MissionCrew 的交互桥上。
            with self._interaction_lock:
                for pending in self._interactions.values():
                    if pending.run_id not in run_ids:
                        continue
                    pending.stopped = True
                    pending.response = {
                        "decision": "cancel", "answers": {},
                        "reason": "用户已停止当前频道中的 Agent 运行",
                    }
                    pending.ready.set()

            targets = {}
            for run in runs:
                backend = self.store.get_backend(run.get("backend_id") or "")
                if backend is None:
                    continue  # queued 或尚未完成后端选择的运行没有进程可停止
                session_key = self._session_key(channel, run["role_id"])
                targets[(backend.id, session_key)] = backend

        # Runtime 原生 interrupt 可能等待协议确认，不能占着运行状态锁；否则
        # 同一 Runtime 的交互回调无法观察到 stopped 并及时返回 cancel。
        interrupted = stopped = runtime_errors = 0
        try:
            for (_backend_id, session_key), backend in targets.items():
                try:
                    if runtime_manager.capabilities(backend).interrupt:
                        count = runtime_manager.interrupt(backend, session_key)
                        interrupted += count
                        if not count:
                            # turn 尚未登记或刚结束时 interrupt 可能返回 0；关闭
                            # 该实例可封住“检查取消状态后、启动 turn 前”的窄竞态。
                            stopped += runtime_manager.stop(backend, session_key)
                    else:
                        stopped += runtime_manager.stop(backend, session_key)
                except Exception as exc:
                    runtime_errors += 1
                    self.store.audit(
                        "platform", "chat_stop_runtime_failed",
                        detail=(f"channel={channel_id} backend={backend.id} "
                                f"session={session_key} error={exc}"),
                    )

            with self._run_state_lock:
                content = f"已停止当前频道中的 {len(runs)} 个 Agent 运行。"
                if runtime_errors:
                    content += f"其中 {runtime_errors} 个 Runtime 控制请求失败，请检查运行状态。"
                marker_id = self.store.add_message(
                    channel_id, "platform", "platform", content, [],
                    kind="agent_stop",
                )
                self._write_channel_history(channel)
        finally:
            with self._run_state_lock:
                self._stopping_channels.discard(channel_id)
        self.store.audit(
            "human", "chat_agents_stopped",
            detail=(f"channel={channel_id} runs={len(runs)} "
                    f"interrupted={interrupted} stopped={stopped} "
                    f"errors={runtime_errors} marker={marker_id}"),
        )
        return {
            "ok": True, "marker_id": marker_id,
            "stopped_runs": len(runs),
            "interrupted_runtimes": interrupted,
            "stopped_runtimes": stopped,
            "runtime_errors": runtime_errors,
        }

    # ---- 内部:可信提及、触发与执行 ----
    def _parse_explicit_mentions(self, content: str,
                                 project_id: str) -> tuple[str, list[str], list[dict]]:
        """把人类 CLI 正文的 ``@[role]`` 归一化成可见 ``@role`` 与精确范围。

        仅服务无选择器的人类 CLI 入口;Agent 消息不经过本函数——Agent 的
        派发只能来自 message.publish 显式命令构造的结构化范围。
        """
        known = {role.id for role in self.store.list_roles(project_id)}
        parts: list[str] = []
        targets: list[str] = []
        spans: list[dict] = []
        cursor = 0
        output_length = 0
        for match in EXPLICIT_MENTION_RE.finditer(content):
            prefix = content[cursor:match.start()]
            parts.append(prefix)
            output_length += len(prefix)
            role_id = match.group(1)
            if role_id not in known:
                raw = match.group(0)
                parts.append(raw)
                output_length += len(raw)
            else:
                visible = f"@{role_id}"
                start = output_length
                parts.append(visible)
                output_length += len(visible)
                spans.append({"role_id": role_id, "start": start,
                              "end": output_length})
                if role_id not in targets:
                    targets.append(role_id)
            cursor = match.end()
        parts.append(content[cursor:])
        return "".join(parts), targets, spans

    def _validate_mention_spans(self, content: str, requested: list[dict],
                                project_id: str) -> tuple[list[str], list[dict]]:
        """验证 UI 选择器给出的 Unicode code-point 范围，不从正文猜目标。"""
        known = {role.id for role in self.store.list_roles(project_id)}
        normalized: list[dict] = []
        for item in requested:
            if not isinstance(item, dict):
                raise ValueError("提及必须由角色选择器生成")
            role_id = item.get("role_id")
            start, end = item.get("start"), item.get("end")
            if (not isinstance(role_id, str) or role_id not in known
                    or type(start) is not int or type(end) is not int
                    or start < 0 or end <= start or end > len(content)
                    or content[start:end] != f"@{role_id}"):
                raise ValueError("提及范围无效，请从角色选择器重新选择")
            normalized.append({"role_id": role_id, "start": start, "end": end})

        normalized.sort(key=lambda item: (item["start"], item["end"]))
        previous_end = -1
        targets: list[str] = []
        for item in normalized:
            if item["start"] < previous_end:
                raise ValueError("提及范围重叠，请从角色选择器重新选择")
            previous_end = item["end"]
            if item["role_id"] not in targets:
                targets.append(item["role_id"])
        return targets, normalized

    def _trigger(self, channel: Channel, role_id: str, msg_id: int,
                 root_id: int, depth: int) -> None:
        project = self.store.get_project(channel.project_id or "")
        max_runs = project.max_chain_runs if project else DEFAULT_MAX_CHAIN_RUNS
        # count + insert 必须串行，否则并行分支可能同时看到剩余额度并突破上限。
        with self._chain_run_lock:
            budget_exhausted = self.store.count_chain_runs(root_id) >= max_runs
            run_id = (0 if budget_exhausted else
                      self.store.add_chat_run(
                          channel.id, role_id, msg_id, root_id, depth))
        if budget_exhausted:
            self.store.add_message(channel.id, "platform", "platform",
                                   f"本条协作链执行数已达上限({max_runs}),"
                                   f"不再触发 @{role_id}。", [], msg_id, root_id, depth)
            self._write_channel_history(channel)
            return
        future = self._pool.submit(self._execute, run_id, channel, role_id,
                                   msg_id, root_id, depth)
        with self._futures_lock:
            self._futures.append(future)

    def _execute(self, run_id: int, channel: Channel, role_id: str,
                 msg_id: int, root_id: int, depth: int) -> None:
        with self._run_state_lock:
            if not self.store.chat_run_is_active(run_id):
                return
        try:
            self._execute_inner(run_id, channel, role_id, msg_id, root_id, depth)
        except Exception as e:  # 后台线程的异常必须落到频道里,不能无声丢失
            error = str(e)
            project = self.store.get_project(channel.project_id or "")
            if project:
                error = normalize_document_resource_urls(
                    error, project.id, [library_for(project.id).root])
            with self._run_state_lock:
                if not self.store.chat_run_is_active(run_id):
                    return
                try:
                    self._post_failure(channel, role_id, msg_id, root_id, depth,
                                       f"@{role_id} 执行出错: {error}")
                finally:
                    self.store.update_chat_run(run_id, "failed", error=error)

    def _post_failure(self, channel: Channel, role_id: str, msg_id: int,
                      root_id: int, depth: int, content: str) -> None:
        """公开执行失败；仅把主控派发的失败自动交回主控。"""
        project = self.store.get_project(channel.project_id or "")
        if project:
            content = normalize_document_resource_urls(
                content, project.id, [library_for(project.id).root])
        result_depth = depth + 1
        failure_id = self.store.add_message(
            channel.id, "platform", "platform", content, [], msg_id, root_id,
            result_depth)
        self._write_channel_history(channel)
        orchestrator = project.orchestrator_role_id if project else ""
        if (orchestrator and role_id != orchestrator
                and self.store.get_role(channel.project_id or "", orchestrator)
                and not self._is_direct_human_dispatch(msg_id, role_id)):
            self._trigger(channel, orchestrator, failure_id, root_id, result_depth)

    def _execute_inner(self, run_id: int, channel: Channel, role_id: str,
                       msg_id: int, root_id: int, depth: int) -> None:
        role = self.store.get_role(channel.project_id or "", role_id)
        if role is None:
            with self._run_state_lock:
                if not self.store.chat_run_is_active(run_id):
                    return
                try:
                    self._post_failure(channel, role_id, msg_id, root_id, depth,
                                       f"@{role_id} 执行失败: 角色不存在")
                finally:
                    self.store.update_chat_run(run_id, "failed", error="角色不存在")
            return

        backend, trace = self._pick_backend(channel, role)
        if backend is None:
            with self._run_state_lock:
                if not self.store.chat_run_is_active(run_id):
                    return
                try:
                    self._post_failure(channel, role_id, msg_id, root_id, depth,
                                       f"@{role_id} 无可用后端: {trace}")
                finally:
                    self.store.update_chat_run(run_id, "failed", error=trace)
            return

        if not self.store.update_chat_run(run_id, "running", backend_id=backend.id):
            return
        self.store.audit("platform", "chat_dispatch",
                         detail=f"channel={channel.id} role={role_id} "
                                f"backend={backend.id} depth={depth} {trace}")

        cfg = self._assemble(channel, role, backend, msg_id, run_id=run_id)
        project = self.store.get_project(channel.project_id or "")
        library = library_for(channel.project_id or "")
        document_roots = [
            library.root, cfg.env.get("MISSIONCREW_DOCUMENTS_DIR", "")]
        document_markers = [str(root) for root in document_roots if str(root)]

        def _emit(kind: str, text: str):
            if not self.store.chat_run_is_active(run_id):
                return None
            may_contain_document_path = (
                ".missioncrew" in text
                or any(marker in text for marker in document_markers))
            if (project and may_contain_document_path
                    and kind in {"text", "stdout", "stderr", "tool", "tool_result"}):
                text = normalize_document_resource_urls(
                    text, project.id, document_roots)
            return self.store.append_run_event(run_id, kind, text)

        # 运行过程(思考/工具/输出)实时落库,前端在聊天流中内联展示
        cfg.emit = _emit
        cfg.interact = lambda kind, payload: self._request_runtime_interaction(
            run_id, backend.id, kind, payload, cfg.timeout)
        cfg.cancelled = lambda: not self.store.chat_run_is_active(run_id)
        library.commit_changes("platform", "Capture external document changes before chat run")
        with self._run_state_lock:
            if not self.store.chat_run_is_active(run_id):
                return
        result = runtime_manager.start(cfg)
        revision = library.commit_changes(
            f"role:{role.id}", f"Documents updated from channel {channel.name}")
        if revision:   # Agent 直接写目录的改动也进平台审计,与 API 写入口径一致
            self.store.audit(f"role:{role.id}", "documents_committed",
                             detail=f"project={channel.project_id} revision={revision[:10]}")
        tasks_dir = cfg.env.get("MISSIONCREW_TASKS_DIR")
        if tasks_dir:
            write_task_files(self.store, channel.project_id or "", Path(tasks_dir))

        # 配额扣减在工具级记账:重取注册表记录,避免模型副本覆盖工具条目
        stored = self.store.get_backend(backend.id)
        if stored is not None and stored.quota is not None:
            stored.quota = max(0.0, stored.quota - backend.cost_per_run)
            self.store.put_backend(stored)
        # 从处理 Runtime 结果到发布消息/控制动作必须和频道停止原子互斥。
        # 停止先发生则丢弃迟到结果；发布先发生则停止会捕获新调度的运行。
        with self._run_state_lock:
            if not self.store.chat_run_is_active(run_id):
                return
            result_output = (result.output or "").strip()
            result_summary = (result.summary or "").strip()
            if not result_output and not result_summary:
                # Runtime 进程退出不等于协作完成。统一补一条平台消息，执行
                # 结果是否交回主控仍遵循本轮由谁发起；主控自身无输出时也
                # 能让人类看到异常。
                exit_kind = "正常退出" if result.success else "异常退出"
                no_output = (
                    f"@{role_id}(后端 {backend.id}){exit_kind}，"
                    "但未产生任何可回传输出。请检查该运行的过程事件、"
                    "Task brief 和已发布产物，再决定是否重试或另行安排。"
                )
                try:
                    self._post_failure(
                        channel, role_id, msg_id, root_id, depth, no_output)
                finally:
                    self.store.update_chat_run(
                        run_id, "failed", backend_id=backend.id,
                        error=f"Runtime {exit_kind}但未产生可回传输出")
                return
            if not result.success:
                failure = result_summary or result_output
                public_summary = (normalize_document_resource_urls(
                    failure, project.id, document_roots)
                    if project else failure)
                try:
                    self._post_failure(
                        channel, role_id, msg_id, root_id, depth,
                        f"@{role_id}(后端 {backend.id})执行失败: {public_summary}")
                finally:
                    self.store.update_chat_run(
                        run_id, "failed", backend_id=backend.id, error=public_summary)
                return

            reply = result_output or result_summary
            if project and role.id == project.orchestrator_role_id:
                reply = self._apply_orchestrator_actions(
                    project, role.id, reply, root_id=root_id, depth=depth)
            elif ACTION_RE.search(reply):
                # 非主控回复中的控制动作:剥离并明示未执行,避免读者误以为已生效
                reply = ACTION_RE.sub("", reply).strip()
                reply += "\n\n(检测到平台控制动作,但只有项目主控可以执行,未生效)"
            if project:
                reply = normalize_document_resource_urls(
                    reply, project.id, document_roots)
                self.store.rewrite_run_events(
                    run_id,
                    lambda content: normalize_document_resource_urls(
                        content, project.id, document_roots),
                    {"text", "stdout", "stderr", "tool", "tool_result"},
                )
            # 运行中先实时展示模型输出；最终回复确定后，如果 text/stdout 与即将
            # 发布的 Agent 消息完全一致，就移除重复事件。部分输出或带进度的输出保留。
            self.store.remove_duplicate_reply_output(run_id, reply)
            # 最终回复永不派发(派发只发生在运行中的 message.publish 显式命令);
            # 执行结果是否返回主控取决于触发方。
            self.post(channel.id, role_id, reply, author_type="agent",
                      reply_to=msg_id, root_id=root_id, depth=depth + 1,
                      runtime_id=backend.id, model=backend.model, effort=cfg.effort)
            self.store.update_chat_run(run_id, "done", backend_id=backend.id)

    def _request_runtime_interaction(self, run_id: int, backend_id: str,
                                     kind: str, payload: dict,
                                     timeout: Optional[float]) -> dict:
        """持久化待处理请求，并阻塞原生协议回调直到用户回答。"""
        with self._run_state_lock:
            if not self.store.wait_chat_run_for_interaction(run_id, backend_id):
                return {
                    "decision": "cancel", "answers": {},
                    "reason": "运行已经结束",
                }
            request_id = uuid.uuid4().hex
            visible = {
                **payload,
                "request_id": request_id,
                "status": "pending",
            }
            event_id = self.store.append_interaction_event(run_id, kind, visible)
            pending = _PendingInteraction(
                run_id=run_id, backend_id=backend_id, kind=kind,
                event_id=event_id, payload=visible)
            with self._interaction_lock:
                self._interactions[request_id] = pending
        self.store.audit(
            "platform", "runtime_interaction_requested",
            detail=f"run={run_id} kind={kind} request={request_id}")

        answered = pending.ready.wait(timeout)
        with self._interaction_lock:
            self._interactions.pop(request_id, None)
        response = pending.response if answered and pending.response else {
            "decision": "cancel", "answers": {},
            "reason": "等待用户回答超时",
        }
        # 不把回答正文写回事件，避免 secret input 或凭据进入日志。
        resolved = {
            **visible,
            "status": ("stopped" if pending.stopped else
                       "resolved" if answered else "timeout"),
            "decision": response["decision"],
            "resolved_at": time.time(),
        }
        self.store.update_interaction_event(event_id, run_id, resolved)
        self.store.resume_chat_run_after_interaction(run_id, backend_id)
        self.store.audit(
            "platform" if pending.stopped or not answered else "human",
            "runtime_interaction_resolved",
            detail=(f"run={run_id} kind={kind} request={request_id} "
                    f"decision={response['decision']}"))
        return response

    # ---- 内部:固定执行组合与上下文装配 ----
    def _pick_backend(self, _channel: Channel, role: Role):
        """角色的 runtime/model 在定义时已固定,执行时直接使用,不做自动路由。"""
        if not role.runtime_id:
            return None, "角色未绑定 runtime,请在角色设置中选择 runtime 与模型"
        if role.runtime_id in self.updating_backends:
            return None, f"runtime {role.runtime_id} 正在更新中,请稍后再试"
        b = self.store.get_backend(role.runtime_id)
        if b is None or not b.enabled:
            return None, f"角色固定的 runtime {role.runtime_id} 不可用"
        # 模型只影响本次执行,不写回注册表;档位与成本始终取工具级取值。
        b = replace(b, model=role.model)
        model = b.model or "(CLI 默认)"
        combo = f"{b.id}+{model}" + (f"+effort={role.effort}" if role.effort else "")
        return b, f"角色固定组合 {combo}"

    def _assemble(self, channel: Channel, role: Role, backend, msg_id: int,
                  run_id: int = 0) -> ExecutionConfig:
        workdir = Path(channel.workdir) if channel.workdir \
            else mc_home() / "channels" / channel.id
        workdir.mkdir(parents=True, exist_ok=True)

        project_section = ""
        tool_section = ""
        orchestrator_section = ""
        project = None
        workspace = None
        env = {}
        allowed_dirs = []
        agent_action = None
        if channel.project_id:
            project = self.store.get_project(channel.project_id)
            if project:
                library = library_for(project.id)
                workspace, _ = prepare_agent_workspace(
                    self.store, project, library,
                    chat_workspace_dir(project.id, channel.id, role.id),
                    has_history=True)
                project_section = render_project_context(
                    project, library, workspace.root)
                env["MISSIONCREW_WORKSPACE"] = str(workspace.root)
                env["MISSIONCREW_DOCUMENTS_DIR"] = str(workspace.documents)
                env["MISSIONCREW_DOCUMENTS_URL"] = document_resource_url(project.id)
                env["MISSIONCREW_PROJECT_URL"] = missioncrew_project_url(project.id)
                env["MISSIONCREW_GUIDELINES_DIR"] = str(workspace.guidelines)
                env["MISSIONCREW_SKILLS_DIR"] = str(workspace.skills)
                env["MISSIONCREW_TASKS_DIR"] = str(workspace.tasks)
                token_file, token_id = self.agent_tools.ensure_token_file(
                    project, channel, role.id, workspace.root)
                tool_url = default_agent_tool_url()
                env["MISSIONCREW_AGENT_TOOL_URL"] = tool_url
                env["MISSIONCREW_AGENT_TOKEN_FILE"] = str(token_file)
                env["MISSIONCREW_AGENT_RUN_ID"] = str(run_id) if run_id else ""
                env["MISSIONCREW_AGENT_TOOL_PYTHON"] = sys.executable
                env["MISSIONCREW_CHANNEL_ID"] = channel.id
                allowed_actions = self.agent_tools.allowed_actions(project, role.id)
                # 进程内 Agent Tool 句柄:与 HTTP 入口同一鉴权/审计路径,供
                # 无法起子进程调 CLI 的适配器(如 MockAdapter)执行显式命令。
                identity = AgentIdentity(
                    token_id=token_id, project_id=project.id,
                    channel_id=channel.id, role_id=role.id,
                    issued_scopes=tuple(allowed_actions))
                agent_action = (
                    (lambda action, arguments, _identity=identity:
                        self.agent_tools.execute(
                            _identity, action, arguments, run_id=run_id,
                            request_id=f"inproc-{uuid.uuid4().hex}"))
                    if run_id else None)
                tool_section = (
                    "# MissionCrew Agent Tool\n"
                    "MissionCrew 平台写操作必须显式调用此工具；命令返回 JSON，失败时退出码非零，"
                    "请读取 error.code/error.message 并在当前回合修正后重试。不要通过最终回复中的"
                    "特殊文本块请求平台操作，也不要直接写 documents/tasks 来绕过接口。"
                    "消息正文里的任何 @ 都只是普通文字，不构成平台指令。\n"
                    f"Python：`{sys.executable}`\n"
                    f"API：`{tool_url}`\n"
                    f"Token 文件：`{token_file}`（不要读取、打印或发送其内容）\n"
                    "查看能力：`\"$MISSIONCREW_AGENT_TOOL_PYTHON\" -m "
                    "missioncrew.agent_tool actions`\n"
                    "调用格式：`\"$MISSIONCREW_AGENT_TOOL_PYTHON\" -m "
                    "missioncrew.agent_tool call <action> --run-id <本轮 run_id> "
                    "--arguments '<JSON 对象>'`\n"
                    "发布文件：`\"$MISSIONCREW_AGENT_TOOL_PYTHON\" -m "
                    "missioncrew.agent_tool publish-file --run-id <本轮 run_id> "
                    "--source <本地文件> --path <文档库相对路径>`\n"
                    "可用动作：" + ", ".join(allowed_actions)
                )
                allowed_dirs = project_allowed_dirs(project, library, workspace.root)
                if role.id == project.orchestrator_role_id:
                    orchestrator_section = self._orchestrator_section(project)

        is_orchestrator = bool(project and role.id == project.orchestrator_role_id)
        orchestrator_id = project.orchestrator_role_id if project else ""
        known_roles = {r.id for r in self.store.list_roles(channel.project_id or "")}

        history_records = []
        trigger_message = self.store.get_message(msg_id)
        for m in self.store.recent_messages(
                channel.id, HISTORY_WINDOW, channel.context_start_message_id):
            if m["id"] != msg_id and is_orchestrator:
                history_records.append(
                    self._message_record(m, role, project, known_roles))

        trigger_record = (self._message_record(
                              trigger_message, role, project, known_roles)
                          if trigger_message else {})
        channel_history_path = self._write_channel_history(channel, role, project)
        history_parent = str(channel_history_path.parent.resolve())
        if history_parent not in allowed_dirs:
            allowed_dirs.append(history_parent)
        env["MISSIONCREW_CHANNEL_HISTORY"] = str(channel_history_path)

        # 只有主控拿到项目角色名册；执行角色只接收当前任务简报，不知道也
        # 不能横向调度其他执行角色。
        def _tag(r):
            labels = "/".join([*r.ability_labels(),
                               *( [r.preference] if r.preference else [] )])
            head = f"@{r.id}({r.name}" + (f"|{labels}" if labels else "") + ")"
            desc = " ".join((r.description or "").split())
            return f"  - {head}: {desc}" if desc else f"  - {head}"
        if is_orchestrator:
            roster = "\n".join(
                _tag(r) for r in self.store.list_roles(channel.project_id or "")
                if r.id != role.id) or "(无其他角色)"
            collaboration_section = (
                "- 只有你（项目主控）可以调度其他角色。派发必须执行显式命令："
                "`message.publish` 传 `mentions` 角色数组，content 写清背景、"
                "要求和验收标准。消息正文里的 @角色ID、@[角色ID] 都只是普通文字，"
                "永不触发执行。\n"
                "- 不需要协作就不要传 mentions；不要调度你自己，不要编造不存在的角色。\n"
                "- 角色名册（仅主控可见，各自定位供你选人参考）：\n" + roster
            )
        else:
            collaboration_section = (
                "- 你不是项目主控，看不到其他执行角色名册，也不能调度其他角色；"
                "消息正文里写任何 @ 都只是普通文字。\n"
                "- 只提交本次任务的完整结果。若本轮由项目主控派发，平台会把结果"
                "自动交回主控；若由人类直接点名，结果只发布到频道，不会自动触发"
                "主控，主控在后续被人类唤起时仍可读取完整记录。"
            )
        common_body = CHAT_COMMON_BODY.format(
            role_id=role.id, role_name=role.name, role_desc=role.description,
            role_capabilities=", ".join(role.capabilities) or "无特别标注",
            role_traits=role.preference or "无特别标注",
            role_runtime=role.runtime_id,
            role_model=role.model or "(CLI 默认)",
            role_effort=f"/effort={role.effort}" if role.effort else "",
            channel_name=channel.name or channel.id,
            channel_purpose=channel.purpose or "(未说明)",
            project_section=project_section,
            tool_section=tool_section,
            orchestrator_section=orchestrator_section,
            channel_history_path=channel_history_path,
            collaboration_section=collaboration_section,
        )
        context_version = hashlib.sha256(common_body.encode("utf-8")).hexdigest()[:16]
        common_prompt = DURABLE_CONTEXT_TEMPLATE.format(
            context_version=context_version, common_body=common_body)
        tool_context = (
            f"本轮 run_id：`{run_id}`。所有写调用必须显式传 `--run-id {run_id}`；"
            "不要依赖持久 Runtime 进程继承的旧环境变量。"
            if run_id else "本次仅装配上下文，未分配可执行的 run_id。")
        turn_prompt = TURN_PROMPT.format(
            tool_context=tool_context,
            trigger=json.dumps(trigger_record, ensure_ascii=False, indent=2))
        recovery_prompt = RECOVERY_PROMPT.format(
            history=json.dumps(history_records, ensure_ascii=False, indent=2),
            turn_prompt=turn_prompt,
        )
        prompt = common_prompt + "\n" + recovery_prompt

        session_key = self._session_key(channel, role.id)

        def _compatible_session(value: Optional[dict]) -> bool:
            return bool(
                value
                and value["backend_id"] == backend.id
                and value["adapter"] == backend.adapter
                and Path(value["workdir"]).resolve() == workdir.resolve()
            )

        saved_session = self.store.get_chat_session(session_key)
        # 自定义打印命令的参数语义未知，不能猜测 resume 标志；若从默认命令
        # 切到自定义模板，先丢弃旧原生 id，避免以后切回时恢复一段缺轮次的历史。
        native_session_supported = runtime_manager.supports_session(backend)
        if saved_session and not native_session_supported:
            self.store.delete_chat_session(session_key)
            saved_session = None
        compatible = _compatible_session(saved_session)
        if saved_session and not compatible:
            self.store.delete_chat_session(session_key)
            saved_session = None
        session_id = (saved_session["runtime_session_id"]
                      if saved_session and compatible else "")
        previous_context = (saved_session["context_version"]
                            if saved_session and compatible else "")

        def _load_session() -> tuple[str, str, bool]:
            latest = self.store.get_chat_session(session_key)
            if not _compatible_session(latest):
                if latest:
                    self.store.delete_chat_session(session_key)
                return "", "", False
            assert latest is not None
            return (str(latest["runtime_session_id"]),
                    str(latest["context_version"]),
                    _reinject_due(latest))

        def _save_session(runtime_session_id: str, accepted_context: str,
                          turn_mode: str = "", turn_bytes: int = 0,
                          compact_seen: bool = False) -> None:
            if not runtime_session_id:
                self.store.delete_chat_session(session_key)
                return
            # 完整注入清零重注入状态;本轮又检测到压缩时保留标记,
            # 下一轮再次完整注入
            self.store.put_chat_session(
                session_key, channel.id, role.id, backend.id, backend.adapter,
                str(workdir.resolve()), runtime_session_id,
                accepted_context or previous_context,
                turn_mode=turn_mode, turn_bytes=turn_bytes,
                clear_reinject=(turn_mode in INJECTION_FULL_MODES
                                and not compact_seen),
            )

        env["MISSIONCREW_SESSION_KEY"] = session_key
        runtime_policy = RuntimePolicy(
            readable_paths=list(allowed_dirs),
            writable_paths=list(allowed_dirs),
            skill_paths=[str(workspace.skills)] if workspace is not None else [],
        )
        return ExecutionConfig(
            task_id=f"chat_{channel.id}", stage_name="chat", backend=backend,
            prompt=prompt, workdir=str(workdir), project_id=channel.project_id or "",
            role_id=role.id, runtime_policy=runtime_policy,
            env=env,
            effort=role.effort,
            session_key=session_key, session_id=session_id,
            common_prompt=common_prompt, turn_prompt=turn_prompt,
            recovery_prompt=recovery_prompt, context_version=context_version,
            context_changed=bool(session_id and previous_context != context_version),
            reinject_due=bool(session_id) and _reinject_due(saved_session),
            load_session=_load_session,
            save_session=_save_session,
            mark_reinject=lambda: self.store.mark_chat_session_reinject(
                session_key),
            agent_action=agent_action,
        )

    @staticmethod
    def _decoded_mentions(message: dict) -> list[str]:
        raw = message.get("mentions", "[]")
        try:
            mentions = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return []
        return [str(item) for item in mentions] if isinstance(mentions, list) else []

    @staticmethod
    def _decoded_mention_spans(message: dict) -> list[dict]:
        raw = message.get("mention_spans", "[]")
        try:
            spans = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return []
        return [dict(item) for item in spans if isinstance(item, dict)] \
            if isinstance(spans, list) else []

    def _message_record(self, message: dict, role: Optional[Role] = None,
                        project=None, known_roles: Optional[set[str]] = None) -> dict:
        """把数据库消息转换为边界明确的 JSON 记录，并按执行角色脱敏。"""
        author = str(message.get("author", ""))
        author_type = str(message.get("author_type", ""))
        content = str(message.get("content", ""))
        mentions = self._decoded_mentions(message)
        mention_spans = self._decoded_mention_spans(message)
        raw_context = message.get("context", "{}")
        try:
            message_context = (json.loads(raw_context)
                               if isinstance(raw_context, str) else raw_context)
        except (json.JSONDecodeError, TypeError):
            message_context = {}
        if not isinstance(message_context, dict):
            message_context = {}
        if project:
            document_root = mc_home() / "projects" / project.id / "documents"
            original_content = content
            content = normalize_document_resource_urls(
                original_content, project.id, [document_root])
            if content != original_content:
                for span in mention_spans:
                    start, end = span.get("start"), span.get("end")
                    if type(start) is not int or type(end) is not int:
                        continue
                    span["start"] = len(normalize_document_resource_urls(
                        original_content[:start], project.id, [document_root]))
                    span["end"] = len(normalize_document_resource_urls(
                        original_content[:end], project.id, [document_root]))
        orchestrator_id = project.orchestrator_role_id if project else ""
        known_roles = (known_roles if known_roles is not None else
                       ({r.id for r in self.store.list_roles(project.id)}
                        if project else set()))
        full_view = role is None or not project or role.id == orchestrator_id
        redact_author = (not full_view and author_type == "agent"
                         and author not in {role.id, orchestrator_id})

        if not full_view:
            def _visible_mention(match: re.Match) -> str:
                role_id = match.group(1)
                if (role_id in {role.id, orchestrator_id}
                        or role_id not in known_roles):
                    return match.group(0)
                return "[其他执行角色]"

            # @[role] 旧语法不再触发执行,但作为字面文本仍可能泄漏角色名,
            # 与普通 @role 同样脱敏。
            content = EXPLICIT_MENTION_RE.sub(_visible_mention, content)
            content = MENTION_RE.sub(_visible_mention, content)
            # 脱敏替换会改变字符偏移；执行角色不需要渲染主控界面的提及样式。
            mention_spans = []
            mentions = [
                item if item in {role.id, orchestrator_id} or item not in known_roles
                else "其他执行角色"
                for item in mentions
            ]

        record = {
            "id": int(message.get("id", 0)),
            "kind": str(message.get("kind") or "message"),
            "author": {
                "id": "执行角色" if redact_author else author,
                "type": author_type,
            },
            "content": content,
            "mentions": mentions,
            "mention_spans": mention_spans,
            "thread": {
                "reply_to": message.get("reply_to"),
                "root_id": message.get("root_id"),
                "depth": int(message.get("depth", 0)),
            },
            "created_at": message.get("created_at"),
        }
        if message_context:
            record["context"] = message_context
        if not redact_author and any(message.get(key) for key in
                                     ("runtime_id", "model", "effort")):
            record["execution"] = {
                "runtime": str(message.get("runtime_id", "")),
                "model": str(message.get("model", "")),
                "effort": str(message.get("effort", "")),
            }
        return record

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _write_channel_history(self, channel: Channel,
                               role: Optional[Role] = None, project=None) -> Path:
        """原子更新完整频道历史，并返回当前角色获准读取的 JSON 视图。"""
        # 原始历史保存在平台内部工作区；每个角色只获准读取自己独立
        # `.missioncrew/channel-history.json`，避免横向看到其他执行角色视图。
        project = project or self.store.get_project(channel.project_id or "")
        project_id = channel.project_id or "_unscoped"
        canonical = platform_history_dir(project_id, channel.id) / "channel-history.json"
        orchestrator_id = project.orchestrator_role_id if project else ""
        scoped = bool(role and project and role.id != orchestrator_id)
        visible_path = (chat_workspace_dir(project.id, channel.id, role.id)
                        / "channel-history.json"
                        if role and project else canonical)

        with self._history_lock:
            messages = self.store.all_messages(channel.id)

            def _payload(records: list[dict]) -> dict:
                return {
                    "schema_version": 1,
                    "channel": {
                        "id": channel.id,
                        "name": channel.name,
                        "project_id": channel.project_id,
                        "purpose": channel.purpose,
                        "context_start_message_id": channel.context_start_message_id,
                    },
                    "message_count": len(records),
                    "last_message_id": records[-1]["id"] if records else None,
                    "messages": records,
                }

            canonical_records = [
                self._message_record(m, project=project) for m in messages]
            self._atomic_write_json(canonical, _payload(canonical_records))
            if visible_path != canonical:
                known_roles = {r.id for r in self.store.list_roles(project.id)}
                visible_records = (
                    [self._message_record(m, role, project, known_roles)
                     for m in messages]
                    if scoped else canonical_records)
                self._atomic_write_json(visible_path, _payload(visible_records))
            elif project:
                # 新消息落库时刷新已经建立的角色视图；尚未执行过的角色不提前
                # 创建 workspace，等首次装配时再生成。
                known_roles = {r.id for r in self.store.list_roles(project.id)}
                for target_role in self.store.list_roles(project.id):
                    target = (chat_workspace_dir(
                        project.id, channel.id, target_role.id)
                              / "channel-history.json")
                    if not target.parent.is_dir():
                        continue
                    target_scoped = target_role.id != project.orchestrator_role_id
                    records = (
                        [self._message_record(m, target_role, project, known_roles)
                         for m in messages]
                        if target_scoped else canonical_records)
                    self._atomic_write_json(target, _payload(records))
        return visible_path

    def _orchestrator_section(self, project) -> str:
        """主控专属上下文:动作说明 + 代码仓/现有频道/现有面板清单与调度预算。

        清单让 create/update 决策有据可依(否则主控只能靠聊天历史猜 id,
        容易触发"频道已存在/面板不存在")。
        """
        def _short(cid: str) -> str:
            return cid.removeprefix(f"{project.id}:")

        repos = "\n".join(
            f"- {r.name or r.id}: {r.path or '(无本地路径)'}"
            + (f"(git 远程 {r.remote})" if r.remote else "")
            for r in project.repos) or "(未配置)"
        channels = "\n".join(
            f"- {_short(c.id)}(#{c.name}):{c.purpose or '无用途说明'}"
            + (f";工作目录 {c.workdir}" if c.workdir else "")
            + f";Web {channel_resource_url(project.id, c.id)}"
            for c in self.store.list_channels(
                project.id, include_archived=False)) or "(无)"
        boards = "\n".join(
            f"- {_short(b.id)}({b.name}):{b.description or '无描述'};组件 "
            + (", ".join(f"{w.id}/{w.type}" for w in b.layout) or "无")
            + f";Web {dashboard_resource_url(project.id, b.id)}"
            for b in self.store.list_boards(project.id)) or "(无)"
        guidelines = "\n".join(
            f"- {g.name}:{g.description or '未填写 description'}"
            f"{'[停用]' if not g.enabled else ''}"
            f";Web {guideline_resource_url(project.id, g.name)}"
            for g in project.guidelines) or "(无)"
        skills = "\n".join(
            f"- {s.id}({s.name or s.id}){'[停用]' if not s.enabled else ''}"
            f";Web {skill_resource_url(project.id, s.id)}"
            for s in project.skills) or "(无)"
        runtimes = "\n".join(
            f"- {backend.id}: adapter={backend.adapter};"
            f"{'启用' if backend.enabled else '停用'}"
            for backend in self.store.list_backends()) or "(无)"
        return ORCHESTRATOR_TEMPLATE.format(
            max_runs=project.max_chain_runs,
            repos=repos, channels=channels, boards=boards,
            guidelines=guidelines, skills=skills, runtimes=runtimes)

    def _apply_orchestrator_actions(self, project, role_id: str, reply: str,
                                    root_id: int, depth: int) -> str:
        """兼容旧文本块；所有动作都转调统一 Agent Tool registry。"""
        reports = []
        for match in ACTION_RE.finditer(reply):
            try:
                action = json.loads(match.group(1))
                result = self.agent_tools.execute_legacy(
                    project, role_id, action, root_id, depth)
                reports.append(str(result["summary"]))
            except (AgentToolError, TypeError, ValueError,
                    json.JSONDecodeError) as exc:
                reports.append(f"控制动作未执行：{exc}")
        cleaned = ACTION_RE.sub("", reply).strip()
        if reports:
            cleaned = "\n\n".join(x for x in (cleaned, "平台操作：" + "；".join(reports)) if x)
        return cleaned or "(主控动作已处理)"
