"""聊天协作引擎。

协作模型:
- 人类在频道里 @角色 布置工作;角色由固定 runtime/model 执行,
  定位、能力与偏好用于协作方选人,不参与执行时路由;
- 只有项目主控能在回复中 @其他角色发起工作;执行角色看不到其他角色名册,
  完成后由平台自动把完整结果交回主控继续调度;
- 所有主控调度与执行结果都对人类完全可见,全程审计。

防失控:级联深度上限 + 单条协作链的执行总数上限 + 不响应自己 @ 自己。
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Optional

from ..runtime import adapters
from ..core.config import mc_home
from .documents import library_for
from ..core.models import (BOARD_WIDGET_TYPES, Board, BoardWidget, Channel,
                     ExecutionConfig, Role)
from .project_context import project_allowed_dirs, render_project_context
from ..core.store import Store

MENTION_RE = re.compile(r"@([\w-]+)")
MAX_DEPTH = 10         # 自动回主控也计一层;与执行总数上限配合，允许多轮调度闭环
MAX_CHAIN_RUNS = 10    # 单条协作链(同一条人类消息引发)的执行总数上限
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
CHAT_PROMPT = """\
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

# 最近对话
{history}

# 触发消息(你的任务简报,由发起者撰写)
{trigger}

# 回复要求
- 只完成触发消息交代的工作;信息不足时在回复中提出,不要臆测扩大范围。
- 你的最终回复会被完整、原样发布到聊天频道,人类可以看到。
- 回复用中文,先说结论,再简述做了什么;不要贴大段日志。
{collaboration_section}
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
- 每个角色的 runtime/模型在项目定义角色时已经固定，你不能也不需要调整；
  调度就是在角色名册中选人：结合角色定位、能力与偏好(风格/领域)挑选
  最合适的角色，@ 它并写清任务简报。
- 调度预算：@ 级联深度上限 {max_depth} 层、单条协作链最多 {max_runs} 次执行。
  复杂任务分批派发；执行角色完成后平台会把完整结果自动交回你，再派下一批。

## 项目代码仓
{repos}

## 现有频道
{channels}

## 现有面板
{boards}
"""


class ChatEngine:
    def __init__(self, store: Store, max_workers: int = 4):
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="chat-run")
        self._futures: list[Future] = []
        self._futures_lock = threading.Lock()
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
            self._post_failure(channel, role_id, msg_id, root_id, depth,
                               f"@{role_id} 执行出错: {e}")

    def _post_failure(self, channel: Channel, role_id: str, msg_id: int,
                      root_id: int, depth: int, content: str) -> None:
        """公开执行失败；非主控失败也自动交回主控决定后续动作。"""
        result_depth = depth + 1
        failure_id = self.store.add_message(
            channel.id, "platform", "platform", content, [], msg_id, root_id,
            result_depth)
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
        result = adapters.get_adapter(backend.adapter).run(cfg)
        revision = library.commit_changes(
            f"role:{role.id}", f"Documents updated from channel {channel.name}")
        if revision:   # Agent 直接写目录的改动也进平台审计,与 API 写入口径一致
            self.store.audit(f"role:{role.id}", "documents_committed",
                             detail=f"project={channel.project_id} revision={revision[:10]}")

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
        env = {}
        allowed_dirs = []
        if channel.project_id:
            project = self.store.get_project(channel.project_id)
            if project:
                library = library_for(project.id)
                project_section = render_project_context(project, backend, library)
                env["MISSIONCREW_DOCUMENTS_DIR"] = str(library.root)
                allowed_dirs = project_allowed_dirs(project, library)
                if not channel.workdir:   # 平台自有工作区才建软链,不污染真实代码仓
                    library.link_into(workdir)
                if role.id == project.orchestrator_role_id:
                    orchestrator_section = self._orchestrator_section(project)

        is_orchestrator = bool(project and role.id == project.orchestrator_role_id)
        orchestrator_id = project.orchestrator_role_id if project else ""
        known_roles = {r.id for r in self.store.list_roles(channel.project_id or "")}

        def _worker_visible(text: str) -> str:
            """执行角色的任务简报不暴露其他执行角色 id。"""
            if is_orchestrator:
                return text
            return MENTION_RE.sub(
                lambda match: (match.group(0)
                               if match.group(1) in {role.id, orchestrator_id}
                               or match.group(1) not in known_roles
                               else "[其他执行角色]"),
                text,
            )

        history_lines = []
        trigger = ""
        for m in self.store.recent_messages(channel.id, HISTORY_WINDOW):
            author = m["author"]
            if (not is_orchestrator and m["author_type"] == "agent"
                    and author != orchestrator_id):
                author = "执行角色"
            line = f"[{author}] {_worker_visible(m['content'])}"
            if m["id"] == msg_id:
                trigger = line
            elif is_orchestrator:
                history_lines.append(line)

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
        prompt = CHAT_PROMPT.format(
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
            history="\n".join(history_lines) or "(无)",
            trigger=trigger, collaboration_section=collaboration_section,
        )
        return ExecutionConfig(
            task_id=f"chat_{channel.id}", stage_name="chat", backend=backend,
            prompt=prompt, workdir=str(workdir), allowed_dirs=allowed_dirs,
            env=env, timeout=CHAT_TIMEOUT,
            effort=role.effort,
        )

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
        return ORCHESTRATOR_TEMPLATE.format(
            max_depth=MAX_DEPTH, max_runs=MAX_CHAIN_RUNS,
            repos=repos, channels=channels, boards=boards)

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

    def _action_post_message(self, project_id: str, role_id: str, action: dict,
                             root_id: int, depth: int) -> str:
        """主控向本项目任意频道发消息(调度闭环):@ 正常触发级联,
        共享同一条协作链的深度/执行数预算,防止跨频道绕开防爆炸限制。"""
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
