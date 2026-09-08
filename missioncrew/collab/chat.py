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
- 无主控模式(项目未选择主控):所有角色权限相同,都能看到名册并经 message.publish
  派发;人类同时点名多个角色时各自启动;由角色显式派发的结果自动交回派发者,
  人类直接点名的结果只留在频道。

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
WORKDIR_SECTION = "工作目录:`{workdir}`(就是当前目录,直接在其中读写文件、运行命令完成工作)"
# 频道绑定目录缺失时的回退提示:让 Agent 知道当前目录不是代码仓,避免误操作
MISSING_WORKDIR_NOTICE = (
    "注意:本频道绑定的工作目录 `{bound}` 已不存在(例如任务 worktree 已清理),"
    "本轮回退到平台默认目录 `{workdir}`。这里没有业务代码,不要在此执行 git 操作,"
    "也不要把它当作代码仓;要继续原目录的工作,先重建该目录(如 git worktree add),"
    "或新建频道绑定正确的目录。")

CHAT_COMMON_BODY = """\
# 聊天协作请求
你是角色 @{role_id}({role_name})。
角色定位(这是你的专长画像,供协作方选人参考;它不是任务,不要据此自行发挥):
{role_desc}
角色能力:{role_capabilities}
角色偏好:{role_traits}
固定执行组合:{role_runtime}/{role_model}{role_effort}
当前频道:#{channel_name}(id `{channel_id}`)
频道用途/讨论边界:{channel_purpose}
项目:{project_line}
{workdir_section}
{repos_line}

# 怎么干活
{workflow_section}

{tool_section}

{project_section}

{orchestrator_section}
{roster_section}
"""

# 执行角色与主控各自的一轮流程:先读的先给,规则都挂在步骤上
EXECUTOR_WORKFLOW = """\
1. 读任务:任务只来自最下方「触发消息」里的任务简报,角色定位不是任务;简报信息不足就在回复中提出,不要臆测扩大范围。
2. 补背景:需要时再看,不要预加载。频道历史在「工作区一览」的 `channel-history.json`;准则和 Skill 先看「项目资料」里的 description,相关再读全文;项目文档在 `{documents_dir}`。
3. 干活:在工作目录读写代码、运行命令;只能碰「必须遵守」里列出的可读写目录;临时文件优先放当前业务仓已有的任务目录(如项目约定的 `.tmp/`、`.e2e/`),没有项目约定时使用 `{temp_dir}`,并在任务完成后清理。
4. 写回平台:发布文档、创建或更新 Task、追加状态简报,一律用下方 Agent Tool;不要直接改 `documents/`、`tasks/` 里的文件。
5. 回复:你的最终回复会被完整、原样发布到聊天频道,人类可以看到;用中文,先说结论,再简述做了什么;不要贴大段日志;引用平台资源用 `/resources/...` 链接,不要输出内部路径。
- 你不是项目主控,看不到其他执行角色名册,也不能调度其他角色;消息正文里写任何 @ 都只是普通文字。
- 只提交本次任务的完整结果。若本轮由项目主控派发,平台会把结果自动交回主控;若由人类直接点名,结果只发布到频道,不会自动触发主控,主控在后续被人类唤起时仍可读取完整记录。"""

ORCHESTRATOR_WORKFLOW = """\
1. 读需求:人类的请求或执行角色的回报就是本轮输入,类型见「本轮触发」一行;人类同时点了多个角色时平台只启动你,完整名单在触发消息 JSON 的 `mentions` 和 `mention_spans` 里,由你决定并行、顺序或调整人选后分别派发。
2. 定人和定地方:在下方「角色名册」按定位、能力与偏好选人;是否新建频道按项目准则,没有约定就在当前频道派发;要引用、派发到其他频道或新建频道前,先用 `channel.list`(scope=active/archived/all)查一遍,避免重复建同名频道。
3. 派发:`message.publish` 传 `channel`(当前频道就传 `{channel_id}`)、`mentions` 角色数组,content 写清背景、要求和验收标准;派工仍必须使用 `message.publish`,并在 `mentions` 中显式写入目标角色,这是唯一的派发方式。
4. 收尾等回报:`message.publish` 返回非空 `dispatched` 时,当前 turn 的派发职责已经完成,立即在最终回复中简短说明已派发,然后结束当前 turn;不要为这条说明再调用一次 `message.publish`。执行角色完成或失败后,平台会自动启动新的主控 turn 并交回完整结果,届时再验收、继续调度或汇总。
5. 验收汇总:收到回报后对照验收标准核对,再继续派发或写结论;阶段结论用 `task.brief` 记进 Task 简报,跨轮状态靠它而不是会话记忆;验收要跑命令时到「项目代码仓」目录里做,当前工作目录不一定是代码仓。"""

# 无主控项目:每个角色既执行也可派发,流程是执行与主控两条线的合并
PEER_WORKFLOW = """\
1. 读任务:本轮输入是最下方「触发消息」里的人类请求、其他角色派发的任务简报或回报,类型见「本轮触发」一行;角色定位不是任务;简报信息不足就在回复中提出,不要臆测扩大范围。
2. 补背景:需要时再看,不要预加载。频道历史在「工作区一览」的 `channel-history.json`;准则和 Skill 先看「项目资料」里的 description,相关再读全文;项目文档在 `{documents_dir}`。
3. 干活或派发:能自己完成的就在工作目录读写代码、运行命令;只能碰「必须遵守」里列出的可读写目录;临时文件优先放当前业务仓已有的任务目录,没有项目约定时使用 `{temp_dir}`,并在任务完成后清理。需要其他角色时在下方「角色名册」按定位、能力与偏好选人,用 `message.publish` 传 `channel`(当前频道就传 `{channel_id}`)和 `mentions` 角色数组派发,content 写清背景、要求和验收标准;这是唯一的派发方式。
4. 写回平台:发布文档、创建或更新 Task、追加状态简报,一律用下方 Agent Tool;不要直接改 `documents/`、`tasks/` 里的文件。
5. 回复:你的最终回复会被完整、原样发布到聊天频道;用中文,先说结论,再简述做了什么;不要贴大段日志;引用平台资源用 `/resources/...` 链接,不要输出内部路径。`message.publish` 返回非空 `dispatched` 时,当前 turn 的派发职责已经完成,简短说明已派发后立即结束 turn;被派发角色完成或失败后,平台会自动启动你的新 turn 并交回完整结果,届时再验收、继续派发或汇总。
- 本项目没有主控:所有角色权限相同。由你派发的任务,结果自动交回你;由人类直接点名的任务,结果只发布到频道,不会自动触发其他角色;人类同时点名多个角色时各自启动、互不等待。"""

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
{resource_notice}# 本轮触发:{trigger_kind}
# 触发消息(JSON,你的任务简报由发起者撰写)
{trigger}
"""

# 准则/Skill 正文与主控清单条目的变化都不改公共上下文版本,只在本轮输入里列差异
RESOURCE_NOTICE = (
    "# MissionCrew 资源更新\n"
    "自你上一轮之后发生了以下变化,公共上下文版本未变:\n{items}\n\n"
)
_CHANGE_LABELS = {
    "guideline": "准则", "skill": "Skill", "role": "角色", "repo": "代码仓",
    "channel": "频道", "board": "面板", "source": "数据源", "automation": "自动化",
    "disabled": "停用清单",
}
_ROSTER_KINDS = frozenset({"role", "repo", "channel", "board", "source", "automation", "disabled"})
_REMOVED_HINT = {
    "role": "已停用或删除,不可再派发", "channel": "已归档或删除", "board": "已删除",
    "source": "已删除", "automation": "已删除", "repo": "已移除",
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
                lines.append(f"- 新增{label}:{text}")
        elif seen[key] != version:
            lines.append(f"- {label}更新:{text}" if line
                         else f"- {label} `{name}` 正文已修改,需要用到时重新读取")
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
# 最近对话(JSON,按消息边界格式化)
{history}

{turn_prompt}
"""

ORCHESTRATOR_HEADER = """\
# 项目主控职责
你是本项目唯一主控,负责理解项目目标、拆解工作并调度其他角色;所有 MissionCrew 写操作必须使用上方 Agent Tool。"""

PEER_HEADER = """\
# 协作职责(无主控项目)
本项目没有主控,所有角色权限相同:你既执行任务,也可以按需把工作派发给其他角色;所有 MissionCrew 写操作必须使用上方 Agent Tool。"""

# 主控级规则(派发纪律、按需查询、预算):有主控时给主控,无主控时给每个角色
CONTROL_RULES_TEMPLATE = """\
{header}
纪律:
- 你发出的任何消息正文(包括最终回复)里的 @角色ID、@[角色ID] 都只是普通文字,永不触发执行;派发只认 `message.publish` 的 `mentions`。不需要协作就不要传 mentions;不要调度你自己,不要编造不存在的角色;角色的 runtime/模型在项目定义时已固定,你不能也不需要调整。
- 派发后不要使用 `sleep`,不要周期性轮询频道历史、工作树或运行状态,不要代替执行角色继续其任务,也不要向仍在执行的角色再次 `message.publish` 追问中间状态:同一频道同一角色的持久会话无法中途插入新 turn,这类请求只会排在原任务后面,不能提供实时进度。只有 `dispatched` 为空,或仍有不依赖已派发角色的即时工作时,才继续当前 turn。
- 回答人类、汇总结论或说明状态时直接写最终回复:你在当前 turn 的最终回复会由平台自动发布到触发消息所在的 Channel,不要再调用空 `mentions` 的 `message.publish` 复制同一份答复。
- 配置页协作消息会给出当前页面、当前条目、未保存草稿和用户选中的内容:只提问或讨论时直接回答,不要改配置;明确要求创建或修改时,必须用对应动作实际落库。
按需查询:
- 活动 Run 状态不会自动写入你的上下文;只有人类询问运行情况或要求停止角色时,才调用 `channel.runs.list` 取一次性快照,并用返回的 `run_id` 调用 `channel.run.stop`。
- 频道清单不在上下文里,用 `channel.list` 查;`channel.create` 的 workdir 只能是项目代码仓路径或其子目录,新频道创建后是空的,用 `message.publish` 把任务简报发进去并在 `mentions` 里点名执行者。
- 面板、看板数据源、准则与 Skill 的保存格式、文档发布与重命名、自动化脚本、回收站的规则见手册对应章节,操作前先读。
协作链预算:本项目单条协作链最多 {max_runs} 次 Agent 执行,`message.publish` 返回值的 `chain_budget` 里有已用次数;这只是防止失控循环的总次数兜底,不限制调度层级,请在预算内自主拆解、分派、验收并推进任务。
"""

# 主控的项目清单:附在公共区块末尾但不参与版本哈希,差异经本轮输入提示
ROSTER_HEADER = (
    "# 项目清单\n"
    "以下为实时数据,不参与公共上下文版本;条目新增、更新、归档或停用时,本轮输入会列出差异。\n")


def _role_line(r: Role) -> str:
    """角色名册一行:能力、偏好、定位分段标注,供主控选人。"""
    desc = " ".join((r.description or "").split()) or "未填写"
    return (f"- @{r.id} {r.name} · 能力 {'/'.join(r.ability_labels()) or '未标注'}"
            f" · 偏好 {r.preference or '未标注'} · 定位 {desc}")


def _trigger_kind(record: dict, orchestrator_id: str, is_orchestrator: bool,
                  role_id: str = "") -> str:
    """一句话说明本轮触发是谁发的,主控据此区分人类请求与执行角色回报。

    无主控项目里没有固定的派发方:触发消息的提及范围点到自己就是派发,
    否则是回报。"""
    author = record.get("author") or {}
    kind, author_id = str(author.get("type") or ""), str(author.get("id") or "")
    if kind == "human":
        text = "人类消息"
    elif kind == "agent":
        if not orchestrator_id:
            dispatched = any(span.get("role_id") == role_id
                             for span in record.get("mention_spans") or [])
            text = (f"角色 @{author_id} 派发的任务" if dispatched
                    else f"角色 @{author_id} 的回报,请验收")
        elif is_orchestrator:
            text = f"执行角色 @{author_id} 的回报,请验收"
        elif author_id == orchestrator_id:
            text = f"主控 @{author_id} 派发的任务"
        else:
            text = f"角色 @{author_id} 的消息"
    elif kind == "automation":
        text = f"自动化脚本 {author_id} 发布的消息"
    else:
        text = f"{kind or '平台'}消息"
    depth = int((record.get("thread") or {}).get("depth") or 0)
    return text + (f"(协作链第 {depth} 层)" if depth else "")


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
        self.agent_tools = AgentActionService(store, self.post, self.stop_run)
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
        # 无主控模式:项目未选择主控,所有角色同权
        peer_mode = bool(project) and not orchestrator
        # Agent 派发只认 message.publish 显式命令传入的结构化范围;正文里的
        # 任何 @（含 @[role] 旧语法）都是普通文字。有主控时执行角色无论写
        # 什么,都只把完整结果交回主控,避免横向看见或调用名册;无主控时
        # 由角色显式派发的结果交回派发者,人类直接点名的留在频道。
        if author_type == "agent":
            can_dispatch = peer_mode or author == orchestrator
            if can_dispatch and mention_spans:
                mentions, legal_spans = self._validate_mention_spans(
                    content, mention_spans, channel.project_id or "")
                if author in mentions:
                    mentions = [item for item in mentions if item != author]
                    legal_spans = [span for span in legal_spans
                                   if span["role_id"] != author]
            elif peer_mode:
                dispatcher = self._dispatching_role(reply_to, author)
                mentions = [dispatcher] if dispatcher else []
            elif (author != orchestrator and orchestrator_available
                  and not self._is_direct_human_dispatch(reply_to, author)):
                mentions = [orchestrator]
            else:
                mentions = []
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
            if (author_type == "agent" and not legal_spans
                    and (peer_mode or author == orchestrator)):
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

    def _dispatching_role(self, trigger_message_id: Optional[int],
                          role_id: str) -> str:
        """无主控模式:触发消息若是其他角色经 message.publish 显式点名本角色
        的派发,返回派发者 id;人类消息、平台消息或平台自动交回的消息返回空。

        只认落库的结构化提及范围:自动交回的消息 mentions 由平台写入而没有
        范围,因此验收方的最终回复不会再弹回执行方,避免两个角色互相唤起。
        """
        if trigger_message_id is None:
            return ""
        trigger = self.store.get_message(trigger_message_id)
        if not trigger or trigger.get("author_type") != "agent":
            return ""
        author = str(trigger.get("author") or "")
        if not author or author == role_id:
            return ""
        spans = self._decoded_mention_spans(trigger)
        return author if any(span.get("role_id") == role_id for span in spans) else ""

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
                                project_id: str) -> tuple[list[str], list[dict]]:
        """验证 UI 选择器给出的 Unicode code-point 范围，不从正文猜目标。"""
        known = {role.id: role for role in self.store.list_roles(project_id)}
        normalized: list[dict] = []
        for item in requested:
            if not isinstance(item, dict):
                raise ValueError("提及必须由角色选择器生成")
            role_id = item.get("role_id")
            start, end = item.get("start"), item.get("end")
            role = known.get(role_id) if isinstance(role_id, str) else None
            if (not isinstance(role_id, str) or role is None or not role.enabled
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
        """公开执行失败;有主控时只把主控派发的失败交回主控,无主控时交回派发者。"""
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
        if project and not orchestrator:
            dispatcher = self._dispatching_role(msg_id, role_id)
            if dispatcher:
                self._trigger(channel, dispatcher, failure_id, root_id, result_depth)
        elif (orchestrator_role and orchestrator_role.enabled
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
        Agent 回复回路交回主控验收。无主控项目一律挂回原触发消息,由
        post 按派发者(角色显式点名)或人类直达决定是否交回。"""
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
        manual_path = "(本次执行未提供工作区)"
        project_line = "(无所属项目)"
        repos_line = ""
        documents_dir = "(本次执行未提供工作区)"
        temp_dir = "当前工作目录内符合项目约定的位置"
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
                manual_path = str(workspace.manual)
                project_line = project.name + (
                    f" · {' '.join(project.description.split())}" if project.description else "")
                repos_line = ("项目代码仓:" + ";".join(
                    f"{r.name or r.id} `{r.path or r.remote or '未配置本地路径'}`"
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
                    "平台写操作(文档、Task、消息、频道、面板、准则、Skill 等)必须显式调用此工具;"
                    "命令返回 JSON,失败时退出码非零,读取 error.code/error.message 并在当前回合"
                    "修正后重试;不要直接写 documents/tasks 绕过接口。\n"
                    f"Python:`{sys.executable}`\n"
                    f"API:`{tool_url}`\n"
                    f"Token 文件:`{token_file}`(不要读取、打印或发送其内容;"
                    "令牌已绑定当前 Run,调用时不要传 `--run-id`)\n"
                    "查看能力:`\"$MISSIONCREW_AGENT_TOOL_PYTHON\" -m "
                    "missioncrew.agent_tool actions`\n"
                    "调用格式:`\"$MISSIONCREW_AGENT_TOOL_PYTHON\" -m "
                    "missioncrew.agent_tool call <action> "
                    "--arguments '<JSON 对象>'`\n"
                    "发布文件:`\"$MISSIONCREW_AGENT_TOOL_PYTHON\" -m "
                    "missioncrew.agent_tool publish-file "
                    "--source <本地文件> --path <文档库相对路径>`\n"
                    "可用动作:" + ", ".join(allowed_actions)
                )
                allowed_dirs = project_allowed_dirs(project, library, workspace.root)
                if project.controls_platform(role.id):
                    orchestrator_section = self._orchestrator_section(project)

        # 有主控时只有主控是"控制方";无主控时每个角色都是
        is_orchestrator = bool(project and project.controls_platform(role.id))
        orchestrator_id = project.orchestrator_role_id if project else ""
        peer_mode = bool(project and not orchestrator_id)
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

        # 人类经输入框上传的频道附件与页面对话的正文快照:都是频道级目录,
        # 存在才授权;消息正文/上下文已含具体路径
        for shared_dir in (channel_uploads_dir(channel.project_id or "", channel.id),
                           channel_page_context_dir(channel.project_id or "",
                                                    channel.id)):
            if shared_dir.is_dir() and str(shared_dir) not in allowed_dirs:
                allowed_dirs.append(str(shared_dir))

        # 只有主控拿到项目角色名册;执行角色只接收当前任务简报,不知道也
        # 不能横向调度其他执行角色。
        short_channel_id = (channel.id.removeprefix(f"{channel.project_id}:")
                            if channel.project_id else channel.id)
        roster_section, roster_snapshot = "", {}
        if peer_mode:
            roster_section, roster_snapshot = self._roster_snapshot(
                project, role, _role_line)
            workflow_section = PEER_WORKFLOW.format(
                documents_dir=documents_dir, temp_dir=temp_dir,
                channel_id=short_channel_id)
        elif is_orchestrator:
            roster_section, roster_snapshot = self._roster_snapshot(
                project, role, _role_line)
            workflow_section = ORCHESTRATOR_WORKFLOW.format(channel_id=short_channel_id)
        else:
            workflow_section = EXECUTOR_WORKFLOW.format(
                documents_dir=documents_dir, temp_dir=temp_dir)
        body_fields = dict(
            role_id=role.id, role_name=role.name, role_desc=role.description,
            role_capabilities=", ".join(role.capabilities) or "无特别标注",
            role_traits=role.preference or "无特别标注",
            role_runtime=role.runtime_id,
            role_model=role.model or "(CLI 默认)",
            role_effort=f"/effort={role.effort}" if role.effort else "",
            channel_name=channel.name or channel.id,
            channel_id=short_channel_id,
            channel_purpose=channel.purpose or "(未说明)",
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
            trigger_kind=_trigger_kind(trigger_record, orchestrator_id,
                                       is_orchestrator, role.id),
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
        full_view = (role is None or not project or not orchestrator_id
                     or role.id == orchestrator_id)
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
                    target_scoped = not project.controls_platform(target_role.id)
                    records = (
                        [self._message_record(m, target_role, project, known_roles)
                         for m in messages]
                        if target_scoped else canonical_records)
                    self._atomic_write_json(target, _payload(records))
        return visible_path

    def _orchestrator_section(self, project) -> str:
        """主控级规则(派发纪律、频道创建、预算);动作细节在手册,清单见 _roster_snapshot。
        有主控时给主控,无主控时给每个角色。"""
        header = ORCHESTRATOR_HEADER if project.has_orchestrator else PEER_HEADER
        return CONTROL_RULES_TEMPLATE.format(
            header=header, max_runs=project.max_chain_runs)

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
            if r.id != role.id and r.enabled:
                _add("role", r.id, tag(r))
        for r in project.repos:
            _add("repo", r.id or r.name,
                 f"- {r.name or r.id}: {r.path or '(无本地路径)'}"
                 + (f"(git 远程 {r.remote})" if r.remote else ""))
        for c in self.store.list_channels(project.id, include_archived=False):
            _add("channel", _short(c.id),
                 f"- {_short(c.id)}(#{c.name}):{c.purpose or '无用途说明'}"
                 + (f";工作目录 {c.workdir}"
                    + ("(目录已不存在)" if not Path(c.workdir).is_dir() else "")
                    if c.workdir else "")
                 + f";Web {channel_resource_url(project.id, c.id)}")
        for b in self.store.list_boards(project.id):
            body = (f"数据源 {b.source},{len(b.filters)} 个筛选列"
                    if b.kind == "taskboard" else
                    "组件 " + (", ".join(f"{w.id}/{w.type}" for w in b.layout) or "无"))
            _add("board", _short(b.id),
                 f"- {_short(b.id)}({b.name}):{b.description or '无描述'};{body};"
                 f"Web {dashboard_resource_url(project.id, b.id)}")
        for s in self.store.list_board_datasources(project.id):
            _add("source", _short(s.id),
                 f"- {_short(s.id)}({s.name}):{s.description or '无描述'}")
        for a in self.store.list_automations(project.id):
            _add("automation", _short(a.id),
                 f"- {_short(a.id)}({a.name}):{a.description or '无描述'};"
                 + (f"cron `{a.cron}`" if a.cron else "仅手动触发")
                 + ("[停用]" if not a.enabled else ""))
        _add("disabled", "guidelines", "- 停用的准则:"
             + (", ".join(g.name for g in project.guidelines if not g.enabled) or "无"))
        _add("disabled", "skills", "- 停用的 Skill:"
             + (", ".join(s.id for s in project.skills if not s.enabled) or "无"))

        def _block(kind: str) -> str:
            return "\n".join(line for key, line in entries.items()
                             if key.startswith(kind + ":"))

        # 频道只列 id:详情走 channel.list;条目行仍保留在快照里供差异提示引用
        channel_ids = ", ".join(key.partition(":")[2] for key in entries
                                if key.startswith("channel:")) or "(无)"
        parts = [
            ROSTER_HEADER,
            ("## 角色名册（仅主控可见，各自定位供你选人参考）\n"
             if project.has_orchestrator else
             "## 角色名册（各自定位供你派发时选人参考）\n")
            + (_block("role") or "(无其他已启用角色)"),
            "## 项目代码仓\n" + (_block("repo") or "(未配置)"),
            "## 现有频道\n用 `channel.list` 按需查看(含归档);新建前先查重。活跃频道 id:"
            + channel_ids,
        ]
        # 空的段落折叠成一行,小项目里不再是一串「(无)」
        optional = [("board", "## 现有面板", "面板"), ("source", "## 现有看板数据源", "看板数据源"),
                    ("automation", "## 现有自动化", "自动化")]
        empty = []
        for kind, title, label in optional:
            block = _block(kind)
            parts.append(f"{title}\n{block}") if block else empty.append(label)
        disabled_lines = [line for key, line in entries.items()
                          if key.startswith("disabled:") and not line.endswith(":无")]
        if disabled_lines:
            parts.append("## 停用的准则与 Skill\n" + "\n".join(disabled_lines))
        else:
            empty.append("停用的准则与 Skill")
        if empty:
            parts.append("、".join(empty) + ":无" + ("(内置看板数据源 built-in 始终可用)"
                                                  if "看板数据源" in empty else ""))
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
