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

from .documents import (document_resource_url, library_for,
                        normalize_document_resource_urls, safe_relative_path)
from .guidelines import save_guideline
from .recycle_bin import (RecycleConflictError, list_recycle_items,
                          purge_recycle_item, recycle_bin_url,
                          recycle_dashboard, recycle_document,
                          recycle_guideline, recycle_skill, recycle_task,
                          restore_recycle_item)
from .resource_urls import (channel_resource_url, dashboard_resource_url,
                            guideline_resource_url, skill_resource_url,
                            task_resource_url)
from .skills import save_project_skill, save_project_skill_markdown
from .workspace import chat_workspace_dir, write_task_files
from ..core.models import (BOARD_WIDGET_TYPES, Board, BoardWidget,
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
    role_id: str
    issued_scopes: tuple[str, ...]


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
            "labels": "标签数组", "channel_ids": "绑定的 Channel id 数组",
            "status": "open/in_progress/blocked/done",
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
            "status": "可选 open/in_progress/blocked/done",
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
    "document.delete": {
        "description": "把项目版本化文档移入统一回收站并保留 Git 历史",
        "orchestrator_only": True,
        "arguments": {"path": "文档库相对路径"},
    },
    "message.publish": {
        "description": (
            "向本项目频道发布消息；mentions 参数是唯一的角色派发通道；"
            "实际派发成功后按返回的 handoff 结束当前 turn"
        ),
        "orchestrator_only": True,
        "arguments": {
            "channel": "项目内频道短 id",
            "content": "消息正文；正文里的任何 @ 都不产生调度",
            "mentions": "要显式调度的角色 id 数组；不传则只发消息不派发",
        },
    },
    "channel.create": {
        "description": "创建项目频道",
        "orchestrator_only": True,
        "arguments": {"id": "频道短 id", "name": "名称", "purpose": "用途",
                      "workdir": "可选项目仓库目录"},
    },
    "dashboard.save": {
        "description": "创建或更新项目面板",
        "orchestrator_only": True,
        "arguments": {"id": "面板短 id", "name": "名称",
                      "description": "用途", "layout": "可选组件数组"},
    },
    "dashboard.delete": {
        "description": "把项目面板移入统一回收站",
        "orchestrator_only": True,
        "arguments": {"id": "面板短 id"},
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
    "document.delete": {"path"},
    "message.publish": {"channel", "content", "mentions"},
    "channel.create": {"id", "name", "purpose", "workdir"},
    "dashboard.save": {"id", "name", "description", "layout", "mode"},
    "dashboard.delete": {"id"},
    "guideline.save": {"markdown", "enabled", "original_name"},
    "guideline.delete": {"name"},
    "skill.save": {
        "id", "markdown", "enabled", "name", "description", "instructions",
    },
    "skill.delete": {"id"},
    "recycle.list": set(),
    "recycle.restore": {"id"},
    "recycle.purge": {"id"},
}


class AgentActionService:
    """统一执行 Agent 可请求的 MissionCrew 平台动作。"""

    def __init__(self, store: Store, post_message: Callable[..., int]):
        self.store = store
        self._post_message = post_message

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def allowed_actions(self, project: Project, role_id: str) -> list[str]:
        is_orchestrator = role_id == project.orchestrator_role_id
        return [
            name for name, definition in ACTION_DEFINITIONS.items()
            if is_orchestrator or not definition["orchestrator_only"]
        ]

    def ensure_token_file(self, project: Project, channel: Channel, role_id: str,
                          workspace_root: Path) -> tuple[Path, str]:
        """为 channel×role 工作区准备可复用令牌文件，数据库只保存哈希。"""
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
            scopes=scopes, expires_at=now + TOKEN_LIFETIME_SECONDS,
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
                    f"token={token_id} scopes={','.join(scopes)}"),
        )
        return token_file, token_id

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
        )

    def capabilities(self, identity: AgentIdentity) -> dict:
        project = self._identity_project(identity)
        allowed = self.allowed_actions(project, identity.role_id)
        return {
            "project_id": identity.project_id,
            "channel_id": identity.channel_id,
            "role_id": identity.role_id,
            "token_id": identity.token_id,
            "actions": {
                name: {key: value for key, value in ACTION_DEFINITIONS[name].items()
                       if key != "orchestrator_only"}
                for name in allowed
            },
        }

    def execute(self, identity: AgentIdentity, action: str, arguments: dict,
                run_id: int, request_id: str) -> dict:
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise AgentToolError(
                "invalid_request_id", "request_id 只能包含字母、数字、下划线、连字符",
            )
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

    def _identity_project(self, identity: AgentIdentity) -> Project:
        project = self.store.get_project(identity.project_id)
        role = self.store.get_role(identity.project_id, identity.role_id)
        if project is None or role is None:
            raise AgentToolError("stale_identity", "项目或角色已不存在", 401)
        return project

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
        allowed = self.allowed_actions(project, identity.role_id)
        if action not in allowed or action not in identity.issued_scopes:
            raise AgentToolError(
                "permission_denied",
                f"角色 @{identity.role_id} 无权执行 {action}", 403,
            )
        handlers = {
            "task.create": self._create_task,
            "task.update": self._update_task,
            "task.brief": self._add_task_brief,
            "task.delete": self._delete_task,
            "document.publish": self._publish_document,
            "document.delete": self._delete_document,
            "message.publish": self._publish_message,
            "channel.create": self._create_channel,
            "dashboard.save": self._save_dashboard,
            "dashboard.delete": self._delete_dashboard,
            "guideline.save": self._save_guideline,
            "guideline.delete": self._delete_guideline,
            "skill.save": self._save_skill,
            "skill.delete": self._delete_skill,
            "recycle.list": self._list_recycle_bin,
            "recycle.restore": self._restore_recycle_item,
            "recycle.purge": self._purge_recycle_item,
        }
        return handlers[action](project, identity, arguments, context)

    def _create_task(self, project: Project, identity: AgentIdentity,
                     arguments: dict, _context: AgentRunContext) -> dict:
        from .tasks import create_task

        task = create_task(
            self.store, project.id, **arguments,
            actor=f"role:{identity.role_id}",
            fallback_channel_id=identity.channel_id,
        )
        self._refresh_task_snapshot(project.id, identity)
        self.store.audit(
            f"role:{identity.role_id}", "agent_task_created", task.id,
            f"project={project.id}")
        url = task_resource_url(project.id, task.id)
        return {
            "summary": f"已创建任务 [{task.title}]({url})",
            "task": task.to_dict(), "resource_url": url,
        }

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
                changes=changes, actor=f"role:{identity.role_id}",
            )
        except ValueError as exc:
            if "重新读取" in str(exc):
                raise AgentToolError("version_conflict", str(exc), 409) from exc
            if "已归档" in str(exc):
                raise AgentToolError("task_archived", str(exc), 409) from exc
            raise AgentToolError("invalid_arguments", str(exc), 400) from exc
        self._refresh_task_snapshot(project.id, identity)
        self.store.audit(
            f"role:{identity.role_id}", "agent_task_updated", task.id,
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
            self.store, project, task, actor=f"role:{identity.role_id}")
        self._refresh_task_snapshot(project.id, identity)
        return {
            "summary": f"已将任务 {task.title} 移入项目回收站",
            "deleted": True,
            "recycle_item": item,
            "resource_url": recycle_bin_url(project.id),
        }

    def _refresh_task_snapshot(self, project_id: str,
                               identity: AgentIdentity) -> None:
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
        actor = f"role:{identity.role_id}"
        try:
            revision = library_for(project.id).write_bytes(
                path, payload, actor=actor,
                message=str(arguments.get("message") or f"Publish {path}"),
                overwrite=overwrite,
            )
        except FileExistsError as exc:
            raise AgentToolError("already_exists", str(exc), 409) from exc
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

    def _delete_document(self, project: Project, identity: AgentIdentity,
                         arguments: dict, _context: AgentRunContext) -> dict:
        path = safe_relative_path(str(arguments.get("path", "")))
        actor = f"role:{identity.role_id}"
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
            if role_id == identity.role_id:
                raise AgentToolError("invalid_arguments", "主控不能调度自己")
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
            message_id = self._post_message(
                channel.id, identity.role_id, publish_content, author_type="agent",
                root_id=context.root_id, depth=context.depth + 1,
                mention_spans=mention_spans,
                origin_run_id=context.run_id or None,
            )
        except DispatchInactiveError as exc:
            raise AgentToolError("run_inactive", str(exc), 409) from exc
        self.store.audit(
            f"role:{identity.role_id}", "agent_message_published",
            detail=(f"project={project.id} channel={channel.id} message={message_id} "
                    f"run={context.run_id}"),
        )
        url = channel_resource_url(project.id, channel.id)
        result = {
            "summary": f"已在 [#{channel.name}]({url}) 发布消息",
            "message_id": message_id, "resource_url": url,
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
                    "完成或失败后自动启动新的主控 turn"
                )
            if dropped:
                result["not_dispatched"] = dropped
                result["summary"] += (
                    "；注意:" + "、".join(f"@{r}" for r in dropped)
                    + " 未启动(协作链执行数已达上限)")
        return result

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
            f"role:{identity.role_id}", "channel_created",
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
        self.store.put_board(board)
        self.store.audit(
            f"role:{identity.role_id}", "dashboard_saved",
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
            self.store, project, board_id, actor=f"role:{identity.role_id}")
        return {"summary": f"已将面板 {board.name} 移入项目回收站",
                "deleted": True, "recycle_item": item}

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
                actor=f"role:{identity.role_id}", original_name=original_name)
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
                self.store, project, name, actor=f"role:{identity.role_id}")
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
        actor = f"role:{identity.role_id}"
        if markdown is not None:
            if not isinstance(markdown, str):
                raise AgentToolError("invalid_arguments", "markdown 必须是字符串")
            saved = save_project_skill_markdown(
                self.store, project, raw_id, markdown, enabled=enabled, actor=actor)
        else:
            skill = ProjectSkill(
                id=raw_id, name=str(arguments.get("name", "")),
                description=str(arguments.get("description", "")),
                instructions=str(arguments.get("instructions", "")), enabled=enabled)
            saved = save_project_skill(self.store, project, skill, actor=actor)
        url = skill_resource_url(project.id, saved.id)
        return {
            "summary": f"已保存 Skill [{saved.name or raw_id}]({url})",
            "skill": saved.__dict__, "resource_url": url,
        }

    def _delete_skill(self, project: Project, identity: AgentIdentity,
                      arguments: dict, _context: AgentRunContext) -> dict:
        skill_id = self._control_id(arguments.get("id"))
        try:
            item = recycle_skill(
                self.store, project, skill_id,
                actor=f"role:{identity.role_id}")
        except FileNotFoundError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        return {
            "summary": f"已将 Skill {skill_id} 移入项目回收站",
            "deleted": True,
            "recycle_item": item,
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
                self.store, project, item_id, actor=f"role:{identity.role_id}")
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
                self.store, project, item_id, actor=f"role:{identity.role_id}")
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
            f"role:{identity.role_id}", "agent_tool_called",
            detail=(f"project={identity.project_id} channel={identity.channel_id} "
                    f"role={identity.role_id} token={identity.token_id} run={run_id} "
                    f"request={request_id} action={action} status={status} code={code}"),
        )


def default_agent_tool_url() -> str:
    return os.environ.get(
        "MISSIONCREW_AGENT_TOOL_URL", "http://127.0.0.1:8321/api/agent/v1")
