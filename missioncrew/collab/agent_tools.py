"""MissionCrew Agent Tool 的鉴权、权限与动作注册表。

Runtime 只需要调用统一 HTTP/CLI 边界，不再依赖最终回复中的文本块来完成平台操作。
旧 ``missioncrew-action`` 解析器仅作为迁移入口，并转调本模块的同一动作实现。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import board_sources
from .content_channels import content_channel, rebind_content_channel
from .documents import (document_resource_url, library_for,
                        normalize_document_resource_urls, safe_relative_path)
from .guidelines import save_guideline
from .recycle_bin import (RecycleConflictError, list_recycle_items,
                          purge_recycle_item, recycle_bin_url,
                          recycle_dashboard, recycle_document,
                          recycle_guideline, recycle_skill, recycle_task,
                          restore_recycle_item)
from .resource_urls import (automation_resource_url, channel_resource_url,
                            dashboard_resource_url, guideline_resource_url,
                            skill_resource_url, task_resource_url)
from .skills import save_project_skill, save_project_skill_markdown
from .workspace import chat_workspace_dir, write_task_files
from ..core.models import (BOARD_KINDS, BOARD_WIDGET_TYPES, Board,
                           BoardDataSource, BoardWidget,
                           Channel, Project, ProjectSkill,
                           Task)
from ..core.store import Store


CONTROL_ID_RE = re.compile(r"[\w-]+")
REQUEST_ID_RE = re.compile(r"[\w-]{1,100}")
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
TOKEN_LIFETIME_SECONDS = 7 * 24 * 60 * 60
TOKEN_RENEWAL_WINDOW_SECONDS = 24 * 60 * 60
LOGGER = logging.getLogger(__name__)


class AgentToolError(Exception):
    """可安全、结构化地返回给 Runtime 的动作错误。"""

    def __init__(self, code: str, message: str, status_code: int = 400,
                 *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class DispatchInactiveError(RuntimeError):
    """运行中 Agent 发布消息时,发起 run 已停止或频道正在停止。

    与参数错误区分开:这是瞬态的停止互斥信号,不是调用方可修正的输入问题。
    """


@dataclass(frozen=True)
class AgentIdentity:
    token_id: str
    project_id: str
    channel_id: str
    role_id: str               # kind=automation 时是自动化脚本 id
    issued_scopes: tuple[str, ...]
    run_id: int = 0
    kind: str = "run"          # run(绑定聊天 Run) | automation(脚本身份)

    @property
    def is_automation(self) -> bool:
        return self.kind == "automation"

    @property
    def actor(self) -> str:
        """审计与产物归属使用的身份前缀,可区分角色与脚本触发。"""
        prefix = "automation" if self.is_automation else "role"
        return f"{prefix}:{self.role_id}"


@dataclass(frozen=True)
class AgentRunContext:
    run_id: int
    channel_id: str
    root_id: int
    depth: int


ACTION_DEFINITIONS = {
    "task.create": {
        "description": "创建一个可绑定多个 Channel 的 Issue 化 Task",
        "orchestrator_only": False,
        "arguments": {
            "title": "任务标题", "summary": "一句话简介", "body": "正文",
            "labels": ("标签数组;`文本` 或 `属性: 值` 高级标签,"
                       "如 owner: 张三"),
            "channel_ids": "可选绑定的 Channel id 数组",
            "status": "状态文本,如 待处理/处理中/已阻塞/已完成,缺省 待处理",
        },
    },
    "task.update": {
        "description": "以乐观并发方式更新任务的需求字段",
        "orchestrator_only": False,
        "arguments": {
            "id": "任务 id", "snapshot_updated_at": "读取任务时的更新时间",
            "title/summary/body/status/labels/channel_ids": "要更新的字段",
        },
    },
    "task.brief": {
        "description": "为 Task 追加一条状态简报，并可同时更新状态",
        "orchestrator_only": False,
        "arguments": {
            "id": "任务 id", "content": "状态简报正文",
            "status": "可选状态文本,如 待处理/处理中/已阻塞/已完成",
        },
    },
    "task.delete": {
        "description": "把 Task 及其状态简报移入项目回收站",
        "orchestrator_only": True,
        "arguments": {"id": "任务 id"},
    },
    "document.publish": {
        "description": "把文本或二进制文件发布到项目版本化文档库",
        "orchestrator_only": False,
        "arguments": {
            "path": "文档库相对路径",
            "content 或 content_base64": "文件内容",
            "overwrite": "目标存在时是否显式覆盖，默认 false",
            "message": "可选版本说明",
        },
    },
    "document.rename": {
        "description": "移动或重命名项目版本化文档并保留 Git 历史",
        "orchestrator_only": False,
        "arguments": {
            "source": "现有文档库相对路径",
            "target": "新的文档库相对路径；目标必须不存在",
        },
    },
    "document.delete": {
        "description": "把项目版本化文档移入统一回收站并保留 Git 历史",
        "orchestrator_only": True,
        "arguments": {"path": "文档库相对路径"},
    },
    "message.publish": {
        "description": (
            "向本项目频道发布消息；mentions 参数是唯一的角色派发通道；"
            "返回的 dispatched(实际启动的角色)非空时结束当前 turn，"
            "chain_budget 给出本条协作链已用次数与上限"
        ),
        "orchestrator_only": True,
        "arguments": {
            "channel": "项目内频道短 id",
            "content": "消息正文；正文里的任何 @ 都不产生调度",
            "mentions": "要显式调度的角色 id 数组；不传则只发消息不派发",
        },
    },
    "channel.runs.list": {
        "description": (
            "按需查询当前 Channel 中 queued/running/waiting_user 的角色运行；"
            "状态不会预先写入聊天上下文"
        ),
        "orchestrator_only": True,
        "automation_allowed": False,
        "arguments": {},
    },
    "channel.run.stop": {
        "description": "停止当前 Channel 中指定的活动角色运行；应先查询取得 run_id",
        "orchestrator_only": True,
        "automation_allowed": False,
        "arguments": {"run_id": "channel.runs.list 返回的正整数 Run id"},
    },
    "channel.list": {
        "description": (
            "按需列出项目频道(id、名称、用途、工作目录、归档态、最近消息时间、"
            "活动运行数);频道清单不预先写入聊天上下文,新建频道前先查避免重复"
        ),
        "orchestrator_only": True,
        "arguments": {"scope": "active(默认)/archived/all"},
    },
    "channel.create": {
        "description": "创建项目频道(先用 channel.list 确认 id 未被占用)",
        "orchestrator_only": True,
        "arguments": {"id": "频道短 id", "name": "名称", "purpose": "用途",
                      "workdir": "可选项目仓库目录"},
    },
    "dashboard.save": {
        "description": "创建或更新项目面板(组件网格或任务看板)",
        "orchestrator_only": True,
        "arguments": {
            "id": "面板短 id", "name": "名称", "description": "用途",
            "layout": "可选组件数组(widgets 形态)",
            "kind": "可选 widgets/taskboard",
            "source": "taskboard 数据源 id(内置 built-in 或自定义源短 id)",
            "filters": ("taskboard 筛选列数组 [{title,query,color}],query 是"
                        "标签表达式(& | ! 与括号,`属性: *` 匹配带该属性的"
                        "任务);新建时缺省按数据源状态取值生成 status 列"),
            "group_by": "可选分组属性名;非空时在每个筛选列内按属性取值分组,保留 filters",
        },
    },
    "dashboard.delete": {
        "description": "把项目面板移入统一回收站",
        "orchestrator_only": True,
        "arguments": {"id": "面板短 id"},
    },
    "board_source.save": {
        "description": (
            "创建或更新自定义任务数据源;cards 按 id 整体同步为该源的任务"
            "(新增/覆盖/删除),适合配自动化脚本定时同步 GitCode/GitHub "
            "Issue 等外部列表,再用 dashboard.save 建任务看板绑定该源"
        ),
        "orchestrator_only": True,
        "arguments": {
            "id": "数据源短 id(不能与内置源重名)",
            "name": "名称", "description": "用途",
            "columns": ("可选状态取值数组 [{value,color}](兼容旧版 "
                        "{key,title,color}),缺省四态"),
            "cards": ("可选卡片数组,按 id 整体同步;每张必须有 id、title,"
                      "可选 summary、status(状态文本)、labels、updated_at、"
                      "meta、url(外部链接);新任务会走项目自动处理规则"),
            "mode": "create/update/upsert,缺省 upsert",
        },
    },
    "board_source.delete": {
        "description": "删除自定义看板数据源(仍被面板引用时拒绝)",
        "orchestrator_only": True,
        "arguments": {"id": "数据源短 id"},
    },
    "guideline.save": {
        "description": "保存完整准则 Markdown",
        "orchestrator_only": True,
        "arguments": {"markdown": "含 name/description frontmatter 的全文",
                      "enabled": "是否启用", "original_name": "重命名前名称"},
    },
    "guideline.delete": {
        "description": "把准则 Markdown 移入统一回收站并保留 Git 历史",
        "orchestrator_only": True,
        "arguments": {"name": "准则 name"},
    },
    "skill.save": {
        "description": "保存完整 Skill Markdown",
        "orchestrator_only": True,
        "arguments": {"id": "Skill id", "markdown": "完整 SKILL.md",
                      "enabled": "是否启用"},
    },
    "skill.delete": {
        "description": "把完整 Skill 包移入统一回收站",
        "orchestrator_only": True,
        "arguments": {"id": "Skill id"},
    },
    "automation.save": {
        "description": (
            "创建或更新项目自动化脚本;脚本经统一定时入口按 cron 或手动触发,"
            "以专属 token 调用平台动作"
        ),
        "orchestrator_only": True,
        "arguments": {
            "id": "脚本短 id", "name": "名称", "description": "用途",
            "script": "脚本全文(有 shebang 按可执行文件运行,否则用 bash)",
            "cron": "五段 crontab;空字符串表示仅手动触发",
            "enabled": "是否启用定时触发",
            "actions": "允许调用的动作白名单数组;不传保留现值",
            "timeout_seconds": "单次运行超时秒数",
        },
    },
    "automation.delete": {
        "description": "删除项目自动化脚本及其运行记录",
        "orchestrator_only": True,
        "arguments": {"id": "脚本短 id"},
    },
    "recycle.list": {
        "description": "列出当前项目统一回收站",
        "orchestrator_only": True,
        "arguments": {},
    },
    "recycle.restore": {
        "description": "从项目回收站恢复资源",
        "orchestrator_only": True,
        "arguments": {"id": "回收站条目 id"},
    },
    "recycle.purge": {
        "description": "永久删除一个项目回收站条目",
        "orchestrator_only": True,
        "arguments": {"id": "回收站条目 id"},
    },
}

AUTOMATION_ACTIONS = frozenset(
    name for name, definition in ACTION_DEFINITIONS.items()
    if definition.get("automation_allowed", True))

ACTION_ARGUMENTS = {
    "task.create": {
        "title", "summary", "body", "labels", "channel_ids", "status",
    },
    "task.update": {
        "id", "snapshot_updated_at", "title", "summary", "body", "status",
        "labels", "channel_ids",
    },
    "task.brief": {"id", "content", "status"},
    "task.delete": {"id"},
    "document.publish": {
        "path", "content", "content_base64", "overwrite", "message",
    },
    "document.rename": {"source", "target"},
    "document.delete": {"path"},
    "message.publish": {"channel", "content", "mentions"},
    "channel.runs.list": set(),
    "channel.run.stop": {"run_id"},
    "channel.list": {"scope"},
    "channel.create": {"id", "name", "purpose", "workdir"},
    "dashboard.save": {"id", "name", "description", "layout", "mode",
                       "kind", "source", "filters", "group_by"},
    "dashboard.delete": {"id"},
    "board_source.save": {"id", "name", "description", "columns", "cards",
                          "mode"},
    "board_source.delete": {"id"},
    "guideline.save": {"markdown", "enabled", "original_name"},
    "guideline.delete": {"name"},
    "skill.save": {
        "id", "markdown", "enabled", "name", "description", "instructions",
    },
    "skill.delete": {"id"},
    "automation.save": {
        "id", "name", "description", "script", "cron", "enabled", "actions",
        "timeout_seconds",
    },
    "automation.delete": {"id"},
    "recycle.list": set(),
    "recycle.restore": {"id"},
    "recycle.purge": {"id"},
}

# 成功的写操作要在发起调用的会话中留下可见回执。message.publish 和停止动作
# 本身已经生成聊天消息；查询动作是只读操作，这些动作都不额外插入回执。
CONVERSATION_RECEIPT_ACTIONS = (
    frozenset(ACTION_DEFINITIONS) - {
        "message.publish", "channel.runs.list", "channel.run.stop", "recycle.list",
    }
)


class AgentActionService:
    """统一执行 Agent 可请求的 MissionCrew 平台动作。"""

    def __init__(self, store: Store, post_message: Callable[..., int],
                 stop_run: Callable[..., dict]):
        self.store = store
        self._post_message = post_message
        self._stop_run = stop_run

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def allowed_actions(self, project: Project, role_id: str) -> list[str]:
        # 无主控项目里每个角色都拥有主控级动作
        is_orchestrator = project.controls_platform(role_id)
        return [
            name for name, definition in ACTION_DEFINITIONS.items()
            if is_orchestrator or not definition["orchestrator_only"]
        ]

    def ensure_token_file(self, project: Project, channel: Channel, role_id: str,
                          workspace_root: Path,
                          run_id: int = 0) -> tuple[Path, str]:
        """准备稳定路径的令牌文件；写操作令牌与一个 Run 确定绑定。"""
        token_file = workspace_root / ".agent-tool-token"
        now = time.time()
        try:
            current = token_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            current = ""
        if current:
            row = self.store.get_agent_token(self._hash_token(current))
            expected_scopes = self.allowed_actions(project, role_id)
            if (row and row["project_id"] == project.id
                    and row["channel"] == channel.id and row["role_id"] == role_id
                    and int(row.get("run_id") or 0) == run_id
                    and row["revoked_at"] is None
                    and row["expires_at"] > now + TOKEN_RENEWAL_WINDOW_SECONDS
                    and row["scopes"] == expected_scopes):
                try:
                    token_file.chmod(0o600)
                except OSError:
                    pass
                return token_file, str(row["token_id"])

        self.store.revoke_agent_tokens(
            project_id=project.id, channel=channel.id, role_id=role_id)
        token = secrets.token_urlsafe(32)
        token_id = uuid.uuid4().hex
        scopes = self.allowed_actions(project, role_id)
        self.store.put_agent_token(
            token_hash=self._hash_token(token), token_id=token_id,
            project_id=project.id, channel=channel.id, role_id=role_id,
            run_id=run_id, scopes=scopes,
            expires_at=now + TOKEN_LIFETIME_SECONDS,
        )
        workspace_root.mkdir(parents=True, exist_ok=True)
        temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=workspace_root,
                    prefix=".agent-tool-token.", delete=False) as handle:
                handle.write(token + "\n")
                temporary = Path(handle.name)
            temporary.chmod(0o600)
            temporary.replace(token_file)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.store.audit(
            f"role:{role_id}", "agent_tool_token_issued",
            detail=(f"project={project.id} channel={channel.id} role={role_id} "
                    f"run={run_id} token={token_id} scopes={','.join(scopes)}"),
        )
        return token_file, token_id

    def deactivate_run_token(self, project_id: str, channel_id: str,
                             role_id: str, workspace_root: Path,
                             run_id: int) -> None:
        """撤销本轮 capability，并仅在文件仍指向本轮时删除稳定入口。"""
        if run_id <= 0:
            return
        token_file = workspace_root / ".agent-tool-token"
        try:
            current = token_file.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            current = ""
        current_row = (
            self.store.get_agent_token(self._hash_token(current))
            if current else None
        )
        self.store.revoke_agent_tokens(
            project_id=project_id, channel=channel_id, role_id=role_id,
            run_id=run_id)
        if (current_row and int(current_row.get("run_id") or 0) == run_id
                and current_row.get("project_id") == project_id
                and current_row.get("channel") == channel_id
                and current_row.get("role_id") == role_id):
            try:
                token_file.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning(
                    "Failed to remove expired Agent Tool token file: %s",
                    token_file, exc_info=True)

    def authenticate(self, token: str) -> AgentIdentity:
        if not token:
            raise AgentToolError("missing_token", "缺少 Agent Tool Bearer token", 401)
        token_hash = self._hash_token(token)
        row = self.store.get_agent_token(token_hash)
        now = time.time()
        if not row or row["revoked_at"] is not None:
            raise AgentToolError("invalid_token", "Agent Tool token 无效或已撤销", 401)
        if row["expires_at"] <= now:
            raise AgentToolError("expired_token", "Agent Tool token 已过期，请开始新一轮执行", 401)
        if str(row.get("kind") or "run") == "automation":
            return self._authenticate_automation(row, token_hash)
        project = self.store.get_project(str(row["project_id"]))
        channel = self.store.get_channel(str(row["channel"]))
        role = self.store.get_role(str(row["project_id"]), str(row["role_id"]))
        if (project is None or channel is None or role is None
                or channel.project_id != project.id):
            raise AgentToolError("stale_identity", "Token 对应的项目、频道或角色已不存在", 401)
        self.store.touch_agent_token(token_hash)
        return AgentIdentity(
            token_id=str(row["token_id"]), project_id=project.id,
            channel_id=channel.id, role_id=role.id,
            issued_scopes=tuple(str(item) for item in row["scopes"]),
            run_id=int(row.get("run_id") or 0),
        )

    def _authenticate_automation(self, row: dict, token_hash: str) -> AgentIdentity:
        """脚本身份不绑定 Run;权限以自动化脚本当前的动作白名单为事实源。"""
        automation = self.store.get_automation(str(row["role_id"]))
        project = self.store.get_project(str(row["project_id"]))
        if (automation is None or project is None
                or automation.project_id != project.id):
            raise AgentToolError(
                "stale_identity", "Token 对应的自动化脚本或项目已不存在", 401)
        self.store.touch_agent_token(token_hash)
        scopes = tuple(action for action in automation.actions
                       if action in AUTOMATION_ACTIONS)
        return AgentIdentity(
            token_id=str(row["token_id"]), project_id=project.id,
            channel_id="", role_id=automation.id,
            issued_scopes=scopes, run_id=0, kind="automation",
        )

    def issue_automation_token(self, automation, ttl_seconds: float) -> str:
        """为一次脚本运行签发短期 token;上一次未撤销的旧 token 一并作废。"""
        self.revoke_automation_tokens(automation.id)
        token = secrets.token_urlsafe(32)
        self.store.put_agent_token(
            token_hash=self._hash_token(token), token_id=uuid.uuid4().hex,
            project_id=automation.project_id, channel="",
            role_id=automation.id, run_id=0, kind="automation",
            scopes=list(automation.actions),
            expires_at=time.time() + max(60.0, ttl_seconds),
        )
        return token

    def revoke_automation_tokens(self, automation_id: str) -> int:
        return self.store.revoke_agent_tokens(
            role_id=automation_id, kind="automation")

    def capabilities(self, identity: AgentIdentity) -> dict:
        project = self._identity_project(identity)
        allowed = self._identity_actions(project, identity)
        return {
            "project_id": identity.project_id,
            "channel_id": identity.channel_id,
            "role_id": identity.role_id,
            "token_id": identity.token_id,
            "actions": {
                name: {key: value for key, value in ACTION_DEFINITIONS[name].items()
                       if key not in {"orchestrator_only", "automation_allowed"}}
                for name in allowed
            },
        }

    def execute(self, identity: AgentIdentity, action: str, arguments: dict,
                run_id: Optional[int], request_id: str) -> dict:
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise AgentToolError(
                "invalid_request_id", "request_id 只能包含字母、数字、下划线、连字符",
            )
        if identity.is_automation:
            # 脚本身份不绑定聊天 Run;消息发布自成协作链,回执由运行记录承载。
            context = AgentRunContext(run_id=0, channel_id="", root_id=0, depth=0)
            try:
                result = self._execute_action(identity, action, arguments, context)
            except AgentToolError as exc:
                self._audit_call(identity, action, request_id, 0, "failed", exc.code)
                raise
            except (TypeError, ValueError, binascii.Error) as exc:
                error = AgentToolError("invalid_arguments", str(exc), 400)
                self._audit_call(identity, action, request_id, 0, "failed", error.code)
                raise error from exc
            except Exception as exc:
                LOGGER.exception(
                    "Agent Tool action failed: action=%s automation=%s",
                    action, identity.role_id)
                error = AgentToolError(
                    "internal_error", "MissionCrew 执行动作时发生内部错误", 500,
                    retryable=True,
                )
                self._audit_call(identity, action, request_id, 0, "failed", error.code)
                raise error from exc
            self._audit_call(identity, action, request_id, 0, "success", "")
            return result
        bound_run_id = identity.run_id
        if bound_run_id <= 0:
            error = AgentToolError(
                "run_unbound", "当前 Agent Tool capability 未绑定活动 Run", 409)
            self._audit_call(identity, action, request_id, 0, "failed", error.code)
            raise error
        if run_id is not None and run_id != bound_run_id:
            error = AgentToolError(
                "run_mismatch", "客户端 run_id 与当前 capability 绑定的 Run 不一致",
                403)
            self._audit_call(
                identity, action, request_id, bound_run_id, "denied", error.code)
            raise error
        run_id = bound_run_id
        run = self.store.get_chat_run(run_id)
        if (not run or run["channel"] != identity.channel_id
                or run["role_id"] != identity.role_id):
            error = AgentToolError(
                "run_mismatch", "run_id 不属于当前 Token 的频道和角色", 403)
            self._audit_call(identity, action, request_id, run_id, "denied", error.code)
            raise error
        if run["status"] not in ("queued", "running", "waiting_user"):
            error = AgentToolError(
                "run_inactive", "run_id 已结束，不能再执行 MissionCrew 操作", 409)
            self._audit_call(identity, action, request_id, run_id, "failed", error.code)
            raise error
        context = AgentRunContext(
            run_id=run_id, channel_id=str(run["channel"]),
            root_id=int(run["root_id"]), depth=int(run["depth"]),
        )
        try:
            result = self._execute_action(identity, action, arguments, context)
        except AgentToolError as exc:
            self._audit_call(identity, action, request_id, run_id, "failed", exc.code)
            raise
        except (TypeError, ValueError, binascii.Error) as exc:
            error = AgentToolError("invalid_arguments", str(exc), 400)
            self._audit_call(identity, action, request_id, run_id, "failed", error.code)
            raise error from exc
        except Exception as exc:
            LOGGER.exception(
                "Agent Tool action failed: action=%s run=%s role=%s",
                action, run_id, identity.role_id)
            error = AgentToolError(
                "internal_error", "MissionCrew 执行动作时发生内部错误", 500,
                retryable=True,
            )
            self._audit_call(identity, action, request_id, run_id, "failed", error.code)
            raise error from exc
        self._audit_call(identity, action, request_id, run_id, "success", "")
        self._record_conversation_receipt(identity, action, result, context)
        return result

    def execute_legacy(self, project: Project, role_id: str, action: dict,
                       root_id: int, depth: int) -> dict:
        """旧文本块兼容入口；业务逻辑仍只在 canonical action 中实现。"""
        legacy_kind = action.get("action")
        mapping = {
            "create_channel": "channel.create",
            "post_message": "message.publish",
            "create_board": "dashboard.save",
            "update_board": "dashboard.save",
            "delete_board": "dashboard.delete",
            "save_guideline": "guideline.save",
            "save_skill": "skill.save",
            "write_document": "document.publish",
        }
        canonical = mapping.get(str(legacy_kind))
        if canonical is None:
            raise AgentToolError("unsupported_action", f"不支持的动作: {legacy_kind}")
        arguments = dict(action)
        arguments.pop("action", None)
        if legacy_kind in ("create_board", "update_board"):
            arguments["mode"] = "create" if legacy_kind == "create_board" else "update"
        # 旧 post_message 块与新契约一致:只有块里显式携带 mentions 数组才派发,
        # 正文中的 @[role] 一律是普通文字。
        if legacy_kind == "write_document":
            arguments["overwrite"] = True
        root_message = self.store.get_message(root_id)
        channel_id = (str(root_message["channel"]) if root_message else
                      next((channel.id for channel in self.store.list_channels(project.id)), ""))
        identity = AgentIdentity(
            token_id="legacy", project_id=project.id, channel_id=channel_id,
            role_id=role_id, issued_scopes=tuple(self.allowed_actions(project, role_id)),
        )
        context = AgentRunContext(
            run_id=0, channel_id=channel_id, root_id=root_id, depth=depth)
        try:
            result = self._execute_action(identity, canonical, arguments, context)
        except (AgentToolError, TypeError, ValueError, binascii.Error) as exc:
            code = exc.code if isinstance(exc, AgentToolError) else "invalid_arguments"
            self._audit_call(identity, canonical, "legacy", 0, "failed", code)
            raise
        self._audit_call(identity, canonical, "legacy", 0, "success", "legacy_block")
        return result

    def _record_conversation_receipt(
            self, identity: AgentIdentity, action: str, result: dict,
            context: AgentRunContext) -> None:
        """把成功的 Agent 写操作作为平台消息展示，不让回执影响动作结果。"""
        if action not in CONVERSATION_RECEIPT_ACTIONS:
            return
        summary = str(result.get("summary") or f"已完成 {action}")
        content = (
            f"@{identity.role_id} 使用 MissionCrew Tool · `{action}`：{summary}"
        )
        try:
            self._post_message(
                context.channel_id, "platform", content, author_type="platform",
                root_id=context.root_id, depth=context.depth + 1,
                mention_spans=[], context={
                    "agent_tool": {
                        "action": action,
                        "role_id": identity.role_id,
                        "run_id": context.run_id,
                    },
                }, kind="agent_tool",
            )
        except Exception:
            # 资源写入已经成功，回执异常不能把成功动作伪装成失败并诱导 Agent
            # 重试；保留错误日志供平台排查。
            LOGGER.exception(
                "Failed to record Agent Tool conversation receipt: "
                "action=%s run=%s role=%s",
                action, context.run_id, identity.role_id,
            )

    def _identity_project(self, identity: AgentIdentity) -> Project:
        project = self.store.get_project(identity.project_id)
        if identity.is_automation:
            automation = self.store.get_automation(identity.role_id)
            if project is None or automation is None:
                raise AgentToolError("stale_identity", "项目或自动化脚本已不存在", 401)
            return project
        role = self.store.get_role(identity.project_id, identity.role_id)
        if project is None or role is None:
            raise AgentToolError("stale_identity", "项目或角色已不存在", 401)
        return project

    def _identity_actions(self, project: Project,
                          identity: AgentIdentity) -> list[str]:
        """身份可用动作:脚本身份只看白名单,角色身份按主控级权限位过滤。"""
        if identity.is_automation:
            return [action for action in identity.issued_scopes
                    if action in AUTOMATION_ACTIONS]
        return self.allowed_actions(project, identity.role_id)

    def _execute_action(self, identity: AgentIdentity, action: str,
                        arguments: dict, context: AgentRunContext) -> dict:
        if not isinstance(arguments, dict):
            raise AgentToolError("invalid_arguments", "arguments 必须是对象")
        definition = ACTION_DEFINITIONS.get(action)
        if definition is None:
            raise AgentToolError("unsupported_action", f"不支持的动作: {action}")
        unknown = set(arguments) - ACTION_ARGUMENTS[action]
        if unknown:
            raise AgentToolError(
                "invalid_arguments", f"{action} 包含未知参数: {', '.join(sorted(unknown))}")
        project = self._identity_project(identity)
        allowed = self._identity_actions(project, identity)
        if action not in allowed or action not in identity.issued_scopes:
            subject = ("自动化脚本" if identity.is_automation
                       else f"角色 @{identity.role_id}")
            raise AgentToolError(
                "permission_denied", f"{subject} 无权执行 {action}", 403,
            )
        handlers = {
            "task.create": self._create_task,
            "task.update": self._update_task,
            "task.brief": self._add_task_brief,
            "task.delete": self._delete_task,
            "document.publish": self._publish_document,
            "document.rename": self._rename_document,
            "document.delete": self._delete_document,
            "message.publish": self._publish_message,
            "channel.runs.list": self._list_channel_runs,
            "channel.run.stop": self._stop_channel_run,
            "channel.list": self._list_channels,
            "channel.create": self._create_channel,
            "dashboard.save": self._save_dashboard,
            "dashboard.delete": self._delete_dashboard,
            "board_source.save": self._save_board_source,
            "board_source.delete": self._delete_board_source,
            "guideline.save": self._save_guideline,
            "guideline.delete": self._delete_guideline,
            "skill.save": self._save_skill,
            "skill.delete": self._delete_skill,
            "automation.save": self._save_automation,
            "automation.delete": self._delete_automation,
            "recycle.list": self._list_recycle_bin,
            "recycle.restore": self._restore_recycle_item,
            "recycle.purge": self._purge_recycle_item,
        }
        return handlers[action](project, identity, arguments, context)

    @staticmethod
    def _require_chat_identity(identity: AgentIdentity) -> None:
        if identity.is_automation or not identity.channel_id:
            raise AgentToolError(
                "permission_denied", "该动作只允许当前 Channel 内的角色运行调用", 403)

    def _list_channel_runs(self, project: Project, identity: AgentIdentity,
                           _arguments: dict, context: AgentRunContext) -> dict:
        """只在显式调用时返回当前频道的活动 Run 快照。"""
        self._require_chat_identity(identity)
        runs = []
        for row in self.store.active_chat_runs(identity.channel_id):
            role = self.store.get_role(project.id, str(row["role_id"]))
            run_id = int(row["id"])
            runs.append({
                "run_id": run_id,
                "role_id": str(row["role_id"]),
                "role_name": role.name if role is not None else "",
                "status": str(row["status"]),
                "backend_id": str(row.get("backend_id") or ""),
                "model": str(row.get("model") or ""),
                "effort": str(row.get("effort") or ""),
                "created_at": float(row["created_at"]),
                "is_current_run": run_id == context.run_id,
                "stoppable": run_id != context.run_id,
            })
        return {
            "summary": f"当前 Channel 有 {len(runs)} 个活动角色运行",
            "channel_id": identity.channel_id,
            "runs": runs,
        }

    def _stop_channel_run(self, project: Project, identity: AgentIdentity,
                          arguments: dict, context: AgentRunContext) -> dict:
        """停止当前频道内显式选中的 Run，不允许调用者终止自身运行。"""
        self._require_chat_identity(identity)
        run_id = arguments.get("run_id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise AgentToolError("invalid_arguments", "run_id 必须是正整数")
        if run_id == context.run_id:
            raise AgentToolError(
                "cannot_stop_self",
                "不能通过当前工具调用停止自己的 Run；请直接结束当前 turn",
                409,
            )
        target = self.store.get_chat_run(run_id)
        if target is None or str(target["channel"]) != identity.channel_id:
            # 不区分不存在与属于其他频道，避免通过 run_id 探测频道外运行。
            raise AgentToolError(
                "run_not_found", "当前 Channel 中不存在指定的 Run", 404)
        if str(target["status"]) not in {"queued", "running", "waiting_user"}:
            raise AgentToolError(
                "run_inactive", "指定 Run 已结束，不能再次停止", 409)
        try:
            result = self._stop_run(
                run_id, actor=identity.actor,
                actor_label=(f"主控 @{identity.role_id}" if project.has_orchestrator
                             else f"角色 @{identity.role_id}"))
        except ValueError as exc:
            # 校验后目标可能恰好自然结束；向调用者返回稳定的并发冲突语义。
            raise AgentToolError("run_inactive", str(exc), 409) from exc
        result["summary"] = (
            f"已停止当前 Channel 中 @{result['role_id']} 的运行 {run_id}")
        result["run_id"] = run_id
        return result

    def _create_task(self, project: Project, identity: AgentIdentity,
                     arguments: dict, _context: AgentRunContext) -> dict:
        from types import SimpleNamespace
        from .tasks import auto_process_task, create_task

        task = create_task(
            self.store, project.id, **arguments,
            actor=identity.actor,
            fallback_channel_id=identity.channel_id,
        )
        self._refresh_task_snapshot(project.id, identity)
        self.store.audit(
            identity.actor, "agent_task_created", task.id,
            f"project={project.id}")
        # 新建 Task 命中项目自动处理规则时立即派发(常见于脚本同步外部任务)
        auto = auto_process_task(
            self.store, SimpleNamespace(post=self._post_message), task)
        url = task_resource_url(project.id, task.id)
        summary = f"已创建任务 [{task.title}]({url})"
        if auto:
            summary += f";已按自动规则(`{auto['rule_query']}`)派发"
        result = {
            "summary": summary,
            "task": task.to_dict(), "resource_url": url,
        }
        if auto:
            result["auto_dispatch"] = {
                "rule_query": auto["rule_query"], "sent": auto["sent"]}
        return result

    def _update_task(self, project: Project, identity: AgentIdentity,
                     arguments: dict, _context: AgentRunContext) -> dict:
        from .tasks import update_task

        task_id = str(arguments.get("id", "")).strip()
        task = self.store.get_task(task_id)
        if task is None or task.project_id != project.id:
            raise AgentToolError("task_not_found", f"任务不存在: {task_id}", 404)
        changes = {key: value for key, value in arguments.items()
                   if key not in {"id", "snapshot_updated_at"}}
        try:
            update_task(
                self.store, task,
                snapshot_updated_at=arguments.get("snapshot_updated_at"),
                changes=changes, actor=identity.actor,
            )
        except ValueError as exc:
            if "重新读取" in str(exc):
                raise AgentToolError("version_conflict", str(exc), 409) from exc
            if "已归档" in str(exc):
                raise AgentToolError("task_archived", str(exc), 409) from exc
            raise AgentToolError("invalid_arguments", str(exc), 400) from exc
        self._refresh_task_snapshot(project.id, identity)
        self.store.audit(
            identity.actor, "agent_task_updated", task.id,
            f"project={project.id}")
        url = task_resource_url(project.id, task.id)
        return {
            "summary": f"已更新任务 [{task.title}]({url})",
            "task": task.to_dict(), "resource_url": url,
        }

    def _add_task_brief(self, project: Project, identity: AgentIdentity,
                        arguments: dict, _context: AgentRunContext) -> dict:
        from .tasks import add_task_brief

        task_id = str(arguments.get("id", "")).strip()
        task = self.store.get_task(task_id)
        if task is None or task.project_id != project.id:
            raise AgentToolError("task_not_found", f"任务不存在: {task_id}", 404)
        try:
            brief = add_task_brief(
                self.store, task, content=arguments.get("content", ""),
                status=arguments.get("status"), author=identity.role_id,
                author_type="agent",
            )
        except ValueError as exc:
            if "已归档" in str(exc):
                raise AgentToolError("task_archived", str(exc), 409) from exc
            raise AgentToolError("invalid_arguments", str(exc), 400) from exc
        self._refresh_task_snapshot(project.id, identity)
        url = task_resource_url(project.id, task.id)
        return {
            "summary": f"已为任务 [{task.title}]({url}) 追加状态简报",
            "task": task.to_dict(), "brief": brief, "resource_url": url,
        }

    def _delete_task(self, project: Project, identity: AgentIdentity,
                     arguments: dict, _context: AgentRunContext) -> dict:
        task_id = str(arguments.get("id", "")).strip()
        task = self.store.get_task(task_id)
        if task is None or task.project_id != project.id:
            raise AgentToolError("task_not_found", f"任务不存在: {task_id}", 404)
        item = recycle_task(
            self.store, project, task, actor=identity.actor)
        self._refresh_task_snapshot(project.id, identity)
        return {
            "summary": f"已将任务 {task.title} 移入项目回收站",
            "deleted": True,
            "recycle_item": item,
            "resource_url": recycle_bin_url(project.id),
        }

    def _refresh_task_snapshot(self, project_id: str,
                               identity: AgentIdentity) -> None:
        if identity.is_automation:
            return   # 脚本没有聊天工作区;角色快照在其下一轮装配时刷新
        tasks_dir = (chat_workspace_dir(
            project_id, identity.channel_id, identity.role_id) / "tasks")
        write_task_files(self.store, project_id, tasks_dir)

    def _publish_document(self, project: Project, identity: AgentIdentity,
                          arguments: dict, _context: AgentRunContext) -> dict:
        path = safe_relative_path(str(arguments.get("path", "")))
        has_text = "content" in arguments
        has_base64 = "content_base64" in arguments
        if has_text == has_base64:
            raise AgentToolError(
                "invalid_arguments", "document.publish 必须且只能提供 content 或 content_base64")
        if has_text:
            content = arguments["content"]
            if not isinstance(content, str):
                raise AgentToolError("invalid_arguments", "content 必须是字符串")
            payload = content.encode("utf-8")
        else:
            encoded = arguments["content_base64"]
            if not isinstance(encoded, str):
                raise AgentToolError("invalid_arguments", "content_base64 必须是字符串")
            try:
                payload = base64.b64decode(encoded, validate=True)
            except binascii.Error as exc:
                raise AgentToolError("invalid_arguments", "content_base64 不是有效 Base64") from exc
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise AgentToolError("file_too_large", "单个发布文件不能超过 50 MB", 413)
        overwrite = arguments.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise AgentToolError("invalid_arguments", "overwrite 必须是布尔值")
        actor = identity.actor
        try:
            revision = library_for(project.id).write_bytes(
                path, payload, actor=actor,
                message=str(arguments.get("message") or f"Publish {path}"),
                overwrite=overwrite,
            )
        except FileExistsError as exc:
            raise AgentToolError(
                "already_exists",
                f"{exc}；更新已有文档请传 overwrite=true"
                "(publish-file 加 --overwrite)",
                409) from exc
        self.store.audit(
            actor, "document_published",
            detail=(f"project={project.id} path={path} size={len(payload)} "
                    f"revision={revision}"),
        )
        url = document_resource_url(project.id, path)
        return {
            "summary": f"已发布文档 [{path}]({url})",
            "path": path, "size": len(payload), "revision": revision,
            "resource_url": url,
        }

    def _rename_document(self, project: Project, identity: AgentIdentity,
                         arguments: dict, _context: AgentRunContext) -> dict:
        source = safe_relative_path(str(arguments.get("source", "")))
        target = safe_relative_path(str(arguments.get("target", "")))
        if source == target:
            raise AgentToolError(
                "invalid_arguments", "document.rename 的 source 和 target 不能相同")

        # 文档页的对话绑定跟随路径迁移；若目标已有独立对话，拒绝重命名，
        # 避免两个页面会话无法确定应保留哪一个。
        source_channel = content_channel(self.store, project.id, "docs", source)
        target_channel = content_channel(self.store, project.id, "docs", target)
        if (source_channel is not None and target_channel is not None
                and source_channel.id != target_channel.id):
            raise AgentToolError(
                "channel_conflict", f"目标文档已有独立对话: {target}", 409)

        actor = identity.actor
        try:
            revision = library_for(project.id).rename(
                source, target, actor=actor)
        except FileNotFoundError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        except FileExistsError as exc:
            raise AgentToolError("already_exists", str(exc), 409) from exc
        rebind_content_channel(
            self.store, project, "docs", source, target, target)
        self.store.audit(
            actor, "document_renamed",
            detail=(f"project={project.id} source={source} target={target} "
                    f"revision={revision}"),
        )
        url = document_resource_url(project.id, target)
        return {
            "summary": f"已将文档 `{source}` 重命名为 [{target}]({url})",
            "source": source, "target": target, "revision": revision,
            "resource_url": url,
        }

    def _delete_document(self, project: Project, identity: AgentIdentity,
                         arguments: dict, _context: AgentRunContext) -> dict:
        path = safe_relative_path(str(arguments.get("path", "")))
        actor = identity.actor
        try:
            item = recycle_document(self.store, project, path, actor=actor)
        except FileNotFoundError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        return {
            "summary": f"已将文档 {path} 移入项目回收站",
            "path": path,
            "deleted": True,
            "revision": item["revision"],
            "recycle_item": item,
            "resource_url": document_resource_url(project.id, path),
        }

    def _publish_message(self, project: Project, identity: AgentIdentity,
                         arguments: dict, context: AgentRunContext) -> dict:
        raw_channel = str(arguments.get("channel", "")).strip()
        content = arguments.get("content", "")
        if not raw_channel or not isinstance(content, str) or not content.strip():
            raise AgentToolError(
                "invalid_arguments", "message.publish 需要 channel 和非空 content")
        channel_id = (raw_channel if raw_channel.startswith(f"{project.id}:")
                      else f"{project.id}:{raw_channel}")
        channel = self.store.get_channel(channel_id) or self.store.get_channel(raw_channel)
        if channel is None or channel.project_id != project.id:
            raise AgentToolError(
                "channel_not_found", f"频道不存在或不属于本项目: {raw_channel}", 404)
        mentions = arguments.get("mentions", [])
        if (not isinstance(mentions, list)
                or not all(isinstance(item, str) for item in mentions)):
            raise AgentToolError("invalid_arguments", "mentions 必须是角色 id 数组")
        unique_mentions: list[str] = []
        for role_id in mentions:
            if not identity.is_automation and role_id == identity.role_id:
                raise AgentToolError("invalid_arguments", "不能调度自己")
            target_role = self.store.get_role(project.id, role_id)
            if target_role is None or not target_role.enabled:
                raise AgentToolError("role_not_found", f"角色不存在: {role_id}", 404)
            if role_id not in unique_mentions:
                unique_mentions.append(role_id)
        # mentions 参数是唯一的派发通道:直接生成可见的 @role 前缀与结构化
        # 范围,交给 post 校验后触发。正文里的任何 @(含 @[role])都是普通文字。
        prefix = " ".join(f"@{role_id}" for role_id in unique_mentions)
        publish_content = (prefix + ("\n\n" if prefix else "") + content.strip())
        mention_spans: list[dict] = []
        offset = 0
        for role_id in unique_mentions:
            mention_spans.append({"role_id": role_id, "start": offset,
                                  "end": offset + len(role_id) + 1})
            offset += len(role_id) + 2   # "@role" + 分隔空格
        publish_content = normalize_document_resource_urls(
            publish_content, project.id, [library_for(project.id).root])
        try:
            if identity.is_automation:
                # 脚本消息自成协作链;author_type=automation 让路由按人类
                # 规则处理提及(单角色直达、多角色收敛主控),但无提及不派发。
                message_id = self._post_message(
                    channel.id, identity.role_id, publish_content,
                    author_type="automation", mention_spans=mention_spans,
                )
            else:
                message_id = self._post_message(
                    channel.id, identity.role_id, publish_content,
                    author_type="agent",
                    root_id=context.root_id, depth=context.depth + 1,
                    mention_spans=mention_spans,
                    origin_run_id=context.run_id or None,
                )
        except DispatchInactiveError as exc:
            raise AgentToolError("run_inactive", str(exc), 409) from exc
        self.store.audit(
            identity.actor, "agent_message_published",
            detail=(f"project={project.id} channel={channel.id} message={message_id} "
                    f"run={context.run_id}"),
        )
        url = channel_resource_url(project.id, channel.id)
        result = {
            "summary": f"已在 [#{channel.name}]({url}) 发布消息",
            "message_id": message_id, "resource_url": url,
        }
        if context.root_id:
            # 主控看不到预算余量;随返回值告知,避免临近上限时盲目派发
            result["chain_budget"] = {
                "used": self.store.count_chain_runs(context.root_id),
                "limit": project.max_chain_runs,
            }
        if unique_mentions:
            # 派发可能被协作链预算兜底丢弃:回传实际启动名单,
            # 避免主控误以为角色已开工。
            started = {run["role_id"] for run in
                       self.store.chat_runs_for_trigger(message_id)}
            dropped = [r for r in unique_mentions if r not in started]
            result["dispatched"] = [r for r in unique_mentions if r in started]
            if result["dispatched"]:
                result["handoff"] = "end_turn"
                result["resume"] = (
                    "不要向仍在执行的角色追问中间状态；MissionCrew 会在已派发角色"
                    "完成或失败后自动启动你的新 turn 交回结果"
                )
            if dropped:
                result["not_dispatched"] = dropped
                result["summary"] += (
                    "；注意:" + "、".join(f"@{r}" for r in dropped)
                    + " 未启动(协作链执行数已达上限)")
        return result

    def _list_channels(self, project: Project, identity: AgentIdentity,
                       arguments: dict, _context: AgentRunContext) -> dict:
        """按需返回项目频道清单;与前端频道列表同一口径,归档频道也可查。"""
        scope = str(arguments.get("scope") or "active").strip().lower()
        if scope not in {"active", "archived", "all"}:
            raise AgentToolError(
                "invalid_arguments", "scope 只能是 active/archived/all")
        channels = self.store.list_channels(project.id, include_archived=True)
        if scope != "all":
            channels = [c for c in channels if c.archived == (scope == "archived")]
        run_counts = self.store.active_chat_run_counts()
        rows = [{
            "id": c.id.removeprefix(f"{project.id}:"),
            "name": c.name,
            "purpose": c.purpose,
            "workdir": c.workdir or "",
            "workdir_missing": bool(c.workdir) and not Path(c.workdir).is_dir(),
            "archived": c.archived,
            "last_message_at": c.last_message_at,
            "active_run_count": run_counts.get(c.id, 0),
            "is_current": c.id == identity.channel_id,
            "resource_url": channel_resource_url(project.id, c.id),
        } for c in channels]
        return {
            "summary": f"项目 {project.id} 的 {scope} 频道共 {len(rows)} 个",
            "scope": scope,
            "channels": rows,
        }

    def _create_channel(self, project: Project, identity: AgentIdentity,
                        arguments: dict, _context: AgentRunContext) -> dict:
        raw_id = self._control_id(arguments.get("id"))
        channel_id = f"{project.id}:{raw_id}"
        if self.store.get_channel(channel_id):
            raise AgentToolError("already_exists", "频道已存在", 409)
        workdir = self._resolve_channel_workdir(
            project, str(arguments.get("workdir", "")).strip())
        channel = Channel(
            id=channel_id, name=str(arguments.get("name") or raw_id),
            project_id=project.id, purpose=str(arguments.get("purpose", "")),
            workdir=workdir, created_by_role_id=identity.role_id,
        )
        self.store.put_channel(channel)
        self.store.audit(
            identity.actor, "channel_created",
            detail=f"project={project.id} channel={channel_id}")
        url = channel_resource_url(project.id, channel.id)
        where = f"(工作目录 {workdir})" if workdir else ""
        return {
            "summary": f"已创建频道 [#{channel.name}]({url}){where}",
            "channel": channel.to_dict(), "resource_url": url,
        }

    def _save_dashboard(self, project: Project, identity: AgentIdentity,
                        arguments: dict, _context: AgentRunContext) -> dict:
        raw_id = self._control_id(arguments.get("id"))
        board_id = f"{project.id}:{raw_id}"
        board = self.store.get_board(board_id)
        mode = arguments.get("mode", "upsert")
        if mode not in ("create", "update", "upsert"):
            raise AgentToolError(
                "invalid_arguments", "mode 必须是 create/update/upsert")
        if mode == "create" and board:
            raise AgentToolError("already_exists", "面板已存在", 409)
        if mode == "update" and (not board or board.project_id != project.id):
            raise AgentToolError("not_found", "面板不存在", 404)
        created = board is None
        board = board or Board(
            id=board_id, project_id=project.id,
            created_by_role_id=identity.role_id)
        if "layout" in arguments:
            board.layout = self._validate_board_layout(arguments["layout"])
        board.name = str(arguments.get("name", board.name or raw_id))
        board.description = str(arguments.get("description", board.description))
        if "kind" in arguments:
            if arguments["kind"] not in BOARD_KINDS:
                raise AgentToolError(
                    "invalid_arguments",
                    f"kind 必须是 {'/'.join(sorted(BOARD_KINDS))}")
            board.kind = arguments["kind"]
        if "source" in arguments:
            source = str(arguments["source"] or "").strip()
            if not board_sources.source_exists(self.store, project.id, source):
                raise AgentToolError("invalid_arguments", f"未知数据源: {source}")
            board.source = source
        if "filters" in arguments:
            board.filters = board_sources.validate_filters(arguments["filters"])
        if "group_by" in arguments:
            board.group_by = str(arguments.get("group_by") or "").strip()
        # 与 Web 端一致:新建看板未显式给筛选列时按数据源状态取值物化默认列
        if (board.kind == "taskboard" and created
                and "filters" not in arguments):
            board.filters = board_sources.default_filters(
                board_sources.source_status_values(
                    self.store, project.id, board.source))
        self.store.put_board(board)
        self.store.audit(
            identity.actor, "dashboard_saved",
            detail=f"project={project.id} board={board_id}")
        url = dashboard_resource_url(project.id, board.id)
        return {
            "summary": f"已{'创建' if created else '更新'}面板 [{board.name}]({url})",
            "dashboard": board.to_dict(), "resource_url": url,
        }

    def _delete_dashboard(self, project: Project, identity: AgentIdentity,
                          arguments: dict, _context: AgentRunContext) -> dict:
        raw_id = self._control_id(arguments.get("id"))
        board_id = f"{project.id}:{raw_id}"
        board = self.store.get_board(board_id)
        if board is None or board.project_id != project.id:
            raise AgentToolError("not_found", "面板不存在", 404)
        item = recycle_dashboard(
            self.store, project, board_id, actor=identity.actor)
        return {"summary": f"已将面板 {board.name} 移入项目回收站",
                "deleted": True, "recycle_item": item}

    def _save_board_source(self, project: Project, identity: AgentIdentity,
                           arguments: dict, _context: AgentRunContext) -> dict:
        from types import SimpleNamespace
        from .tasks import auto_process_task

        raw_id = self._control_id(arguments.get("id"))
        if board_sources.normalize_source_id(raw_id) \
                == board_sources.BUILTIN_SOURCE_ID:
            raise AgentToolError(
                "invalid_arguments", f"{raw_id} 是内置数据源,不能覆盖")
        full_id = board_sources.custom_source_full_id(project.id, raw_id)
        record = self.store.get_board_datasource(full_id)
        mode = arguments.get("mode", "upsert")
        if mode not in ("create", "update", "upsert"):
            raise AgentToolError(
                "invalid_arguments", "mode 必须是 create/update/upsert")
        if mode == "create" and record:
            raise AgentToolError("already_exists", "数据源已存在", 409)
        if mode == "update" and record is None:
            raise AgentToolError("not_found", "数据源不存在", 404)
        created = record is None
        record = record or BoardDataSource(
            id=full_id, project_id=project.id,
            created_by_role_id=identity.role_id)
        if "name" in arguments:
            record.name = str(arguments.get("name") or "").strip()
        if not record.name:
            record.name = raw_id
        if "description" in arguments:
            record.description = str(arguments.get("description") or "")
        if "columns" in arguments:
            record.status_values = board_sources.validate_source_columns(
                arguments["columns"])
        self.store.put_board_datasource(record)
        stats = None
        if "cards" in arguments:
            cards = board_sources.validate_source_cards(arguments["cards"])
            chat = SimpleNamespace(post=self._post_message)
            stats = board_sources.sync_source_tasks(
                self.store, project.id, raw_id, cards,
                raw_columns=arguments.get("columns"),
                status_values=record.status_values,
                auto_process=lambda task: auto_process_task(
                    self.store, chat, task))
        self.store.audit(
            identity.actor, "board_source_saved",
            detail=(f"project={project.id} source={full_id} "
                    + (f"cards={stats['created']}+{stats['updated']}"
                       f"-{stats['removed']}" if stats else "cards=(未同步)")))
        summary = f"已{'创建' if created else '更新'}任务数据源 {record.name}"
        if stats:
            summary += (f"(新增 {stats['created']}、更新 {stats['updated']}、"
                        f"删除 {stats['removed']})")
        return {"summary": summary, "source": record.to_dict(),
                **({"sync": stats} if stats else {})}

    def _delete_board_source(self, project: Project, identity: AgentIdentity,
                             arguments: dict,
                             _context: AgentRunContext) -> dict:
        raw_id = self._control_id(arguments.get("id"))
        full_id = board_sources.custom_source_full_id(project.id, raw_id)
        record = self.store.get_board_datasource(full_id)
        if record is None or record.project_id != project.id:
            raise AgentToolError("not_found", "数据源不存在", 404)
        used_by = [b for b in self.store.list_boards(project.id)
                   if b.kind == "taskboard" and b.source == raw_id]
        if used_by:
            names = "、".join(b.name or b.id for b in used_by)
            raise AgentToolError(
                "in_use", f"数据源仍被面板使用: {names};请先删除或改绑这些面板",
                409)
        removed = 0
        for task in self.store.list_tasks(project.id, source_id=raw_id):
            self.store.delete_task(task.id)
            removed += 1
        self.store.delete_board_datasource(full_id)
        self.store.audit(
            identity.actor, "board_source_deleted",
            detail=f"project={project.id} source={full_id} tasks={removed}")
        return {"summary": (f"已删除任务数据源 {record.name or raw_id}"
                            f"(连带 {removed} 个任务)"),
                "deleted": True}

    def _save_automation(self, project: Project, identity: AgentIdentity,
                         arguments: dict, _context: AgentRunContext) -> dict:
        from .automations import save_automation

        try:
            automation, created = save_automation(
                self.store, project.id,
                id=str(arguments.get("id", "")),
                name=arguments.get("name"),
                description=arguments.get("description"),
                script=arguments.get("script"),
                cron=arguments.get("cron"),
                enabled=arguments.get("enabled"),
                actions=arguments.get("actions"),
                timeout_seconds=arguments.get("timeout_seconds"),
                actor=identity.actor,
                created_by_role_id=identity.role_id,
            )
        except ValueError as exc:
            raise AgentToolError("invalid_arguments", str(exc), 400) from exc
        schedule = (f"cron `{automation.cron}`" if automation.cron
                    else "仅手动触发")
        return {
            "summary": (f"已{'创建' if created else '更新'}自动化脚本 "
                        f"{automation.name}({schedule})"),
            "automation": automation.to_dict(),
            "resource_url": automation_resource_url(project.id, automation.id),
        }

    def _delete_automation(self, project: Project, identity: AgentIdentity,
                           arguments: dict, _context: AgentRunContext) -> dict:
        from .automations import delete_automation

        try:
            automation = delete_automation(
                self.store, self, project.id,
                str(arguments.get("id", "")), actor=identity.actor)
        except ValueError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        return {
            "summary": f"已删除自动化脚本 {automation.name}",
            "deleted": True,
        }

    def _save_guideline(self, project: Project, identity: AgentIdentity,
                        arguments: dict, _context: AgentRunContext) -> dict:
        enabled = arguments.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AgentToolError("invalid_arguments", "enabled 必须是布尔值")
        markdown = arguments.get("markdown", "")
        if not isinstance(markdown, str):
            raise AgentToolError("invalid_arguments", "markdown 必须是字符串")
        original_name = str(arguments.get("original_name", "")).strip()
        try:
            guideline, revision = save_guideline(
                self.store, project, markdown, enabled=enabled,
                actor=identity.actor, original_name=original_name)
        except FileExistsError as exc:
            raise AgentToolError("already_exists", str(exc), 409) from exc
        url = guideline_resource_url(project.id, guideline.name)
        return {
            "summary": f"已保存准则文档 [{guideline.name}]({url})",
            "guideline": guideline.to_dict(), "resource_url": url,
            "revision": revision,
        }

    def _delete_guideline(self, project: Project, identity: AgentIdentity,
                          arguments: dict, _context: AgentRunContext) -> dict:
        name = self._control_id(arguments.get("name"))
        try:
            item = recycle_guideline(
                self.store, project, name, actor=identity.actor)
        except FileNotFoundError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        return {
            "summary": f"已将准则文档 {name} 移入项目回收站",
            "deleted": True,
            "revision": item["revision"],
            "recycle_item": item,
            "resource_url": guideline_resource_url(project.id, name),
        }

    def _save_skill(self, project: Project, identity: AgentIdentity,
                    arguments: dict, _context: AgentRunContext) -> dict:
        raw_id = self._control_id(arguments.get("id"))
        enabled = arguments.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AgentToolError("invalid_arguments", "enabled 必须是布尔值")
        markdown = arguments.get("markdown")
        actor = identity.actor
        if markdown is not None:
            if not isinstance(markdown, str):
                raise AgentToolError("invalid_arguments", "markdown 必须是字符串")
            saved, revision = save_project_skill_markdown(
                self.store, project, raw_id, markdown, enabled=enabled, actor=actor)
        else:
            skill = ProjectSkill(
                id=raw_id, name=str(arguments.get("name", "")),
                description=str(arguments.get("description", "")),
                instructions=str(arguments.get("instructions", "")), enabled=enabled)
            saved, revision = save_project_skill(
                self.store, project, skill, actor=actor)
        url = skill_resource_url(project.id, saved.id)
        return {
            "summary": f"已保存 Skill [{saved.name or raw_id}]({url})",
            "skill": saved.__dict__, "resource_url": url,
            "revision": revision,
        }

    def _delete_skill(self, project: Project, identity: AgentIdentity,
                      arguments: dict, _context: AgentRunContext) -> dict:
        skill_id = self._control_id(arguments.get("id"))
        try:
            item = recycle_skill(
                self.store, project, skill_id,
                actor=identity.actor)
        except FileNotFoundError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        return {
            "summary": f"已将 Skill {skill_id} 移入项目回收站",
            "deleted": True,
            "recycle_item": item,
            "revision": item["revision"],
            "resource_url": skill_resource_url(project.id, skill_id),
        }

    def _list_recycle_bin(self, project: Project, _identity: AgentIdentity,
                          _arguments: dict, _context: AgentRunContext) -> dict:
        items = list_recycle_items(project.id)
        return {
            "summary": f"项目回收站共有 {len(items)} 项",
            "items": items,
            "resource_url": recycle_bin_url(project.id),
        }

    def _restore_recycle_item(self, project: Project, identity: AgentIdentity,
                              arguments: dict,
                              _context: AgentRunContext) -> dict:
        item_id = str(arguments.get("id", "")).strip()
        try:
            item = restore_recycle_item(
                self.store, project, item_id, actor=identity.actor)
        except RecycleConflictError as exc:
            raise AgentToolError("already_exists", str(exc), 409) from exc
        except FileNotFoundError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        if item["resource_type"] == "task":
            self._refresh_task_snapshot(project.id, identity)
        return {
            "summary": f"已从项目回收站恢复 {item['name']}",
            "item": item,
            "resource_url": item["restored_resource_url"],
        }

    def _purge_recycle_item(self, project: Project, identity: AgentIdentity,
                            arguments: dict,
                            _context: AgentRunContext) -> dict:
        item_id = str(arguments.get("id", "")).strip()
        try:
            item = purge_recycle_item(
                self.store, project, item_id, actor=identity.actor)
        except FileNotFoundError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        return {
            "summary": f"已永久删除回收项 {item['name']}",
            "purged": True,
            "item": item,
            "resource_url": recycle_bin_url(project.id),
        }

    @staticmethod
    def _control_id(value) -> str:
        raw_id = str(value or "").strip()
        if not CONTROL_ID_RE.fullmatch(raw_id):
            raise AgentToolError(
                "invalid_arguments", "id 只能包含字母、数字、下划线、连字符")
        return raw_id

    @staticmethod
    def _resolve_channel_workdir(project: Project, requested: str) -> Optional[str]:
        repos = [str(Path(item).expanduser()) for item in project.repo_paths()]
        if not requested:
            if len(repos) == 1 and Path(repos[0]).is_dir():
                return repos[0]
            return None
        target = Path(requested).expanduser()
        if not target.is_dir():
            raise AgentToolError("invalid_arguments", f"workdir 不存在: {requested}")
        resolved = target.resolve()
        for repo in repos:
            try:
                resolved.relative_to(Path(repo).resolve())
                return str(target)
            except ValueError:
                continue
        raise AgentToolError(
            "permission_denied", "workdir 必须是项目代码仓路径或其子目录", 403)

    @staticmethod
    def _validate_board_layout(raw_layout) -> list[BoardWidget]:
        if not isinstance(raw_layout, list):
            raise AgentToolError("invalid_arguments", "面板 layout 必须是列表")
        widgets: list[BoardWidget] = []
        seen: set[str] = set()
        for raw in raw_layout:
            widget = BoardWidget(**raw)
            if not CONTROL_ID_RE.fullmatch(widget.id) or widget.id in seen:
                raise AgentToolError(
                    "invalid_arguments", "组件 id 必须合法且不能重复")
            if (widget.x < 0 or widget.y < 0 or not 1 <= widget.width <= 12
                    or not 1 <= widget.height <= 100):
                raise AgentToolError(
                    "invalid_arguments", "组件位置必须非负，宽度为 1..12，高度为 1..100")
            if widget.type not in BOARD_WIDGET_TYPES:
                raise AgentToolError(
                    "invalid_arguments",
                    f"未知组件类型 {widget.type},可用: {', '.join(sorted(BOARD_WIDGET_TYPES))}")
            seen.add(widget.id)
            widgets.append(widget)
        return widgets

    def _audit_call(self, identity: AgentIdentity, action: str, request_id: str,
                    run_id: int, status: str, code: str) -> None:
        self.store.audit(
            identity.actor, "agent_tool_called",
            detail=(f"project={identity.project_id} channel={identity.channel_id} "
                    f"role={identity.role_id} token={identity.token_id} run={run_id} "
                    f"request={request_id} action={action} status={status} code={code}"),
        )


def default_agent_tool_url() -> str:
    return os.environ.get(
        "MISSIONCREW_AGENT_TOOL_URL", "http://127.0.0.1:8321/api/agent/v1")
