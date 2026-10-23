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
- 无主控模式(项目未选择主控):所有角色都有主控级管理权限、能看到名册,但不能
  互相派发;只有人类点名启动角色,同时点名多个角色时各自启动,结果只留在频道。

防失控:项目可配置的单条协作链执行总数上限 + 不响应自己 @ 自己。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
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
from .workspace import (channel_page_context_dir, channel_uploads_dir,
                        chat_workspace_dir, platform_history_dir,
                        prepare_agent_workspace, write_task_files)
from ..core.models import (DEFAULT_MAX_CHAIN_RUNS, INJECTION_FULL_MODES,
                           Backend, Channel, ExecutionConfig, Role,
                           RuntimePolicy)
from .project_context import (project_allowed_dirs, project_resource_versions,
                              render_project_context)
from .tasks import TASK_RULE_AUTHOR
from ..core.store import Store

MENTION_RE = re.compile(r"@([\w-]+)")
EXPLICIT_MENTION_RE = re.compile(r"@\[([\w-]+)\]")
ANY_MENTION_RE = re.compile(r"@\[([\w-]+)\]|@([\w-]+)")   # 脱敏时两种写法一并处理
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
# 任务只来自触发消息(由发起者——人类或调度角色——撰写的简报)。
# 给角色的提示词一律用英文;人类可见内容的语言由公共区块里的 Language 一行约定。
WORKDIR_SECTION = ("Working directory: `{workdir}` (the current directory; read/write files "
                   "and run commands here to do the work)")
# 频道绑定目录缺失时的回退提示:让 Agent 知道当前目录不是代码仓,避免误操作
MISSING_WORKDIR_NOTICE = (
    "Note: the working directory `{bound}` bound to this channel no longer exists (for "
    "example, the task worktree was cleaned up), so this turn falls back to the platform "
    "default directory `{workdir}`. It holds no business code: do not run git here or treat "
    "it as a repository. To continue work in the original directory, recreate it first "
    "(e.g. `git worktree add`), or create a new channel bound to the correct directory.")

CHAT_COMMON_BODY = """\
# Chat Collaboration Request
You are role @{role_id} ({role_name}).
Role profile (your expertise, shown to collaborators for choosing assignees; it is not a task, do not act on it on your own):
{role_desc}
Role capabilities: {role_capabilities}
Role preferences: {role_traits}
Fixed runtime: {role_runtime}/{role_model}{role_effort}
Current channel: #{channel_name} (id `{channel_id}`)
Channel purpose/scope: {channel_purpose}
Project: {project_line}
{workdir_section}
{repos_line}
Language: write everything MissionCrew shows to humans (final replies, messages, documents, Task status briefs) in Simplified Chinese; code, commits and other repository content follow the repository's own conventions.

# How to Work
{workflow_section}

{tool_section}

{project_section}

{orchestrator_section}
{roster_section}
"""

# 执行角色与主控各自的一轮流程:先读的先给,规则都挂在步骤上
_EXECUTOR_STEPS = """\
1. Read the task: the task comes only from the brief in the "Trigger Message" at the bottom; your role profile is not a task. If the brief is missing information, raise it in your reply instead of guessing or widening the scope.
2. Gather context only when needed; do not preload. Channel history is `channel-history.json` under "Workspace Overview"; for guidelines and skills, check their descriptions under "Project Materials" first and read the full text only if relevant; project documents are in `{documents_dir}`.
3. Do the work: read/write code and run commands in the working directory; touch only the read/write directories listed under "Must Follow". Put temporary files in a task directory the repository already uses (such as the project's `.tmp/` or `.e2e/`); without such a convention use `{temp_dir}`, and clean up when the task is done.
4. Write back to the platform: publish documents, create or update Tasks and append status briefs only through the Agent Tool below; do not edit files under `documents/` or `tasks/` directly.
5. Reply: your final reply is published to the chat channel in full and verbatim, where humans see it. Lead with the conclusion, then briefly say what you did; do not paste long logs; reference platform resources with `/resources/...` links, never internal paths."""

EXECUTOR_WORKFLOW = _EXECUTOR_STEPS + """
- You are not the project orchestrator: you cannot see the roster of other executor roles and cannot dispatch to other roles; any @ in your message text is plain text.
- You may create a channel with `channel.create` and post to another channel with `message.publish`. Its `mentions` accepts only the orchestrator `{orchestrator_id}`: use it to bring the orchestrator into another channel (such as one you just created) or, when a human mentioned you directly, to hand it follow-up work; the orchestrator's replies stay in that channel and are not returned to you.
- Submit only the complete result of this task. If the orchestrator dispatched this turn, the platform hands your result back to it automatically; if a human mentioned you directly, the result is only published to the channel and does not trigger the orchestrator, which can still read the full record when a human calls on it later."""

# 仅人工点名的角色:不进 Agent 协作图,有无主控都按执行角色干活,结果只留给人类
MANUAL_ONLY_WORKFLOW = _EXECUTOR_STEPS + """
- You are a human-mention-only role: only humans can mention you and other roles cannot see you; you cannot see the role roster or dispatch to other roles, and any @ in your message text is plain text.
- Submit only the complete result of this task: it is published to the channel for humans and does not trigger any other role."""

ORCHESTRATOR_WORKFLOW = """\
1. Read the request: a human request or an executor's report is this turn's input; its type is on the "This Turn's Trigger" line. When a human mentions several roles at once, the platform starts only you; the full list is in the trigger message JSON's `mentions` and `mention_spans`, and you decide whether to dispatch them in parallel, in sequence, or to different assignees.
2. Choose who and where: pick roles from the "Role Roster" below by profile, capabilities and preferences. Whether to open a new channel follows the project guidelines; without a convention, dispatch in the current channel. Before referencing, dispatching to, or creating another channel, check with `channel.list` (scope=active/archived/all) to avoid duplicate channels.
3. Dispatch: call `message.publish` with `channel` (`{channel_id}` for the current channel) and a `mentions` array of roles; the content states the background, requirements and acceptance criteria. Dispatching always goes through `message.publish` with the target roles listed explicitly in `mentions`; there is no other way.
4. Wrap up and wait for reports: once `message.publish` returns a non-empty `dispatched`, your dispatch duty for this turn is done; say briefly in your final reply that the work was dispatched, then end the turn, and do not call `message.publish` again for that note. When the executor finishes or fails, the platform automatically starts a new orchestrator turn with its full result; review, dispatch further, or summarize then.
5. Review and summarize: check reports against the acceptance criteria, then dispatch further or write the conclusion. Record milestone conclusions in the Task with `task.brief`; state across turns lives there, not in session memory. Run verification commands inside the "Project repositories" directories; the current working directory is not necessarily a repository."""

# 无主控项目:每个角色都有主控级管理权限,但只执行、不派发,由人类点名启动
PEER_WORKFLOW = """\
1. Read the task: this turn's input is the human request (or automation script message) in the "Trigger Message" at the bottom; its type is on the "This Turn's Trigger" line. Your role profile is not a task. If the request is missing information, raise it in your reply instead of guessing or widening the scope.
2. Gather context only when needed; do not preload. Channel history is `channel-history.json` under "Workspace Overview"; for guidelines and skills, check their descriptions under "Project Materials" first and read the full text only if relevant; project documents are in `{documents_dir}`.
3. Do the work: read/write code and run commands in the working directory; touch only the read/write directories listed under "Must Follow". Put temporary files in a task directory the repository already uses; without such a convention use `{temp_dir}`, and clean up when the task is done. You cannot dispatch other roles: when part of the work needs another role, finish what you can and name that role in your reply (see the "Role Roster" below) so a human can mention it.
4. Write back to the platform: publish documents, create or update Tasks and append status briefs only through the Agent Tool below; do not edit files under `documents/` or `tasks/` directly.
5. Reply: your final reply is published to the chat channel in full and verbatim. Lead with the conclusion, then briefly say what you did; do not paste long logs; reference platform resources with `/resources/...` links, never internal paths.
- This project has no orchestrator: every role holds the orchestrator-level management permissions but cannot dispatch other roles. Your result is only published to the channel and triggers no other role; when a human mentions several roles at once, each one starts on its own without waiting for the others."""

DURABLE_CONTEXT_TEMPLATE = """\
# MissionCrew Durable Shared Context
Context version: {context_version}

# Session Compaction Rules
- This block is the authoritative shared input from MissionCrew: role, project, permissions, working directories and collaboration rules.
- When the runtime compacts the context, compact only ordinary conversation, tool activity and task details; keep this block complete and never summarize, trim or rewrite it.
- If this session later receives this block with a different version, the newer version fully replaces the old one; do not keep using or merge old project settings.

{common_body}
"""

TURN_PROMPT = """\
{resource_notice}{reply_hint}# This Turn's Trigger: {trigger_kind}
# Trigger Message (JSON; your task brief is written by the sender)
{trigger}
"""

# 能调用 message.publish 的角色每轮都带:增量回合不重发公共上下文,长会话里
# 「最终回复自动发布」这条规则离得太远,模型会养成先 publish 再回复的重复输出
CONTROL_REPLY_HINT = (
    "# How Your Reply Is Delivered\n"
    "Your output this turn is automatically published as the result to the trigger "
    "message's channel; unless necessary, do not publish the answer again with "
    "`message.publish`, which is only for dispatching or posting to other channels.\n\n"
)

# 准则/Skill 正文与主控清单条目的变化都不改公共上下文版本,只在本轮输入里列差异
RESOURCE_NOTICE = (
    "# MissionCrew Resource Updates\n"
    "Since your last turn the following changed; the shared context version is unchanged:\n"
    "{items}\n\n"
)
_CHANGE_LABELS = {
    "guideline": "Guideline", "skill": "Skill", "role": "Role", "repo": "Repository",
    "channel": "Channel", "board": "Dashboard", "source": "Data source",
    "automation": "Automation", "disabled": "Disabled list",
}
_ROSTER_KINDS = frozenset({"role", "repo", "channel", "board", "source", "automation", "disabled"})
_REMOVED_HINT = {
    "role": "was disabled or deleted; do not dispatch to it", "channel": "was archived or deleted",
    "board": "was deleted", "source": "was deleted", "automation": "was deleted",
    "repo": "was removed",
}


def _change_notice(snapshot: dict[str, tuple[str, str]], seen: dict[str, str]) -> str:
    """比对会话上次看到的版本表,生成本轮差异提示。

    snapshot 的值是 (版本, 展示行):准则/Skill 正文项没有展示行,只报「正文已修改」;
    清单项新增/更新时给出整行,移除时按类型说明。seen 里还没有任何清单项时视为
    基线(旧会话首次带清单),不把整份清单当作新增。
    """
    if not seen:
        return ""
    baseline = not any(key.partition(":")[0] in _ROSTER_KINDS for key in seen)
    lines = []
    for key, (version, line) in snapshot.items():
        kind, _, name = key.partition(":")
        label = _CHANGE_LABELS.get(kind, kind)
        text = line.strip().removeprefix("- ")   # 清单行自带列表符号,提示里去掉
        if key not in seen:
            if line and not baseline and kind != "disabled":
                lines.append(f"- New {label.lower()}: {text}")
        elif seen[key] != version:
            lines.append(f"- Updated {label.lower()}: {text}" if line
                         else f"- {label} `{name}` content changed; re-read it when needed")
    for key in seen:
        kind, _, name = key.partition(":")
        if key not in snapshot and kind in _REMOVED_HINT:
            lines.append(f"- {_CHANGE_LABELS[kind]} `{name}` {_REMOVED_HINT[kind]}")
    return RESOURCE_NOTICE.format(items="\n".join(lines)) if lines else ""


def _compact_records(records: list[dict]) -> str:
    """最近对话 JSON:一条消息一行,省掉缩进,仍是合法 JSON 数组。"""
    if not records:
        return "[]"
    return "[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n]"

RECOVERY_PROMPT = """\
# Recent Conversation (JSON, one message per line)
{history}

{turn_prompt}
"""

# 主控与无主控角色共用的规则条目:配置页协作、按需查询运行、手册指引
_CONFIG_PAGE_RULE = (
    "- Messages from config-page collaboration include the current page, the current item, "
    "unsaved drafts and the user's selection: if the user only asks or discusses, just answer and "
    "do not change the configuration; when explicitly asked to create or modify, persist it with "
    "the corresponding action.\n")
_RUN_QUERY_RULE = (
    "- Active run status is not written into your context automatically; only when a human asks "
    "about runs or asks to stop a role, call `channel.runs.list` for a one-off snapshot and pass "
    "the returned `run_id` to `channel.run.stop`.\n")
_MANUAL_RULE = (
    "- The rules for dashboards, board data sources, guideline and skill formats, document "
    "publishing and renaming, automation scripts and the recycle bin are in the corresponding "
    "manual sections; read them before acting. Dashboards, data sources, automation scripts and "
    "disabled guidelines/skills are not in your workspace: read the current configuration with "
    "the matching `*.list` action (pass the id for full content) before changing one.\n")

# 主控级规则(派发纪律、按需查询、预算),只给有主控项目的主控
CONTROL_RULES_TEMPLATE = """\
# Orchestrator Duties
You are the only orchestrator of this project: you understand the project goals, break down the work and dispatch other roles; every MissionCrew write operation must go through the Agent Tool above.
Discipline:
- Any @roleID or @[roleID] in a message body you send (including your final reply) is plain text and never triggers execution; dispatching works only through the `mentions` of `message.publish`. Do not pass mentions unless you need collaboration; do not dispatch to yourself or invent roles; each role's runtime/model is fixed in the project definition, and you cannot and need not change it.
- After dispatching, do not use `sleep`, do not poll channel history, worktrees or run status, do not take over an executor's task, and do not send `message.publish` to a role that is still running to ask for interim status: a role's persistent session in a channel cannot accept a new turn mid-run, so such requests just queue behind the original task and cannot give live progress. Continue the current turn only if `dispatched` is empty or you still have immediate work that does not depend on the dispatched roles.
- Your output this turn (the final reply) is automatically published in full as this turn's result to the channel of the trigger message: put answers to humans, conclusions, status and anything else you need to include into that one output. Use `message.publish` only when necessary, i.e. to dispatch (with `mentions`) or to post to another channel; do not call `message.publish` with empty `mentions` to post the answer again, or the same result appears twice in the channel.
""" + _CONFIG_PAGE_RULE + """\
On-demand queries:
""" + _RUN_QUERY_RULE + """\
- Other channels are not in your context: to check whether a channel was dispatched or how it is progressing, call `channel.runs.list` / `channel.history` with `channel` once; do not poll on them.
- The channel list is not in your context; query it with `channel.list`. The workdir of `channel.create` must be a project repository path or a subdirectory of one; a new channel starts empty, so post the task brief into it with `message.publish` and name the executor in `mentions`.
""" + _MANUAL_RULE + """\
Collaboration chain budget: a single collaboration chain in this project allows at most {max_runs} agent runs, and the `chain_budget` in the `message.publish` result shows how many are used. This is only a total cap against runaway loops and does not limit dispatch depth; break down, assign, review and drive the work autonomously within the budget.
"""

# 无主控项目:每个角色都有主控级管理权限,但不能互相派发,只由人类点名启动
PEER_RULES = """\
# Collaboration Duties (No-Orchestrator Project)
This project has no orchestrator: every role holds the orchestrator-level management permissions (channels, dashboards, board data sources, guidelines, skills, automations, the recycle bin, run queries), but roles cannot dispatch each other and only humans start roles. Do the work yourself; every MissionCrew write operation must go through the Agent Tool above.
Discipline:
- Any @roleID or @[roleID] in a message body you send (including your final reply) is plain text and never triggers execution, and `message.publish` rejects `mentions` in this project. When the work needs another role, say in your final reply which role should take it and why; a human decides whether to mention it.
- Your output this turn (the final reply) is automatically published in full as this turn's result to the channel of the trigger message: put answers to humans, conclusions, status and anything else you need to include into that one output. Use `message.publish` only to post to another channel; do not call it to post the answer again, or the same result appears twice in the channel.
""" + _CONFIG_PAGE_RULE + """\
On-demand queries:
""" + _RUN_QUERY_RULE + """\
- Other channels are not in your context: to see how work in another channel is going, call `channel.runs.list` / `channel.history` with `channel` once; do not poll on them.
- The channel list is not in your context; query it with `channel.list`. The workdir of `channel.create` must be a project repository path or a subdirectory of one; a new channel starts empty, and a human mentions the roles that should work there.
""" + _MANUAL_RULE

# 主控的项目清单:附在公共区块末尾但不参与版本哈希,差异经本轮输入提示
ROSTER_HEADER = (
    "# Project Inventory\n"
    "Live data, not part of the shared context version; when entries are added, updated, "
    "archived or disabled, this turn's input lists the differences.\n")


def _role_line(r: Role) -> str:
    """角色名册一行:能力、偏好、定位分段标注,供主控选人。能力用英文 id,与身份段一致。"""
    desc = " ".join((r.description or "").split()) or "not provided"
    return (f"- @{r.id} {r.name} · capabilities {'/'.join(r.capabilities) or 'unspecified'}"
            f" · preferences {r.preference or 'unspecified'} · profile {desc}")


def _trigger_kind(record: dict, orchestrator_id: str,
                  is_orchestrator: bool) -> str:
    """一句话说明本轮触发是谁发的,主控据此区分人类请求与执行角色回报。"""
    author = record.get("author") or {}
    kind, author_id = str(author.get("type") or ""), str(author.get("id") or "")
    if kind == "human":
        text = "human message"
    elif kind == "agent":
        if is_orchestrator and record.get("mention_spans"):
            # 执行角色经 message.publish 显式提及主控,是新请求而非结果回报
            text = (f"request from executor @{author_id}, who mentioned you explicitly; "
                    "handle it like a new request")
        elif is_orchestrator:
            text = f"report from executor @{author_id}; review it"
        elif author_id == orchestrator_id:
            text = f"task dispatched by orchestrator @{author_id}"
        else:
            text = f"message from role @{author_id}"
    elif kind == "automation" and author_id == TASK_RULE_AUTHOR:
        # 规则由人在任务看板列头配置,不是项目清单里的自动化脚本
        text = "task dispatched by the project's automatic task-handling rule"
    elif kind == "automation":
        text = f"message posted by automation script {author_id}"
    else:
        text = f"{kind or 'platform'} message"
    depth = int((record.get("thread") or {}).get("depth") or 0)
    return text + (f" (collaboration chain depth {depth})" if depth else "")


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
        self._agent_execution_locks_guard = threading.Lock()
        self._agent_execution_locks: dict[tuple[str, str], threading.Lock] = {}
        # 频道停止与运行发布共用同一把锁：停止先赢时禁止迟到回复和后续调度，
        # 回复先赢时停止操作会连同刚产生的后续运行一起捕获。
        self._run_state_lock = threading.RLock()
        self._stopping_channels: set[str] = set()
        self._interaction_lock = threading.Lock()
        self._interactions: dict[str, _PendingInteraction] = {}
        self.agent_tools = AgentActionService(
            store, self.post, self.stop_run,
            history_records=self.channel_history_records,
            stop_channel_sessions=self.stop_channel_sessions)
        # 正在更新的 runtime 集合(由 server 注入共享):更新期间不派发执行,
        # 避免 Agent 跑在半更新的二进制上
        self.updating_backends: set[str] = set()
        # Claude 后台命令结束后 CLI 会自发唤醒模型汇报;平台把这类 turn
        # 落成频道里的新运行,汇报按常规回路交回主控验收。turn 开始时同步
        # 落成运行并签发令牌,turn 结束后在执行池里收尾
        runtime_manager.set_wake_handler(
            self._handle_runtime_wake, begin=self._begin_runtime_wake)

    @staticmethod
    def _session_key(channel: Channel, role_id: str) -> str:
        base = f"{channel.id}::{role_id}"
        if channel.context_start_message_id:
            return f"{base}::context-{channel.context_start_message_id}"
        return base

    def _agent_execution_lock(self, channel_id: str,
                              role_id: str) -> threading.Lock:
        """同一 channel×role 只允许一个 Run 装配并持有 Agent Tool capability。"""
        key = (channel_id, role_id)
        with self._agent_execution_locks_guard:
            return self._agent_execution_locks.setdefault(key, threading.Lock())

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
             origin_run_id: Optional[int] = None,
             kind: str = "message") -> int:
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
        orchestrator_role = (
            self.store.get_role(channel.project_id or "", orchestrator)
            if orchestrator else None
        )
        orchestrator_available = bool(
            orchestrator_role and orchestrator_role.enabled)
        role = (self.store.get_role(channel.project_id or "", author)
                if author_type == "agent" else None)

        legal_spans: list[dict] = []
        # Agent 派发只认 message.publish 显式命令传入的结构化范围;正文里的任何 @
        # （含 @[role] 旧语法）都是普通文字。主控可派发任意角色;有主控时执行角色
        # 只能提及主控,最终回复把完整结果交回主控,避免横向看见或调用名册;无主控
        # 时角色不能互相派发,回复只留在频道。
        can_dispatch = (author_type == "agent" and bool(project)
                        and project.can_dispatch(author))
        if author_type == "agent":
            mentions = []
            if mention_spans and project and not self._manual_only_role(project, role):
                mentions, legal_spans = self._validate_mention_spans(
                    content, mention_spans, channel.project_id or "",
                    by_agent=True)
                # 剔除自己与无权提及的目标(agent_tools 已先拒绝,这里是引擎兜底)
                allowed = {item for item in mentions
                           if item != author and project.can_mention(author, item)}
                mentions = [item for item in mentions if item in allowed]
                legal_spans = [span for span in legal_spans
                               if span["role_id"] in allowed]
            if (not mentions and origin_run_id is None and author != orchestrator
                    and orchestrator_available
                    and not self._is_direct_human_dispatch(reply_to, author)):
                # 执行角色的最终回复(含唤醒汇报)交回主控;运行中经 message.publish
                # 发出、不带 mentions 的消息(origin_run_id 非空)只发布不触发
                mentions = [orchestrator]
            if role:
                runtime_id = role.runtime_id if runtime_id is None else runtime_id
                model = role.model if model is None else model
                effort = role.effort if effort is None else effort
        elif author_type == "human":
            # Web 明确传空数组时，正文里的 @xxx 仍是普通文本；人类 CLI 调用
            # 若省略结构化范围，可使用更明确的 @[role] 语法。
            if mention_spans is None:
                content, mentions, legal_spans = self._parse_explicit_mentions(
                    content, channel.project_id or "")
            else:
                mentions, legal_spans = self._validate_mention_spans(
                    content, mention_spans, channel.project_id or "")
        elif author_type == "automation":
            # 自动化脚本消息:提及规则与人类一致(单角色直达、多角色收敛主控),
            # 但没有"无提及默认交给主控"——脚本不显式点名就只发消息不触发。
            if mention_spans:
                mentions, legal_spans = self._validate_mention_spans(
                    content, mention_spans, channel.project_id or "")
            else:
                mentions = []
        else:
            # 平台消息只用于展示状态和操作回执，不解析提及，也不触发角色。
            mentions = []
        if not mentions and author_type == "human" and channel.project_id:
            if project and not project.has_orchestrator:
                # 无主控项目只靠人类点名启动角色,不点名的消息谁也不会处理;
                # 直接拒收,免得人类以为发出去就有人接手。
                raise ValueError("本项目没有主控，请先 @ 至少一个角色再发送")
            if orchestrator_available and author != orchestrator:
                mentions = [orchestrator]
        dispatch_targets = list(mentions)
        if (author_type in ("human", "automation") and len(mentions) > 1
                and orchestrator_available and author != orchestrator):
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
                kind=kind, mention_spans=legal_spans, context=context)
            if can_dispatch and not legal_spans:
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
        """判断角色是否由人类或自动化脚本在触发消息中直接选择。

        只看可信的落库 mentions，不解析正文中的普通 ``@role``。这样主控派发
        的执行结果仍会自动回传，而人类/脚本直接点名角色后的结果只保留在
        频道中,不再自动触发主控。
        """
        if trigger_message_id is None:
            return False
        trigger = self.store.get_message(trigger_message_id)
        return bool(
            trigger
            and trigger.get("author_type") in ("human", "automation")
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

    def shutdown(self, wait: bool = True) -> None:
        """关闭执行池;wait=True 时先等已派发的运行(含级联)全部结束。

        服务停机与测试里 TestClient 退出都必须经这里收尾,否则工作线程会跑到
        宿主生命周期之外,继续按当时的环境变量定位并改写频道历史。
        """
        try:
            if wait:
                self.wait_idle()
        finally:
            self._pool.shutdown(wait=wait)

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
        targets = self._channel_runtime_targets(channel)
        # 清除上下文/归档时还要覆盖尚未来得及保存 chat_session 的实例。
        # 频道 stop 自身会从活动 run 补齐这个窄竞态，不需要终止无关角色。
        for role in self.store.list_roles(channel.project_id or ""):
            backend = self.store.get_backend(role.runtime_id)
            if backend is not None:
                targets[(backend.id, self._session_key(channel, role.id))] = backend

        stopped = 0
        for (_backend_id, session_key), backend in targets.items():
            stopped += runtime_manager.stop(backend, session_key)
        return stopped

    def _channel_runtime_targets(self, channel: Channel,
                                 runs: Optional[list[dict]] = None) -> dict:
        """收集频道已保存会话与活动 run 对应的精确 Runtime 实例。"""
        targets: dict[tuple[str, str], object] = {}
        for session in self.store.chat_sessions_for_channel(channel.id):
            backend = self.store.get_backend(session["backend_id"])
            if backend is not None:
                targets[(backend.id, session["session_key"])] = backend
        for run in runs or []:
            backend = self.store.get_backend(run.get("backend_id") or "")
            if backend is not None:
                targets[(backend.id, self._session_key(channel, run["role_id"]))] = backend
        return targets

    def stop_channel_agents(self, channel_id: str) -> dict:
        """停止频道运行并终止对应 Runtime 进程，保留原生 session id。"""
        channel = self.store.get_channel(channel_id)
        if channel is None:
            raise ValueError(f"频道不存在: {channel_id}")

        with self._run_state_lock:
            runs = self.store.stop_active_chat_runs(channel_id)
            self._stopping_channels.add(channel_id)
            run_ids = {run["id"] for run in runs}
            for run in runs:
                self.store.append_run_event(
                    run["id"], "status", "用户已停止当前频道中的 Agent 运行")

            # 等待权限或用户输入的回调必须先释放，否则 Runtime 进程终止前
            # 可能继续阻塞在 MissionCrew 的交互桥上。
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

            # 已保存 session 允许在 chat_runs 已无活动项时再次清理孤儿进程；
            # 活动 run 快照补齐 Runtime 尚未来得及保存 session 的启动窗口。
            targets = self._channel_runtime_targets(channel, runs)

        # 终止进程可能等待 reader/协议线程退出，不能占着运行状态锁；否则
        # 同一 Runtime 的交互回调无法观察到 stopped 并及时返回 cancel。
        stopped = runtime_errors = 0
        marker_id = 0
        try:
            for (_backend_id, session_key), backend in targets.items():
                try:
                    stopped += runtime_manager.stop(backend, session_key)
                except Exception as exc:
                    runtime_errors += 1
                    self.store.audit(
                        "platform", "chat_stop_runtime_failed",
                        detail=(f"channel={channel_id} backend={backend.id} "
                                f"session={session_key} error={exc}"),
                    )

            with self._run_state_lock:
                if runs or stopped or runtime_errors:
                    content = (f"已停止当前频道中的 {len(runs)} 个 Agent 运行"
                               if runs else "当前频道没有活动 Agent 运行")
                    if stopped:
                        content += f"，并终止 {stopped} 个 Runtime 进程"
                    if runtime_errors:
                        content += f"；其中 {runtime_errors} 个 Runtime 终止请求失败，请检查运行状态"
                    marker_id = self.store.add_message(
                        channel_id, "platform", "platform", content + "。", [],
                        kind="agent_stop",
                    )
                    self._write_channel_history(channel)
        finally:
            with self._run_state_lock:
                self._stopping_channels.discard(channel_id)
        if runs or stopped or runtime_errors:
            self.store.audit(
                "human", "chat_agents_stopped",
                detail=(f"channel={channel_id} runs={len(runs)} "
                        f"interrupted=0 stopped={stopped} "
                        f"errors={runtime_errors} marker={marker_id}"),
            )
        result = {
            "ok": True,
            "stopped_runs": len(runs),
            "interrupted_runtimes": 0,
            "stopped_runtimes": stopped,
            "runtime_errors": runtime_errors,
        }
        if marker_id:
            result["marker_id"] = marker_id
        return result

    def stop_run(self, run_id: int, *, actor: str = "human",
                 actor_label: str = "用户") -> dict:
        """停止单个运行并终止其角色的 Runtime 实例,不影响频道内其他角色。

        与频道级 stop 相同:先原子翻终态并释放交互回调,再在锁外终止
        Runtime 进程;原生 session id 保留,可以续接。
        """
        with self._run_state_lock:
            run = self.store.stop_chat_run(run_id)
            if run is None:
                raise ValueError("运行不存在或已结束")
            channel = self.store.get_channel(run["channel"])
            actor_prefix = actor_label.strip() + (" " if actor != "human" else "")
            stopped_reason = f"{actor_prefix}已停止本次 Agent 运行"
            self.store.append_run_event(run_id, "status", stopped_reason)

            with self._interaction_lock:
                for pending in self._interactions.values():
                    if pending.run_id != run_id:
                        continue
                    pending.stopped = True
                    pending.response = {
                        "decision": "cancel", "answers": {},
                        "reason": stopped_reason,
                    }
                    pending.ready.set()

            # 只定位该 run 已记录的 Runtime 实例。排队 Run 尚无 backend_id，
            # 此时只取消队列项，不能根据同角色旧会话误停另一个执行中的 Run。
            targets: dict[tuple[str, str], Backend] = {}
            if channel is not None:
                backend = self.store.get_backend(run.get("backend_id") or "")
                if backend is not None:
                    targets[(backend.id, self._session_key(channel, run["role_id"]))] = backend

        stopped = runtime_errors = 0
        for (_backend_id, session_key), backend in targets.items():
            try:
                stopped += runtime_manager.stop(backend, session_key)
            except Exception as exc:
                runtime_errors += 1
                self.store.audit(
                    "platform", "chat_stop_runtime_failed",
                    detail=(f"channel={run['channel']} backend={backend.id} "
                            f"session={session_key} error={exc}"),
                )

        marker_id = 0
        if channel is not None:
            with self._run_state_lock:
                content = f"{actor_prefix}已停止 @{run['role_id']} 的本次运行"
                if stopped:
                    content += f"，并终止 {stopped} 个 Runtime 进程"
                if runtime_errors:
                    content += f"；其中 {runtime_errors} 个 Runtime 终止请求失败，请检查运行状态"
                marker_id = self.store.add_message(
                    channel.id, "platform", "platform", content + "。", [],
                    kind="agent_stop",
                )
                self._write_channel_history(channel)
        self.store.audit(
            actor, "chat_run_stopped",
            detail=(f"channel={run['channel']} run={run_id} "
                    f"role={run['role_id']} stopped={stopped} "
                    f"errors={runtime_errors} marker={marker_id}"),
        )
        return {
            "ok": True,
            "stopped_runs": 1,
            "role_id": run["role_id"],
            "stopped_runtimes": stopped,
            "runtime_errors": runtime_errors,
            "marker_id": marker_id,
        }

    # ---- 内部:可信提及、触发与执行 ----
    def _parse_explicit_mentions(self, content: str,
                                 project_id: str) -> tuple[str, list[str], list[dict]]:
        """把人类 CLI 正文的 ``@[role]`` 归一化成可见 ``@role`` 与精确范围。

        仅服务无选择器的人类 CLI 入口;Agent 消息不经过本函数——Agent 的
        派发只能来自 message.publish 显式命令构造的结构化范围。
        """
        known = {role.id: role for role in self.store.list_roles(project_id)}
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
            role = known.get(role_id)
            if role is None:
                raw = match.group(0)
                parts.append(raw)
                output_length += len(raw)
            elif not role.enabled:
                raise ValueError(f"角色不存在或不可用: @{role_id}")
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
                                project_id: str, by_agent: bool = False
                                ) -> tuple[list[str], list[dict]]:
        """验证 UI 选择器给出的 Unicode code-point 范围，不从正文猜目标。

        ``by_agent`` 是 Agent 经 message.publish 派发:仅人工点名的角色对它
        视同不存在(agent_tools 已先拦一道,这里是引擎入口的兜底)。
        """
        known = {role.id: role for role in self.store.list_roles(project_id)}
        normalized: list[dict] = []
        for item in requested:
            if not isinstance(item, dict):
                raise ValueError("提及必须由角色选择器生成")
            role_id = item.get("role_id")
            start, end = item.get("start"), item.get("end")
            role = known.get(role_id) if isinstance(role_id, str) else None
            if (not isinstance(role_id, str) or role is None or not role.enabled
                    or (by_agent and role.manual_only)
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
        role = self.store.get_role(channel.project_id or "", role_id)
        if role is not None and not role.enabled:
            self.store.add_message(
                channel.id, "platform", "platform",
                f"@{role_id} 不存在，本次不触发执行。",
                [], msg_id, root_id, depth,
            )
            self._write_channel_history(channel)
            return
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
        try:
            future = self._pool.submit(self._execute, run_id, channel, role_id,
                                       msg_id, root_id, depth)
        except RuntimeError as e:   # 执行池已关闭:运行不能停留在 queued
            self.store.update_chat_run(run_id, "failed", error=f"聊天引擎已关闭: {e}")
            return
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
        """公开执行失败;有主控时只把主控派发的失败交回主控,无主控时只留在频道。"""
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
        orchestrator_role = (
            self.store.get_role(channel.project_id or "", orchestrator)
            if orchestrator else None
        )
        if (orchestrator_role and orchestrator_role.enabled
                and role_id != orchestrator
                and not self._is_direct_human_dispatch(msg_id, role_id)):
            self._trigger(channel, orchestrator, failure_id, root_id, result_depth)

    def _handle_runtime_wake(self, payload: dict) -> None:
        """Runtime 协议线程的回调:移交执行池处理,不阻塞 stdout 读取。"""
        try:
            self._pool.submit(self._process_runtime_wake, payload)
        except RuntimeError:
            pass   # 引擎已关闭,丢弃迟到的唤醒

    def _resolve_wake_session(self, payload: dict
                              ) -> Optional[tuple[Channel, Role, Optional[Backend], str]]:
        """会话按 channel×role 复用,session_key 能唯一定位汇报应落入的
        频道与角色;会话不存在、频道已归档或角色停用时唤醒被静默丢弃。"""
        session_key = str(payload.get("session_key") or "")
        session = (self.store.get_chat_session(session_key)
                   if session_key else None)
        if not session:
            return None
        channel = self.store.get_channel(str(session["channel"]))
        if channel is None or channel.archived:
            return None
        role = self.store.get_role(channel.project_id or "", str(session["role_id"]))
        if role is None or not role.enabled:
            return None
        backend_id = str(payload.get("backend_id")
                         or session["backend_id"] or "")
        return channel, role, self.store.get_backend(backend_id), backend_id

    @staticmethod
    def _wake_tasks(payload: dict) -> list[dict]:
        return [task for task in payload.get("tasks") or []
                if isinstance(task, dict)]

    def _open_wake_run(self, channel: Channel, role: Role,
                       backend: Optional[Backend], backend_id: str,
                       tasks: list[dict]) -> Optional[tuple[int, int]]:
        """以一条平台消息为触发锚点落成运行;频道正在停止时不再开新运行。"""
        described = "、".join(f"`{task['description']}`" for task in tasks
                             if task.get("description"))
        trigger_text = (f"@{role.id} 的后台命令已结束"
                        + (f"：{described}" if described else "")
                        + "，以下是其自动汇报。")
        with self._run_state_lock:
            if channel.id in self._stopping_channels:
                return None
            trigger_id = self.store.add_message(
                channel.id, "platform", "platform", trigger_text, [])
            run_id = self.store.add_chat_run(
                channel.id, role.id, trigger_id, trigger_id, 0)
            self.store.update_chat_run(
                run_id, "running", backend_id=backend_id,
                model=(backend.model if backend else "") or "",
                effort=role.effort or "")
        self._write_channel_history(channel)
        return trigger_id, run_id

    def _run_event_emitter(self, run_id: int, project, document_roots: list):
        """运行过程事件的落库入口:运行结束后丢弃迟到事件,含文档路径的
        文本改写成平台资源 URL。"""
        markers = [str(root) for root in document_roots if str(root)]

        def _emit(kind: str, text: str):
            if not self.store.chat_run_is_active(run_id):
                return None
            may_contain_document_path = (
                ".missioncrew" in text
                or any(marker in text for marker in markers))
            if (project and may_contain_document_path
                    and kind in {"text", "stdout", "stderr", "tool", "tool_result"}):
                text = normalize_document_resource_urls(
                    text, project.id, document_roots)
            return self.store.append_run_event(run_id, kind, text)
        return _emit

    def _begin_runtime_wake(self, payload: dict) -> Optional[dict]:
        """Runtime 协议线程的同步回调:自唤醒 turn 一开始就落成运行、签发
        绑定该运行的 Agent Tool 令牌,并返回实时事件接收器。

        令牌在常规运行结束时随 deactivate_run_token 撤销删除,而唤醒 turn
        在 CLI 进程里先于平台知晓就开始执行;不在这里补发,turn 内的
        平台写操作(发布文档等)会因缺少令牌全部失败。"""
        resolved = self._resolve_wake_session(payload)
        if resolved is None:
            return None
        channel, role, backend, backend_id = resolved
        opened = self._open_wake_run(
            channel, role, backend, backend_id, self._wake_tasks(payload))
        if opened is None:
            return None
        trigger_id, run_id = opened
        project = self.store.get_project(channel.project_id or "")
        document_roots = [library_for(channel.project_id or "").root]
        try:
            if project:
                workspace_root = chat_workspace_dir(project.id, channel.id, role.id)
                document_roots.append(workspace_root / "documents")
                library_for(project.id).commit_changes(
                    "platform", "Capture external document changes before chat run")
                self.agent_tools.ensure_token_file(
                    project, channel, role.id, workspace_root, run_id=run_id)
        except Exception as exc:
            with self._run_state_lock:
                self.store.update_chat_run(
                    run_id, "failed", backend_id=backend_id,
                    error=f"唤醒运行准备失败: {exc}")
            self.store.audit("platform", "chat_wake_begin_failed",
                             detail=f"channel={channel.id} role={role.id} "
                                    f"run={run_id} error={exc}")
            return None
        self.store.audit("platform", "chat_wake_begin",
                         detail=f"channel={channel.id} role={role.id} "
                                f"backend={backend_id} run={run_id}")
        return {"run_id": run_id, "trigger_id": trigger_id,
                "emit": self._run_event_emitter(run_id, project, document_roots)}

    def _process_runtime_wake(self, payload: dict) -> None:
        """把 Runtime 自唤醒 turn(后台命令结束后的自动汇报)落成/收尾运行。

        带 run_id 的 payload 对应 _begin_runtime_wake 已落成的运行,事件已
        实时写入,这里只收尾;否则(ACP 静默判定、begin 未能落成)以一条
        平台消息为触发锚点新建运行并回放缓冲事件。汇报走常规 Agent 回复
        回路(非主控的结果自动交回主控)。"""
        run_id = int(payload.get("run_id") or 0)
        if run_id > 0:
            self._finish_wake_run(run_id, payload)
            return
        resolved = self._resolve_wake_session(payload)
        if resolved is None:
            return
        channel, role, backend, backend_id = resolved
        output = str(payload.get("output") or "").strip()
        if not output:
            return
        tasks = self._wake_tasks(payload)
        opened = self._open_wake_run(channel, role, backend, backend_id, tasks)
        if opened is None:
            return
        trigger_id, run_id = opened
        project = self.store.get_project(channel.project_id or "")
        document_roots = [library_for(channel.project_id or "").root]
        emit = self._run_event_emitter(run_id, project, document_roots)
        # 回放自唤醒 turn 缓冲的过程事件,运行卡片与常规运行一致
        for item in payload.get("events") or []:
            try:
                kind, text = item
            except (TypeError, ValueError):
                continue
            emit(str(kind), str(text))
        self._publish_wake_reply(channel, role, backend, backend_id, project,
                                 run_id, trigger_id, output, tasks)

    def _finish_wake_run(self, run_id: int, payload: dict) -> None:
        """收尾 begin 阶段落成的唤醒运行:提交文档改动、发布汇报或记失败,
        并撤销本轮令牌。"""
        run = self.store.get_chat_run(run_id)
        if run is None:
            return
        channel = self.store.get_channel(str(run["channel"]))
        role_id = str(run["role_id"])
        try:
            if channel is None:
                return
            role = self.store.get_role(channel.project_id or "", role_id)
            if role is None:
                return
            project = self.store.get_project(channel.project_id or "")
            backend_id = str(run.get("backend_id") or payload.get("backend_id") or "")
            backend = self.store.get_backend(backend_id)
            trigger_id = int(run["trigger_message_id"])
            tasks = self._wake_tasks(payload)
            if project:
                library = library_for(project.id)
                revision = library.commit_changes(
                    f"role:{role.id}", f"Documents updated from channel {channel.name}")
                if revision:
                    self.store.audit(f"role:{role.id}", "documents_committed",
                                     detail=f"project={project.id} revision={revision[:10]}")
                tasks_dir = chat_workspace_dir(project.id, channel.id, role.id) / "tasks"
                if tasks_dir.is_dir():
                    write_task_files(self.store, project.id, tasks_dir)
            output = str(payload.get("output") or "").strip()
            success = bool(payload.get("success", True))
            if output and success:
                self._publish_wake_reply(channel, role, backend, backend_id, project,
                                         run_id, trigger_id, output, tasks)
                return
            # 与常规运行一致:进程退出不等于协作完成,失败或无输出都补一条
            # 平台消息,并按本轮由谁发起决定交回对象
            error = str(payload.get("error") or "").strip()
            if not error:
                error = ("Runtime 正常退出但未产生可回传输出" if success
                         else "Runtime 异常退出但未产生可回传输出")
            if project:
                error = normalize_document_resource_urls(
                    error, project.id, [library_for(project.id).root])
            anchor, root, depth = self._wake_reply_anchor(
                project, role.id, trigger_id, tasks)
            with self._run_state_lock:
                if not self.store.chat_run_is_active(run_id):
                    return
                try:
                    self._post_failure(
                        channel, role.id, anchor, root, depth - 1,
                        f"@{role.id}(后端 {backend_id})后台命令汇报失败: {error}")
                finally:
                    self.store.update_chat_run(
                        run_id, "failed", backend_id=backend_id, error=error)
        finally:
            if channel is not None and channel.project_id:
                self.agent_tools.deactivate_run_token(
                    channel.project_id, channel.id, role_id,
                    chat_workspace_dir(channel.project_id, channel.id, role_id),
                    run_id)

    def _wake_reply_anchor(self, project, role_id: str, trigger_id: int,
                           tasks: list[dict]) -> tuple[int, int, int]:
        """唤醒汇报继承"启动后台任务的那轮"的派发语义:该轮由人类直接点名
        时,汇报挂回原人类消息(post 会因此不再交回主控);否则按常规
        Agent 回复回路交回主控验收。无主控项目没有交回对象,一律挂回原触发
        消息。"""
        origin_trigger = next(
            (int(task.get("origin_trigger") or 0) for task in tasks
             if task.get("origin_trigger")), 0)
        origin = (self.store.get_message(origin_trigger)
                  if origin_trigger else None)
        if origin and ((project and not project.has_orchestrator)
                       or self._is_direct_human_dispatch(origin_trigger, role_id)):
            return (origin_trigger, int(origin.get("root_id") or origin_trigger),
                    int(origin.get("depth") or 0) + 1)
        return trigger_id, trigger_id, 1

    def _publish_wake_reply(self, channel: Channel, role: Role,
                            backend: Optional[Backend], backend_id: str,
                            project, run_id: int, trigger_id: int,
                            output: str, tasks: list[dict]) -> None:
        document_roots = [library_for(channel.project_id or "").root]
        reply = output
        if project and project.controls_platform(role.id):
            reply = self._apply_orchestrator_actions(
                project, role.id, reply, root_id=trigger_id, depth=0)
        elif ACTION_RE.search(reply):
            reply = ACTION_RE.sub("", reply).strip()
            reply += "\n\n(检测到平台控制动作,但只有项目主控可以执行,未生效)"
        if project:
            reply = normalize_document_resource_urls(
                reply, project.id, document_roots)
        reply_anchor, reply_root, reply_depth = self._wake_reply_anchor(
            project, role.id, trigger_id, tasks)
        with self._run_state_lock:
            if not self.store.chat_run_is_active(run_id):
                return
            self.store.remove_duplicate_reply_output(run_id, reply)
            self.post(channel.id, role.id, reply, author_type="agent",
                      reply_to=reply_anchor, root_id=reply_root,
                      depth=reply_depth, runtime_id=backend_id,
                      model=backend.model if backend else None,
                      effort=role.effort or None)
            self.store.update_chat_run(run_id, "done", backend_id=backend_id)
        self.store.audit("platform", "chat_wake",
                         detail=f"channel={channel.id} role={role.id} "
                                f"backend={backend_id} tasks={len(tasks)}")

    def _execute_inner(self, run_id: int, channel: Channel, role_id: str,
                       msg_id: int, root_id: int, depth: int) -> None:
        with self._agent_execution_lock(channel.id, role_id):
            try:
                self._execute_inner_serialized(
                    run_id, channel, role_id, msg_id, root_id, depth)
            finally:
                if channel.project_id:
                    self.agent_tools.deactivate_run_token(
                        channel.project_id, channel.id, role_id,
                        chat_workspace_dir(channel.project_id, channel.id, role_id),
                        run_id)

    def _execute_inner_serialized(self, run_id: int, channel: Channel,
                                  role_id: str, msg_id: int,
                                  root_id: int, depth: int) -> None:
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

        # 转入 running 时盖章执行组合(模型/推理力度),运行卡片实时展示
        if not self.store.update_chat_run(
                run_id, "running", backend_id=backend.id,
                model=backend.model or "", effort=role.effort or ""):
            return
        self.store.audit("platform", "chat_dispatch",
                         detail=f"channel={channel.id} role={role_id} "
                                f"backend={backend.id} depth={depth} {trace}")

        cfg = self._assemble(channel, role, backend, msg_id, run_id=run_id)
        project = self.store.get_project(channel.project_id or "")
        library = library_for(channel.project_id or "")
        document_roots = [
            library.root, cfg.env.get("MISSIONCREW_DOCUMENTS_DIR", "")]
        # 运行过程(思考/工具/输出)实时落库,前端在聊天流中内联展示
        cfg.emit = self._run_event_emitter(run_id, project, document_roots)
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
            if project and project.controls_platform(role.id):
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
            # 事件 commit 后立即对轮询方可见,落库与注册必须同在交互锁内;
            # 否则应答方可能抢在注册之前调 respond_interaction 而误报请求不存在。
            with self._interaction_lock:
                event_id = self.store.append_interaction_event(
                    run_id, kind, visible)
                pending = _PendingInteraction(
                    run_id=run_id, backend_id=backend_id, kind=kind,
                    event_id=event_id, payload=visible)
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

    @staticmethod
    def _resolve_workdir(channel: Channel) -> tuple[Path, Optional[Path]]:
        """频道绑定目录缺失(如任务 worktree 已清理)时回退到平台默认目录,并返回
        缺失的绑定路径供上下文提示。不能静默 mkdir 重建绑定目录:主仓内重建的
        空目录会让 Agent 的 git 命令落到主仓当前分支。"""
        default = mc_home() / "channels" / channel.id
        bound = Path(channel.workdir) if channel.workdir else None
        if bound is not None and bound.is_dir():
            return bound, None
        default.mkdir(parents=True, exist_ok=True)
        return default, bound

    def _assemble(self, channel: Channel, role: Role, backend, msg_id: int,
                  run_id: int = 0) -> ExecutionConfig:
        workdir, missing_workdir = self._resolve_workdir(channel)
        workdir_section = WORKDIR_SECTION.format(workdir=workdir)
        if missing_workdir is not None:
            workdir_section += "\n" + MISSING_WORKDIR_NOTICE.format(
                bound=missing_workdir, workdir=workdir)

        project_section = ""
        tool_section = ""
        orchestrator_section = ""
        project = None
        workspace = None
        env = {}
        allowed_dirs = []
        agent_action = None
        manual_path = "(no workspace for this run)"
        project_line = "(no project)"
        repos_line = ""
        documents_dir = "(no workspace for this run)"
        temp_dir = "a location in the current working directory that follows project conventions"
        if channel.project_id:
            project = self.store.get_project(channel.project_id)
            if project:
                library = library_for(project.id)
                workspace, _ = prepare_agent_workspace(
                    self.store, project, library,
                    chat_workspace_dir(project.id, channel.id, role.id),
                    has_history=True)
                project_section = render_project_context(
                    project, library, workspace.root,
                    extra_dirs=runtime_manager.private_dirs(backend))
                env["MISSIONCREW_WORKSPACE"] = str(workspace.root)
                env["MISSIONCREW_DOCUMENTS_DIR"] = str(workspace.documents)
                env["MISSIONCREW_DOCUMENTS_URL"] = document_resource_url(project.id)
                env["MISSIONCREW_PROJECT_URL"] = missioncrew_project_url(project.id)
                env["MISSIONCREW_GUIDELINES_DIR"] = str(workspace.guidelines)
                env["MISSIONCREW_SKILLS_DIR"] = str(workspace.skills)
                env["MISSIONCREW_TASKS_DIR"] = str(workspace.tasks)
                env["MISSIONCREW_MANUAL"] = str(workspace.manual)
                env["MISSIONCREW_PLATFORM_DOCS_DIR"] = str(workspace.platform_docs)
                manual_path = str(workspace.manual)
                project_line = project.name + (
                    f" · {' '.join(project.description.split())}" if project.description else "")
                repos_line = ("Project repositories: " + "; ".join(
                    f"{r.name or r.id} `{r.path or r.remote or 'no local path configured'}`"
                    for r in project.repos)) if project.repos else ""
                documents_dir = str(workspace.documents)
                temp_dir = str(workspace.root / "temp")
                token_file, token_id = self.agent_tools.ensure_token_file(
                    project, channel, role.id, workspace.root, run_id=run_id)
                tool_url = default_agent_tool_url()
                env["MISSIONCREW_AGENT_TOOL_URL"] = tool_url
                env["MISSIONCREW_AGENT_TOKEN_FILE"] = str(token_file)
                env["MISSIONCREW_AGENT_TOOL_PYTHON"] = sys.executable
                env["MISSIONCREW_CHANNEL_ID"] = channel.id
                allowed_actions = self.agent_tools.allowed_actions(project, role.id)
                # 进程内 Agent Tool 句柄:与 HTTP 入口同一鉴权/审计路径,供
                # 无法起子进程调 CLI 的适配器(如 MockAdapter)执行显式命令。
                identity = AgentIdentity(
                    token_id=token_id, project_id=project.id,
                    channel_id=channel.id, role_id=role.id,
                    issued_scopes=tuple(allowed_actions), run_id=run_id)
                agent_action = (
                    (lambda action, arguments, _identity=identity:
                        self.agent_tools.execute(
                            _identity, action, arguments, run_id=run_id,
                            request_id=f"inproc-{uuid.uuid4().hex}"))
                    if run_id else None)
                tool_section = (
                    "# MissionCrew Agent Tool\n"
                    "Platform write operations (documents, Tasks, messages, channels, dashboards, "
                    "guidelines, skills, etc.) must explicitly call this tool. Commands return JSON "
                    "and exit non-zero on failure; read error.code/error.message, fix the call and "
                    "retry within the current turn; do not bypass the API by writing to "
                    "documents/ or tasks/ directly.\n"
                    f"Python: `{sys.executable}`\n"
                    f"API: `{tool_url}`\n"
                    f"Token file: `{token_file}` (do not read, print or send its contents; "
                    "the token is bound to the current run, so do not pass `--run-id`)\n"
                    "List actions: `\"$MISSIONCREW_AGENT_TOOL_PYTHON\" -m "
                    "missioncrew.agent_tool actions`\n"
                    "Call: `\"$MISSIONCREW_AGENT_TOOL_PYTHON\" -m "
                    "missioncrew.agent_tool call <action> "
                    "--arguments '<JSON object>'`\n"
                    "Publish a file: `\"$MISSIONCREW_AGENT_TOOL_PYTHON\" -m "
                    "missioncrew.agent_tool publish-file "
                    "--source <local file> --path <path in the document library>`\n"
                    "Available actions: " + ", ".join(allowed_actions)
                )
                allowed_dirs = project_allowed_dirs(project, library, workspace.root)
                if (project.controls_platform(role.id)
                        and not self._manual_only_role(project, role)):
                    orchestrator_section = self._orchestrator_section(project)

        # 有主控时只有主控是"控制方";无主控时每个角色都是
        is_orchestrator = bool(project and project.controls_platform(role.id))
        orchestrator_id = project.orchestrator_role_id if project else ""
        peer_mode = bool(project and not orchestrator_id)
        manual_only = self._manual_only_role(project, role)
        known_roles = {r.id: r for r in self.store.list_roles(channel.project_id or "")}

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

        # 人类经输入框上传的频道附件与页面对话的正文快照:都是频道级目录,
        # 存在才授权;消息正文/上下文已含具体路径
        for shared_dir in (channel_uploads_dir(channel.project_id or "", channel.id),
                           channel_page_context_dir(channel.project_id or "",
                                                    channel.id)):
            if shared_dir.is_dir() and str(shared_dir) not in allowed_dirs:
                allowed_dirs.append(str(shared_dir))

        # 只有主控拿到项目角色名册;执行角色只接收当前任务简报,不知道也
        # 不能横向调度其他执行角色。仅人工点名的角色在无主控项目里同样只执行。
        short_channel_id = (channel.id.removeprefix(f"{channel.project_id}:")
                            if channel.project_id else channel.id)
        roster_section, roster_snapshot = "", {}
        if manual_only:
            workflow_section = MANUAL_ONLY_WORKFLOW.format(
                documents_dir=documents_dir, temp_dir=temp_dir)
        elif peer_mode:
            roster_section, roster_snapshot = self._roster_snapshot(
                project, role, _role_line)
            workflow_section = PEER_WORKFLOW.format(
                documents_dir=documents_dir, temp_dir=temp_dir)
        elif is_orchestrator:
            roster_section, roster_snapshot = self._roster_snapshot(
                project, role, _role_line)
            workflow_section = ORCHESTRATOR_WORKFLOW.format(channel_id=short_channel_id)
        else:
            workflow_section = EXECUTOR_WORKFLOW.format(
                documents_dir=documents_dir, temp_dir=temp_dir,
                orchestrator_id=orchestrator_id)
        body_fields = dict(
            role_id=role.id, role_name=role.name, role_desc=role.description,
            role_capabilities=", ".join(role.capabilities) or "none specified",
            role_traits=role.preference or "none specified",
            role_runtime=role.runtime_id,
            role_model=role.model or "(CLI default)",
            role_effort=f"/effort={role.effort}" if role.effort else "",
            channel_name=channel.name or channel.id,
            channel_id=short_channel_id,
            channel_purpose=channel.purpose or "(not specified)",
            project_line=project_line,
            workdir_section=workdir_section,
            repos_line=repos_line,
            workflow_section=workflow_section,
            tool_section=tool_section,
            project_section=project_section,
            orchestrator_section=orchestrator_section,
        )

        def _body(roster: str) -> str:
            text = CHAT_COMMON_BODY.format(**body_fields, roster_section=roster)
            return re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"

        # 版本号只覆盖规则与索引;项目清单是实时数据,变化经本轮输入的差异提示告知
        context_version = hashlib.sha256(_body("").encode("utf-8")).hexdigest()[:16]
        common_body = _body(roster_section)
        common_prompt = DURABLE_CONTEXT_TEMPLATE.format(
            context_version=context_version, common_body=common_body)
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

        # 准则/Skill 正文版本与主控清单条目:与会话上次看到的快照比对,只把
        # 差异写进本轮输入,公共上下文版本不受影响
        snapshot: dict[str, tuple[str, str]] = {
            key: (version, "") for key, version in
            (project_resource_versions(project) if project else {}).items()}
        snapshot.update(roster_snapshot)
        resource_state = json.dumps(
            {key: version for key, (version, _) in snapshot.items()}, sort_keys=True)
        resource_notice = ""
        if saved_session and compatible:
            try:
                seen = json.loads(saved_session.get("resource_state") or "{}")
            except (json.JSONDecodeError, TypeError):
                seen = {}
            resource_notice = _change_notice(snapshot, seen)
        turn_prompt = TURN_PROMPT.format(
            resource_notice=resource_notice,
            reply_hint=CONTROL_REPLY_HINT if is_orchestrator else "",
            trigger_kind=_trigger_kind(trigger_record, orchestrator_id,
                                       is_orchestrator),
            trigger=json.dumps(trigger_record, ensure_ascii=False))
        recovery_prompt = RECOVERY_PROMPT.format(
            history=_compact_records(history_records), turn_prompt=turn_prompt)
        prompt = common_prompt + "\n" + recovery_prompt

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
                resource_state=resource_state,
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
            trigger_message_id=msg_id,
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

    def channel_history_records(self, channel: Channel, role_id: str,
                                limit: int, before_id: int = 0) -> list[dict]:
        """按角色视图返回频道最新 limit 条消息记录(id 升序),供 Agent Tool
        `channel.history` 使用;脱敏规则与工作区 channel-history.json 一致。"""
        project_id = channel.project_id or ""
        project = self.store.get_project(project_id) if project_id else None
        role = self.store.get_role(project_id, role_id) if project else None
        known_roles = ({r.id: r for r in self.store.list_roles(project_id)}
                       if project else {})
        return [self._message_record(m, role, project, known_roles)
                for m in self.store.recent_messages(
                    channel.id, limit, 0, before_id)]

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

    @staticmethod
    def _manual_only_role(project, role: Optional[Role]) -> bool:
        """角色是否在 Agent 协作图之外(仅人工点名)。

        API/CLI 已保证主控不会设为仅人工点名;旧数据若不一致,主控仍按主控
        处理,避免项目失去派发方。
        """
        if role is None or not role.manual_only:
            return False
        return not (project and project.has_orchestrator
                    and role.id == project.orchestrator_role_id)

    @staticmethod
    def _redact_mentions(content: str, mention_spans: list[dict],
                         redacted: set[str], keep_spans: bool
                         ) -> tuple[str, list[dict]]:
        """把 redacted 角色的 @role / @[role](旧派发语法作为字面文本同样会
        泄漏角色名)替换成匿名占位。keep_spans 时剔除匿名角色的范围并按替换
        造成的长度差平移其余范围,否则整体丢弃。"""
        edits: list[tuple[int, int]] = []   # (原文结束位置, 长度差)

        def _sub(match: re.Match) -> str:
            role_id = match.group(1) or match.group(2)
            if role_id not in redacted:
                return match.group(0)
            replacement = "[other executor]"
            edits.append((match.end(), len(replacement) - len(match.group(0))))
            return replacement

        content = ANY_MENTION_RE.sub(_sub, content)
        if not keep_spans:
            return content, []
        kept = []
        for span in mention_spans:
            start, end = span.get("start"), span.get("end")
            if (span.get("role_id") in redacted
                    or type(start) is not int or type(end) is not int):
                continue
            shift = sum(delta for edit_end, delta in edits if edit_end <= start)
            kept.append({**span, "start": start + shift, "end": end + shift})
        return content, kept

    def _message_record(self, message: dict, role: Optional[Role] = None,
                        project=None,
                        known_roles: Optional[dict[str, Role]] = None) -> dict:
        """把数据库消息转换为边界明确的 JSON 记录，并按执行角色脱敏。

        ``role`` 是读取视图的 Agent(None 即人类/平台的原始视图);``known_roles``
        是项目角色 id -> Role,调用方批量转换时传入以免逐条查库。
        """
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
        if known_roles is None:
            known_roles = ({r.id: r for r in self.store.list_roles(project.id)}
                           if project else {})
        full_view = (role is None or not project or not orchestrator_id
                     or role.id == orchestrator_id)
        # 仅人工点名的角色名册里没有,历史里对其他 Agent 也不能露出 id;
        # 执行角色视图在此之上再把其余执行角色一并匿名。
        hidden = ({rid for rid, r in known_roles.items()
                   if rid != role.id and self._manual_only_role(project, r)}
                  if role is not None and project else set())
        redacted = set(hidden)
        if not full_view:
            redacted |= set(known_roles) - {role.id, orchestrator_id}
        redact_author = author_type == "agent" and (
            author in hidden
            or (not full_view and author not in {role.id, orchestrator_id}))

        if redacted or not full_view:
            # 执行角色不需要渲染主控界面的提及样式,范围直接丢弃;主控/同权
            # 视图靠范围判断本轮是派发还是回报,只剔除匿名角色并平移其余偏移。
            content, mention_spans = self._redact_mentions(
                content, mention_spans, redacted, keep_spans=full_view)
            mentions = ["other executor" if item in redacted else item
                        for item in mentions]

        record = {
            "id": int(message.get("id", 0)),
            "kind": str(message.get("kind") or "message"),
            "author": {
                "id": "executor" if redact_author else author,
                "type": author_type,
            },
            "content": content,
            "mentions": mentions,
            "thread": {
                "reply_to": message.get("reply_to"),
                "root_id": message.get("root_id"),
                "depth": int(message.get("depth", 0)),
            },
            "created_at": message.get("created_at"),
        }
        if mention_spans:
            record["mention_spans"] = mention_spans
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
        # 临时文件名必须唯一:多个写者(例如不同 ChatEngine 实例)同时更新
        # 同一文件时,固定名字会让一方的 replace 抢走另一方刚写好的临时文件。
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent,
                    prefix=f".{path.name}.", suffix=".tmp",
                    delete=False) as handle:
                handle.write(content)
                temporary = Path(handle.name)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _write_channel_history(self, channel: Channel,
                               role: Optional[Role] = None, project=None) -> Path:
        """原子更新完整频道历史，并返回当前角色获准读取的 JSON 视图。"""
        # 原始历史保存在平台内部工作区；每个角色只获准读取自己独立
        # `.missioncrew/channel-history.json`，避免横向看到其他执行角色视图。
        project = project or self.store.get_project(channel.project_id or "")
        project_id = channel.project_id or "_unscoped"
        canonical = platform_history_dir(project_id, channel.id) / "channel-history.json"
        orchestrator_id = project.orchestrator_role_id if project else ""
        scoped = bool(role and project and orchestrator_id
                      and role.id != orchestrator_id)
        visible_path = (chat_workspace_dir(project.id, channel.id, role.id)
                        / "channel-history.json"
                        if role and project else canonical)
        known_roles: dict[str, Role] = {}

        def hidden_peers(viewer: Role) -> bool:
            """主控/同权角色的视图本可直接用原始记录,但项目里有别的仅人工
            点名角色时同样要脱敏。"""
            return any(rid != viewer.id and self._manual_only_role(project, r)
                       for rid, r in known_roles.items())

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
                known_roles = {r.id: r for r in self.store.list_roles(project.id)}
                visible_records = (
                    [self._message_record(m, role, project, known_roles)
                     for m in messages]
                    if scoped or hidden_peers(role) else canonical_records)
                self._atomic_write_json(visible_path, _payload(visible_records))
            elif project:
                # 新消息落库时刷新已经建立的角色视图；尚未执行过的角色不提前
                # 创建 workspace，等首次装配时再生成。
                known_roles = {r.id: r for r in self.store.list_roles(project.id)}
                for target_role in known_roles.values():
                    target = (chat_workspace_dir(
                        project.id, channel.id, target_role.id)
                              / "channel-history.json")
                    if not target.parent.is_dir():
                        continue
                    target_scoped = (not project.controls_platform(target_role.id)
                                     or hidden_peers(target_role))
                    records = (
                        [self._message_record(m, target_role, project, known_roles)
                         for m in messages]
                        if target_scoped else canonical_records)
                    self._atomic_write_json(target, _payload(records))
        return visible_path

    def _orchestrator_section(self, project) -> str:
        """主控级规则(派发纪律、频道创建、预算);动作细节在手册,清单见 _roster_snapshot。
        有主控时给主控;无主控时每个角色拿不含派发的 PEER_RULES。"""
        if not project.has_orchestrator:
            return PEER_RULES
        return CONTROL_RULES_TEMPLATE.format(max_runs=project.max_chain_runs)

    def _roster_snapshot(self, project, role: Role, tag) -> tuple[str, dict]:
        """主控的项目清单:角色名册、代码仓、频道、面板、数据源、自动化、停用项。

        清单附在公共区块末尾但不参与版本哈希;返回 (清单文本, {key: (版本, 行)})。
        会话保存上次看到的版本,下一轮只把差异写进本轮输入,避免新建频道、配额停用
        角色、数据源同步这类高频变化让主控整块重发。
        """
        def _short(cid: str) -> str:
            return cid.removeprefix(f"{project.id}:")

        entries: dict[str, str] = {}

        def _add(kind: str, ident: str, line: str) -> None:
            entries[f"{kind}:{ident}"] = line

        for r in self.store.list_roles(project.id):
            if r.id != role.id and r.enabled and not self._manual_only_role(project, r):
                _add("role", r.id, tag(r))
        for r in project.repos:
            _add("repo", r.id or r.name,
                 f"- {r.name or r.id}: {r.path or '(no local path)'}"
                 + (f" (git remote {r.remote})" if r.remote else ""))
        for c in self.store.list_channels(project.id, include_archived=False):
            _add("channel", _short(c.id),
                 f"- {_short(c.id)} (#{c.name}): {c.purpose or 'no purpose given'}"
                 + (f"; workdir {c.workdir}"
                    + (" (directory no longer exists)" if not Path(c.workdir).is_dir() else "")
                    if c.workdir else "")
                 + f"; Web {channel_resource_url(project.id, c.id)}")
        for b in self.store.list_boards(project.id):
            body = (f"data source {b.source}, {len(b.filters)} filter columns"
                    if b.kind == "taskboard" else
                    "widgets " + (", ".join(f"{w.id}/{w.type}" for w in b.layout) or "none"))
            _add("board", _short(b.id),
                 f"- {_short(b.id)} ({b.name}): {b.description or 'no description'}; {body}; "
                 f"Web {dashboard_resource_url(project.id, b.id)}")
        for s in self.store.list_board_datasources(project.id):
            # 状态取值随源而定:task.create/task.update 往自定义源写状态时要用它们
            _add("source", _short(s.id),
                 f"- {_short(s.id)} ({s.name}): {s.description or 'no description'}; "
                 "status values " + "/".join(str(c.get("value") or "")
                                             for c in s.status_values))
        for a in self.store.list_automations(project.id):
            _add("automation", _short(a.id),
                 f"- {_short(a.id)} ({a.name}): {a.description or 'no description'}; "
                 + (f"cron `{a.cron}`" if a.cron else "manual trigger only")
                 + (" [disabled]" if not a.enabled else ""))
        _add("disabled", "guidelines", "- Disabled guidelines: "
             + (", ".join(g.name for g in project.guidelines if not g.enabled) or "none"))
        _add("disabled", "skills", "- Disabled skills: "
             + (", ".join(s.id for s in project.skills if not s.enabled) or "none"))

        def _block(kind: str) -> str:
            return "\n".join(line for key, line in entries.items()
                             if key.startswith(kind + ":"))

        # 频道只列 id:详情走 channel.list;条目行仍保留在快照里供差异提示引用
        channel_ids = ", ".join(key.partition(":")[2] for key in entries
                                if key.startswith("channel:")) or "(none)"
        parts = [
            ROSTER_HEADER,
            ("## Role Roster (visible only to the orchestrator; use each profile to choose assignees)\n"
             if project.has_orchestrator else
             "## Role Roster (for reference: roles cannot dispatch each other here; when another "
             "role is needed, name it in your reply for a human to mention)\n")
            + (_block("role") or "(no other enabled roles)"),
            "## Project Repositories\n" + (_block("repo") or "(not configured)"),
            "## Existing Channels\nView them on demand with `channel.list` (archived included); "
            "check for duplicates before creating one. Active channel ids: "
            + channel_ids,
        ]
        # 空的段落折叠成一行,小项目里不再是一串「(无)」
        optional = [("board", "## Existing Dashboards", "dashboards"),
                    ("source", "## Existing Board Data Sources", "board data sources"),
                    ("automation", "## Existing Automations", "automations")]
        empty = []
        for kind, title, label in optional:
            block = _block(kind)
            parts.append(f"{title}\n{block}") if block else empty.append(label)
        disabled_lines = [line for key, line in entries.items()
                          if key.startswith("disabled:") and not line.endswith(": none")]
        if disabled_lines:
            parts.append("## Disabled Guidelines and Skills\n" + "\n".join(disabled_lines))
        else:
            empty.append("disabled guidelines and skills")
        if empty:
            listed = ", ".join(empty)
            parts.append(listed[0].upper() + listed[1:] + ": none"
                         + (" (the built-in board data source `built-in` is always available)"
                            if "board data sources" in empty else ""))
        section = "\n".join(parts) + "\n"
        snapshot = {
            key: (hashlib.sha256(line.encode("utf-8")).hexdigest()[:16], line)
            for key, line in entries.items()}
        return section, snapshot

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
