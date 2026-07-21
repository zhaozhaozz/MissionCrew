"""聊天协作引擎。

协作模型:
- 人类在频道里 @角色 布置工作;角色由固定 runtime/model 执行,
  定位、能力与偏好用于协作方选人,不参与执行时路由;
- 只有项目主控能在回复中 @其他角色发起工作;执行角色看不到其他角色名册,
  完成后由平台自动把完整结果交回主控继续调度;
- 所有主控调度与执行结果都对人类完全可见,全程审计。

防失控:项目可配置的单条协作链执行总数上限 + 不响应自己 @ 自己。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Optional

from ..runtime import runtime_manager
from ..core.config import mc_home
from .documents import library_for, safe_relative_path
from .workspace import (chat_workspace_dir, platform_history_dir,
                        prepare_agent_workspace, sync_task_files,
                        write_task_files)
from ..core.models import (BOARD_WIDGET_TYPES, DEFAULT_MAX_CHAIN_RUNS, Board,
                           BoardWidget, Channel, ExecutionConfig,
                           GuidelineDocument, ProjectSkill, Role, RuntimePolicy)
from .project_context import (project_allowed_dirs, render_project_context,
                              write_guideline_context)
from .skills import save_project_skill, save_project_skill_markdown
from ..core.store import Store

MENTION_RE = re.compile(r"@([\w-]+)")
HISTORY_WINDOW = 20    # 装配进 Prompt 的最近消息条数
CHAT_TIMEOUT = 900     # 单次聊天执行超时(秒)
ACTION_RE = re.compile(r"<missioncrew-action>(.*?)</missioncrew-action>", re.S)
CONTROL_ID_RE = re.compile(r"[\w-]+")

# 转交语义:@ 前若紧跟顺序词("完成后请 @reviewer"),说明是让前序角色
# 完成后转交,不立即触发;前序角色回复中 @ 到时才真正执行
DEFER_MARKERS = ("完成后", "然后", "之后", "接着", "随后")
DEFER_WINDOW = 8       # 顺序词与 @ 之间允许的最大字符距离

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
{orchestrator_section}\
工作目录就是当前目录,直接在其中读写文件、运行命令完成工作。

# 频道历史记录(JSON)
完整频道历史文件:{channel_history_path}
也可以通过环境变量 MISSIONCREW_CHANNEL_HISTORY 获取该路径。仅在最近对话不足以完成任务时按需读取。

# 回复要求
- 只完成触发消息交代的工作;信息不足时在回复中提出,不要臆测扩大范围。
- 你的最终回复会被完整、原样发布到聊天频道,人类可以看到。
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
你是本项目唯一主控，负责理解项目目标、拆解工作并调度其他角色。可在回复中加入
一个或多个控制动作（动作会被平台执行并从公开回复中移除，执行结果附在回复末尾）：
<missioncrew-action>{{"action":"create_channel","id":"channel-id","name":"名称","purpose":"任务边界","workdir":"可选，项目代码仓路径"}}</missioncrew-action>
<missioncrew-action>{{"action":"post_message","channel":"channel-id","content":"开工简报，@角色 会正常触发执行"}}</missioncrew-action>
<missioncrew-action>{{"action":"create_board","id":"board-id","name":"需求管理","description":"用途","layout":[]}}</missioncrew-action>
<missioncrew-action>{{"action":"update_board","id":"board-id","name":"新名称"}}</missioncrew-action>
<missioncrew-action>{{"action":"delete_board","id":"board-id"}}</missioncrew-action>
<missioncrew-action>{{"action":"save_guideline","markdown":"---\\nname: dev-spec\\ndescription: 涉及代码实现、API 或数据库变更时使用\\n---\\n\\n# 开发规范\\n\\nMarkdown 正文，可用 [部署说明](runbooks/deploy.md) 链接项目文档","enabled":true}}</missioncrew-action>
<missioncrew-action>{{"action":"save_skill","id":"local-ci","markdown":"---\\nname: 本地 CI\\ndescription: 用途\\n---\\n\\n完整执行说明，可用 [本地 CI](runbooks/local-ci.md) 链接项目文档","enabled":true}}</missioncrew-action>
<missioncrew-action>{{"action":"write_document","path":"specs/design.md","content":"Markdown 正文","message":"新增设计文档"}}</missioncrew-action>
要点：
- create_channel 的 workdir 只能是项目代码仓路径（见下方仓库清单）或其子目录；
  不填时若项目只配了一个代码仓则自动使用它。新频道创建后是空的，
  用 post_message 把任务简报发进去、@ 相应角色开工。
- update_board 不携带 layout 字段时保留现有布局；携带则全量替换。
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
- save_guideline 接收完整 markdown，文件必须以只含 name、description 的 YAML
  frontmatter 开头；后端直接读取这两个属性，不使用 id/title/summary，也不做字段转换。
  修改并重命名现有准则时传 original_name。save_skill 按 id 新建或覆盖，markdown 是
  完整 SKILL.md 原文：frontmatter 至少含 name、description，附加属性原样保留。
  不要建立文件、Runtime 或角色绑定列表；需要关联项目文档时，在正文中写标准相对
  Markdown 链接。description 应简洁说明适用场景；所有执行者只会收到已启用准则的
  description，并在相关时从对应准则 Markdown 文件读取完整正文。
  Skill 仍结合当前任务自行判断是否适用、是否需要读取链接文件。
- 验证、审查、安全和审批等项目要求也统一写入准则 Markdown，由 Agent 根据任务
  判断是否适用；平台不再维护或机械执行独立的验证规则。write_document 写入项目
  版本化文档库并立即生成 Git 版本。
- 配置页面协作消息会明确给出当前页面、当前条目、未保存草稿，以及用户选中的
  字段、行号和原文。只提问或讨论时直接回答，不要改配置；明确要求创建或修改时，
  必须使用对应的 save_guideline / save_skill / write_document 动作实际落库。
- 每个角色的 runtime/模型在项目定义角色时已经固定，你不能也不需要调整；
  调度就是在角色名册中选人：结合角色定位、能力与偏好(风格/领域)挑选
  最合适的角色，@ 它并写清任务简报。
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
    def __init__(self, store: Store, max_workers: int = 4):
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="chat-run")
        self._futures: list[Future] = []
        self._futures_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._chain_run_lock = threading.Lock()
        # 正在更新的 runtime 集合(由 server 注入共享):更新期间不派发执行,
        # 避免 Agent 跑在半更新的二进制上
        self.updating_backends: set[str] = set()

    # ---- 对外入口 ----
    def post(self, channel_id: str, author: str, content: str,
             author_type: str = "human", reply_to: Optional[int] = None,
             root_id: Optional[int] = None, depth: int = 0,
             runtime_id: Optional[str] = None, model: Optional[str] = None,
             effort: Optional[str] = None) -> int:
        """发布一条消息,并异步触发其中 @ 到的角色。返回消息 id。"""
        channel = self.store.get_channel(channel_id)
        if channel is None:
            raise ValueError(f"频道不存在: {channel_id}")
        project = self.store.get_project(channel.project_id or "")
        orchestrator = project.orchestrator_role_id if project else ""
        role = (self.store.get_role(channel.project_id or "", author)
                if author_type == "agent" else None)

        # 只有主控的 Agent 消息能按正文中的 @ 调度其他角色。执行角色不论
        # 回复里是否误写了 @，都只把完整结果交回主控，避免横向看见/调用名册。
        if author_type == "agent":
            if author == orchestrator:
                mentions = self._valid_mentions(
                    content, channel.project_id or "", exclude=author)
            elif orchestrator and self.store.get_role(
                    channel.project_id or "", orchestrator):
                mentions = [orchestrator]
            else:
                mentions = []
            if role:
                runtime_id = role.runtime_id if runtime_id is None else runtime_id
                model = role.model if model is None else model
                effort = role.effort if effort is None else effort
        else:
            # 人类仍可直接点名角色；无有效 @ 时默认交给项目主控。
            mentions = self._valid_mentions(content, channel.project_id or "", exclude=None)
        if not mentions and author_type == "human" and channel.project_id:
            if (orchestrator and author != orchestrator
                    and self.store.get_role(channel.project_id, orchestrator)):
                mentions = [orchestrator]
        msg_id = self.store.add_message(channel_id, author, author_type, content,
                                        mentions, reply_to, root_id, depth,
                                        runtime_id or "", model or "", effort or "")
        self._write_channel_history(channel)
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
        try:
            self._execute_inner(run_id, channel, role_id, msg_id, root_id, depth)
        except Exception as e:  # 后台线程的异常必须落到频道里,不能无声丢失
            self.store.update_chat_run(run_id, "failed", error=str(e))
            self._post_failure(channel, role_id, msg_id, root_id, depth,
                               f"@{role_id} 执行出错: {e}")

    def _post_failure(self, channel: Channel, role_id: str, msg_id: int,
                      root_id: int, depth: int, content: str) -> None:
        """公开执行失败；非主控失败也自动交回主控决定后续动作。"""
        result_depth = depth + 1
        failure_id = self.store.add_message(
            channel.id, "platform", "platform", content, [], msg_id, root_id,
            result_depth)
        self._write_channel_history(channel)
        project = self.store.get_project(channel.project_id or "")
        orchestrator = project.orchestrator_role_id if project else ""
        if (orchestrator and role_id != orchestrator
                and self.store.get_role(channel.project_id or "", orchestrator)):
            self._trigger(channel, orchestrator, failure_id, root_id, result_depth)

    def _execute_inner(self, run_id: int, channel: Channel, role_id: str,
                       msg_id: int, root_id: int, depth: int) -> None:
        role = self.store.get_role(channel.project_id or "", role_id)
        if role is None:
            self.store.update_chat_run(run_id, "failed", error="角色不存在")
            self._post_failure(channel, role_id, msg_id, root_id, depth,
                               f"@{role_id} 执行失败: 角色不存在")
            return

        backend, trace = self._pick_backend(channel, role)
        if backend is None:
            self.store.update_chat_run(run_id, "failed", error=trace)
            self._post_failure(channel, role_id, msg_id, root_id, depth,
                               f"@{role_id} 无可用后端: {trace}")
            return

        self.store.update_chat_run(run_id, "running", backend_id=backend.id)
        self.store.audit("platform", "chat_dispatch",
                         detail=f"channel={channel.id} role={role_id} "
                                f"backend={backend.id} depth={depth} {trace}")

        cfg = self._assemble(channel, role, backend, msg_id)
        # 运行过程(思考/工具/输出)实时落库,前端在聊天流中内联展示
        cfg.emit = lambda kind, text: self.store.append_run_event(run_id, kind, text)
        library = library_for(channel.project_id or "")
        library.commit_changes("platform", "Capture external document changes before chat run")
        result = runtime_manager.start(cfg)
        revision = library.commit_changes(
            f"role:{role.id}", f"Documents updated from channel {channel.name}")
        if revision:   # Agent 直接写目录的改动也进平台审计,与 API 写入口径一致
            self.store.audit(f"role:{role.id}", "documents_committed",
                             detail=f"project={channel.project_id} revision={revision[:10]}")
        tasks_dir = cfg.env.get("MISSIONCREW_TASKS_DIR")
        task_sync_errors = (sync_task_files(
            self.store, channel.project_id or "", Path(tasks_dir), f"role:{role.id}")
            if tasks_dir else [])
        if tasks_dir:
            write_task_files(self.store, channel.project_id or "", Path(tasks_dir))
        if task_sync_errors:
            self.store.audit(
                f"role:{role.id}", "task_workspace_sync_failed",
                detail="; ".join(task_sync_errors))

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
            self._post_failure(
                channel, role_id, msg_id, root_id, depth,
                f"@{role_id}(后端 {backend.id})执行失败: {result.summary}")
            return

        reply = (result.output or result.summary or "(无输出)").strip()
        if task_sync_errors:
            reply += ("\n\n(MissionCrew 任务文件同步失败："
                      + "；".join(task_sync_errors) + ")")
        project = self.store.get_project(channel.project_id or "")
        if project and role.id == project.orchestrator_role_id:
            reply = self._apply_orchestrator_actions(project, role.id, reply,
                                                     root_id=root_id, depth=depth)
        elif ACTION_RE.search(reply):
            # 非主控回复中的控制动作:剥离并明示未执行,避免读者误以为已生效
            reply = ACTION_RE.sub("", reply).strip()
            reply += "\n\n(检测到平台控制动作,但只有项目主控可以执行,未生效)"
        # 运行中先实时展示模型输出；最终回复确定后，如果 text/stdout 与即将
        # 发布的 Agent 消息完全一致，就移除重复事件。部分输出或带进度的输出保留。
        self.store.remove_duplicate_reply_output(run_id, reply)
        self.store.update_chat_run(run_id, "done", backend_id=backend.id)
        # 主控回复中的 @ 才会分派；执行角色回复自动返回主控(深度 +1)。
        self.post(channel.id, role_id, reply, author_type="agent",
                  reply_to=msg_id, root_id=root_id, depth=depth + 1,
                  runtime_id=backend.id, model=backend.model, effort=cfg.effort)

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
        # 模型在工具阶梯里则带上对应档位/成本;只影响本次执行,不写回注册表。
        spec = next((m for m in b.models if m.get("name", "") == role.model), None)
        if spec:
            b = replace(b, model=role.model, tier=spec.get("tier", b.tier),
                        cost_per_run=float(spec.get("cost", b.cost_per_run)))
        else:
            b = replace(b, model=role.model)
        model = b.model or "(CLI 默认)"
        combo = f"{b.id}+{model}" + (f"+effort={role.effort}" if role.effort else "")
        return b, f"角色固定组合 {combo}"

    def _assemble(self, channel: Channel, role: Role, backend, msg_id: int) -> ExecutionConfig:
        workdir = Path(channel.workdir) if channel.workdir \
            else mc_home() / "channels" / channel.id
        workdir.mkdir(parents=True, exist_ok=True)

        project_section = ""
        orchestrator_section = ""
        project = None
        workspace = None
        env = {}
        allowed_dirs = []
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
                env["MISSIONCREW_GUIDELINES_DIR"] = str(workspace.guidelines)
                env["MISSIONCREW_SKILLS_DIR"] = str(workspace.skills)
                env["MISSIONCREW_TASKS_DIR"] = str(workspace.tasks)
                allowed_dirs = project_allowed_dirs(project, library, workspace.root)
                if role.id == project.orchestrator_role_id:
                    orchestrator_section = self._orchestrator_section(project)

        is_orchestrator = bool(project and role.id == project.orchestrator_role_id)
        orchestrator_id = project.orchestrator_role_id if project else ""
        known_roles = {r.id for r in self.store.list_roles(channel.project_id or "")}

        history_records = []
        trigger_message = self.store.get_message(msg_id)
        for m in self.store.recent_messages(channel.id, HISTORY_WINDOW):
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

        # 只有主控拿到项目角色名册；执行角色只接收当前任务简报，完成后由
        # 平台自动回传主控，不知道也不能横向调度其他执行角色。
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
                "- 只有你（项目主控）可以在回复中 @其他角色。需要接手时，为对方写清"
                "背景、要求和验收标准。\n"
                "- 不需要协作就不要 @任何角色；不要 @你自己，不要编造不存在的角色。\n"
                "- 角色名册（仅主控可见，各自定位供你选人参考）：\n" + roster
            )
        else:
            collaboration_section = (
                "- 你不是项目主控，看不到其他执行角色名册，也不能 @或调度其他角色。\n"
                "- 只提交本次任务的完整结果；完成或失败后，平台会自动把结果交回"
                "项目主控，由主控检查并继续后续流程。"
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
            orchestrator_section=orchestrator_section,
            channel_history_path=channel_history_path,
            collaboration_section=collaboration_section,
        )
        context_version = hashlib.sha256(common_body.encode("utf-8")).hexdigest()[:16]
        common_prompt = DURABLE_CONTEXT_TEMPLATE.format(
            context_version=context_version, common_body=common_body)
        turn_prompt = TURN_PROMPT.format(
            trigger=json.dumps(trigger_record, ensure_ascii=False, indent=2))
        recovery_prompt = RECOVERY_PROMPT.format(
            history=json.dumps(history_records, ensure_ascii=False, indent=2),
            turn_prompt=turn_prompt,
        )
        prompt = common_prompt + "\n" + recovery_prompt

        session_key = f"{channel.id}::{role.id}"

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

        def _load_session() -> tuple[str, str]:
            latest = self.store.get_chat_session(session_key)
            if not _compatible_session(latest):
                if latest:
                    self.store.delete_chat_session(session_key)
                return "", ""
            assert latest is not None
            return (str(latest["runtime_session_id"]),
                    str(latest["context_version"]))

        def _save_session(runtime_session_id: str, accepted_context: str) -> None:
            if not runtime_session_id:
                self.store.delete_chat_session(session_key)
                return
            self.store.put_chat_session(
                session_key, channel.id, role.id, backend.id, backend.adapter,
                str(workdir.resolve()), runtime_session_id,
                accepted_context or previous_context,
            )

        env["MISSIONCREW_SESSION_KEY"] = session_key
        runtime_policy = RuntimePolicy(
            readable_paths=list(allowed_dirs),
            writable_paths=list(allowed_dirs),
            skill_paths=[str(workspace.skills)] if workspace is not None else [],
        )
        return ExecutionConfig(
            task_id=f"chat_{channel.id}", stage_name="chat", backend=backend,
            prompt=prompt, workdir=str(workdir), runtime_policy=runtime_policy,
            env=env, timeout=CHAT_TIMEOUT,
            effort=role.effort,
            session_key=session_key, session_id=session_id,
            common_prompt=common_prompt, turn_prompt=turn_prompt,
            recovery_prompt=recovery_prompt, context_version=context_version,
            context_changed=bool(session_id and previous_context != context_version),
            load_session=_load_session,
            save_session=_save_session,
        )

    @staticmethod
    def _decoded_mentions(message: dict) -> list[str]:
        raw = message.get("mentions", "[]")
        try:
            mentions = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return []
        return [str(item) for item in mentions] if isinstance(mentions, list) else []

    def _message_record(self, message: dict, role: Optional[Role] = None,
                        project=None, known_roles: Optional[set[str]] = None) -> dict:
        """把数据库消息转换为边界明确的 JSON 记录，并按执行角色脱敏。"""
        author = str(message.get("author", ""))
        author_type = str(message.get("author_type", ""))
        content = str(message.get("content", ""))
        mentions = self._decoded_mentions(message)
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

            content = MENTION_RE.sub(_visible_mention, content)
            mentions = [
                item if item in {role.id, orchestrator_id} or item not in known_roles
                else "其他执行角色"
                for item in mentions
            ]

        record = {
            "id": int(message.get("id", 0)),
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
                    },
                    "message_count": len(records),
                    "last_message_id": records[-1]["id"] if records else None,
                    "messages": records,
                }

            canonical_records = [self._message_record(m) for m in messages]
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
            for c in self.store.list_channels(project.id)) or "(无)"
        boards = "\n".join(
            f"- {_short(b.id)}({b.name}):{b.description or '无描述'};组件 "
            + (", ".join(f"{w.id}/{w.type}" for w in b.layout) or "无")
            for b in self.store.list_boards(project.id)) or "(无)"
        guidelines = "\n".join(
            f"- {g.name}:{g.description or '未填写 description'}"
            f"{'[停用]' if not g.enabled else ''}"
            for g in project.guidelines) or "(无)"
        skills = "\n".join(
            f"- {s.id}({s.name or s.id}){'[停用]' if not s.enabled else ''}"
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
        """执行主控回复中的受限平台动作；其他角色的相同文本只会作为普通回复。"""
        project_id = project.id
        reports = []
        for match in ACTION_RE.finditer(reply):
            try:
                action = json.loads(match.group(1))
                kind = action.get("action")
                if kind == "post_message":
                    reports.append(self._action_post_message(
                        project_id, role_id, action, root_id, depth))
                    continue
                if kind in ("save_guideline", "save_skill", "write_document"):
                    reports.append(self._apply_project_config_action(
                        project, role_id, action))
                    continue
                if kind not in ("create_channel", "create_board",
                                "update_board", "delete_board"):
                    raise ValueError(f"不支持的动作: {kind}")
                raw_id = str(action.get("id", "")).strip()
                if not CONTROL_ID_RE.fullmatch(raw_id):
                    raise ValueError("id 只能包含字母、数字、下划线、连字符")
                item_id = f"{project_id}:{raw_id}"
                if kind == "create_channel":
                    if self.store.get_channel(item_id):
                        raise ValueError("频道已存在")
                    workdir = self._resolve_channel_workdir(
                        project, str(action.get("workdir", "")).strip())
                    channel = Channel(
                        id=item_id, name=str(action.get("name") or raw_id),
                        project_id=project_id, purpose=str(action.get("purpose", "")),
                        workdir=workdir, created_by_role_id=role_id,
                    )
                    self.store.put_channel(channel)
                    where = f"(工作目录 {workdir})" if workdir else ""
                    reports.append(f"已创建频道 #{channel.name}{where}")
                    self.store.audit(role_id, "channel_created",
                                     detail=f"project={project_id} channel={item_id}")
                elif kind in ("create_board", "update_board"):
                    board = self.store.get_board(item_id)
                    if kind == "create_board" and board:
                        raise ValueError("面板已存在")
                    if kind == "update_board" and (not board or board.project_id != project_id):
                        raise ValueError("面板不存在")
                    board = board or Board(id=item_id, project_id=project_id,
                                           created_by_role_id=role_id)
                    if "layout" in action:   # 不携带 layout 时保留现有布局
                        board.layout = self._validate_board_layout(action["layout"])
                    board.name = str(action.get("name", board.name or raw_id))
                    board.description = str(action.get("description", board.description))
                    self.store.put_board(board)
                    reports.append(f"已{'创建' if kind == 'create_board' else '更新'}面板 {board.name}")
                    self.store.audit(role_id, kind,
                                     detail=f"project={project_id} board={item_id}")
                elif kind == "delete_board":
                    board = self.store.get_board(item_id)
                    if not board or board.project_id != project_id:
                        raise ValueError("面板不存在")
                    self.store.delete_board(item_id)
                    reports.append(f"已删除面板 {board.name}")
                    self.store.audit(role_id, "board_deleted",
                                     detail=f"project={project_id} board={item_id}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                reports.append(f"控制动作未执行：{exc}")
        cleaned = ACTION_RE.sub("", reply).strip()
        if reports:
            cleaned = "\n\n".join(x for x in (cleaned, "平台操作：" + "；".join(reports)) if x)
        return cleaned or "(主控动作已处理)"

    def _apply_project_config_action(self, project, role_id: str,
                                     action: dict) -> str:
        """校验并执行主控生成的项目配置；所有写入都限定在当前项目。"""
        kind = action.get("action")
        if kind == "write_document":
            path = safe_relative_path(str(action.get("path", "")))
            content = action.get("content", "")
            if not isinstance(content, str):
                raise ValueError("document content 必须是字符串")
            revision = library_for(project.id).write(
                path, content, actor=f"role:{role_id}",
                message=str(action.get("message") or f"Generate {path}"),
            )
            self.store.audit(role_id, "document_saved",
                             detail=f"project={project.id} path={path} revision={revision}")
            return f"已保存文档 {path}"

        enabled = action.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")

        if kind == "save_guideline":
            markdown = action.get("markdown", "")
            if not isinstance(markdown, str):
                raise ValueError("save_guideline.markdown 必须是字符串")
            guideline = GuidelineDocument.from_markdown(markdown, enabled)
            if not CONTROL_ID_RE.fullmatch(guideline.name):
                raise ValueError("准则 name 只能包含字母、数字、下划线、连字符")
            original_name = str(action.get("original_name", "")).strip()
            if original_name and not CONTROL_ID_RE.fullmatch(original_name):
                raise ValueError("original_name 只能包含字母、数字、下划线、连字符")
            replaced_names = {guideline.name, original_name} - {""}
            project.guidelines = [
                g for g in project.guidelines if g.name not in replaced_names]
            project.guidelines.append(guideline)
            self.store.put_project(project)
            write_guideline_context(project)
            self.store.audit(role_id, "guideline_saved",
                             detail=f"project={project.id} guideline={guideline.name}")
            return f"已保存准则文档 {guideline.name}"

        raw_id = str(action.get("id", "")).strip()
        if not CONTROL_ID_RE.fullmatch(raw_id):
            raise ValueError("id 只能包含字母、数字、下划线、连字符")
        markdown = action.get("markdown")
        if markdown is not None:
            if not isinstance(markdown, str):
                raise ValueError("save_skill.markdown 必须是字符串")
            saved = save_project_skill_markdown(
                self.store, project, raw_id, markdown, enabled=enabled, actor=role_id)
            return f"已保存 Skill {saved.name or raw_id}"
        skill = ProjectSkill(
            id=raw_id, name=str(action.get("name", "")),
            description=str(action.get("description", "")),
            instructions=str(action.get("instructions", "")),
            enabled=enabled,
        )
        saved = save_project_skill(self.store, project, skill, actor=role_id)
        return f"已保存 Skill {saved.name or raw_id}"

    def _action_post_message(self, project_id: str, role_id: str, action: dict,
                             root_id: int, depth: int) -> str:
        """主控向本项目任意频道发消息(调度闭环):@ 正常触发级联,
        共享同一条协作链的执行次数预算,防止跨频道绕开防爆炸限制。"""
        raw = str(action.get("channel", "")).strip()
        content = str(action.get("content", "")).strip()
        if not raw or not content:
            raise ValueError("post_message 需要 channel 和 content")
        cid = raw if raw.startswith(f"{project_id}:") else f"{project_id}:{raw}"
        channel = self.store.get_channel(cid) or self.store.get_channel(raw)
        if channel is None or channel.project_id != project_id:
            raise ValueError(f"频道不存在或不属于本项目: {raw}")
        self.post(channel.id, role_id, content, author_type="agent",
                  root_id=root_id, depth=depth + 1)
        self.store.audit(role_id, "orchestrator_post",
                         detail=f"project={project_id} channel={channel.id}")
        return f"已在 #{channel.name} 发布消息"

    @staticmethod
    def _resolve_channel_workdir(project, requested: str) -> Optional[str]:
        """频道工作目录只能落在项目代码仓内;未指定且仅一个仓时默认使用它。"""
        repos = [str(Path(r).expanduser()) for r in project.repo_paths()]
        if not requested:
            if len(repos) == 1 and Path(repos[0]).is_dir():
                return repos[0]
            return None
        target = Path(requested).expanduser()
        if not target.is_dir():
            raise ValueError(f"workdir 不存在: {requested}")
        resolved = target.resolve()
        for repo in repos:
            try:
                resolved.relative_to(Path(repo).resolve())
                return str(target)
            except ValueError:
                continue
        raise ValueError("workdir 必须是项目代码仓路径或其子目录")

    @staticmethod
    def _validate_board_layout(raw_layout) -> list[BoardWidget]:
        if not isinstance(raw_layout, list):
            raise ValueError("面板 layout 必须是列表")
        widgets = []
        seen = set()
        for raw in raw_layout:
            widget = BoardWidget(**raw)
            if not CONTROL_ID_RE.fullmatch(widget.id) or widget.id in seen:
                raise ValueError("组件 id 必须合法且不能重复")
            if (widget.x < 0 or widget.y < 0 or not 1 <= widget.width <= 12
                    or not 1 <= widget.height <= 100):
                raise ValueError("组件位置必须非负，宽度为 1..12，高度为 1..100")
            if widget.type not in BOARD_WIDGET_TYPES:
                raise ValueError(f"未知组件类型 {widget.type},"
                                 f"可用: {', '.join(sorted(BOARD_WIDGET_TYPES))}")
            seen.add(widget.id)
            widgets.append(widget)
        return widgets
