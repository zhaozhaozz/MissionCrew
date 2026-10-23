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
from . import channels as channel_ops
from .content_channels import content_channel, rebind_content_channel
from .documents import (document_resource_url, library_for,
                        normalize_document_resource_urls, safe_relative_path)
from .guidelines import (guideline_history, read_guideline_version, save_guideline,
                         sync_guideline_library)
from .recycle_bin import (RecycleConflictError, list_recycle_items,
                          purge_recycle_item, recycle_bin_url,
                          recycle_dashboard, recycle_document,
                          recycle_guideline, recycle_skill, recycle_task,
                          restore_recycle_item)
from .resource_urls import (automation_resource_url, channel_resource_url,
                            dashboard_resource_url, guideline_resource_url,
                            skill_resource_url, task_resource_url)
from .skill_versions import skill_version_library
from .skills import (read_skill_version, save_project_skill, save_project_skill_markdown,
                     skill_history, skill_package, sync_project_skill_library)
from .webhooks import webhook_path
from .workspace import chat_workspace_dir, write_task_files
from ..core.models import (BOARD_KINDS, BOARD_WIDGET_TYPES, Board,
                           BoardDataSource, BoardWidget,
                           Channel, Project, ProjectSkill,
                           Task)
from ..core.store import Store


CONTROL_ID_RE = re.compile(r"[\w-]+")
REQUEST_ID_RE = re.compile(r"[\w-]{1,100}")
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
# automation.list 带 id 时每条运行记录的 stdout/stderr 只回传末尾这么多字符
AUTOMATION_OUTPUT_TAIL = 4000
# *.history 读取单个历史版本的上限;更大的文件请人类在 Web 端下载
MAX_HISTORY_READ_BYTES = 5 * 1024 * 1024
TOKEN_LIFETIME_SECONDS = 7 * 24 * 60 * 60
TOKEN_RENEWAL_WINDOW_SECONDS = 24 * 60 * 60
LOGGER = logging.getLogger(__name__)


class AgentToolError(Exception):
    """可安全、结构化地返回给 Runtime 的动作错误。"""

    def __init__(self, code: str, message: str, status_code: int = 400,
                 *, retryable: bool = False, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details   # 帮调用方当场恢复的结构化信息,如冲突时的当前版本

    def to_dict(self) -> dict:
        error = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            error["details"] = self.details
        return error


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


# 动作说明只给 Agent 看(actions 命令与 /api/agent/v1/actions),与 Prompt 一样用英文;
# 状态示例里的 待处理/处理中 等是项目真实的标签取值,保持原样
ACTION_DEFINITIONS = {
    "task.create": {
        "description": "Create an issue-style Task that can be bound to several channels",
        "orchestrator_only": False,
        "arguments": {
            "title": "task title", "summary": "one-line summary", "body": "body text",
            "labels": ("label array; plain `text` or advanced `property: value` labels, "
                       "e.g. owner: 张三"),
            "channel_ids": ("optional array of channel ids to bind; omitted binds the current "
                            "channel"),
            "status": ("status text such as 待处理/处理中/已阻塞/已完成; defaults to the data "
                       "source's first status value (待处理 for the built-in source)"),
            "source": ("optional data source id: the built-in `built-in` (default) or a custom "
                       "source's short id; the task then shows on boards bound to that source and "
                       "uses its status values"),
            "url": "optional link to the external item (e.g. the issue page)",
            "meta": "optional array of short display notes shown on the board card",
            "external_id": ("optional id of the external item (e.g. the issue number), unique "
                            "within the source; already taken returns already_exists with the "
                            "existing task in error.details. In a custom source it is the key "
                            "of the whole sync (board_source.save cards): card `id` equal to "
                            "external_id updates this task instead of creating another, and a "
                            "later card list without it deletes the task. Tasks without "
                            "external_id are never touched by the whole sync"),
            "external_updated_at": ("optional update time of the item in the external "
                                    "system: Unix seconds or ISO 8601 with a time zone "
                                    "(e.g. 2026-10-19T08:00:00Z); the board shows it as the "
                                    "task's update time and sorts by it. Distinct from "
                                    "updated_at, which MissionCrew sets itself on every "
                                    "write and cannot be set"),
        },
    },
    "task.update": {
        "description": ("Update a task's requirement fields with optimistic concurrency; the "
                        "result's task carries the new updated_at (MissionCrew's own system "
                        "update time, the version) for the next update"),
        "orchestrator_only": False,
        "arguments": {
            "id": "task id",
            "snapshot_updated_at": ("the task's updated_at as last read (tasks/ snapshot or a "
                                    "previous result); on version_conflict, error.details.task "
                                    "is the current task"),
            "title/summary/body/status/labels/channel_ids/url/meta/external_id/"
            "external_updated_at": (
                "fields to update; labels replaces the whole label list; channel_ids [] "
                "unbinds all; external_id \"\" removes the sync key; external_updated_at "
                "null clears it (same rules as task.create)"),
            "properties": ("set individual `property: value` labels without touching the rest, "
                           "e.g. {\"owner\": \"张三\", \"priority\": null}; a value may be a "
                           "string or an array, null clears the property; status wins if "
                           "also given"),
        },
    },
    "task.brief": {
        "description": ("Append a status brief to a Task, optionally updating its status; "
                        "needs no snapshot_updated_at"),
        "orchestrator_only": False,
        "arguments": {
            "id": "task id", "content": "status brief text",
            "status": "optional status text such as 待处理/处理中/已阻塞/已完成",
        },
    },
    "task.delete": {
        "description": "Move a Task and its status briefs to the project recycle bin",
        "orchestrator_only": True,
        "arguments": {"id": "task id"},
    },
    "task.archive": {
        "description": ("Archive a Task: it leaves the task boards and the tasks/ snapshot but "
                        "keeps its body and briefs; task.restore brings it back"),
        "orchestrator_only": True,
        "arguments": {"id": "task id"},
    },
    "task.restore": {
        "description": ("Restore an archived Task to the task boards and the tasks/ snapshot "
                        "(archived tasks are not in the snapshot; take the id from the human or "
                        "an earlier result)"),
        "orchestrator_only": True,
        "arguments": {"id": "task id"},
    },
    "document.publish": {
        "description": "Publish a text or binary file to the project's versioned document library",
        "orchestrator_only": False,
        "arguments": {
            "path": "path in the document library",
            "content or content_base64": "file content",
            "overwrite": "whether to explicitly overwrite an existing target; default false",
            "message": "optional version message",
        },
    },
    "document.rename": {
        "description": "Move or rename a versioned project document, keeping its Git history",
        "orchestrator_only": False,
        "arguments": {
            "source": "existing path in the document library",
            "target": "new path in the document library; the target must not exist",
        },
    },
    "document.delete": {
        "description": ("Move a versioned project document to the unified recycle bin, "
                        "keeping its Git history"),
        "orchestrator_only": True,
        "arguments": {"path": "path in the document library"},
    },
    "document.history": {
        "description": ("Read a document's version history (the history is not in the "
                        "workspace): without revision, list its versions newest first (also for "
                        "renamed or deleted documents); with revision, return that version's "
                        "content (content, or content_base64 for non-UTF-8 files)"),
        "orchestrator_only": False,
        "arguments": {
            "path": "path in the document library",
            "revision": "optional revision from the version list",
            "limit": "without revision: how many versions to list; default 20, at most 100",
        },
    },
    "message.publish": {
        "description": (
            "Post a message to a project channel, only for dispatching or posting to other "
            "channels; your final reply is already published to the trigger channel by the "
            "platform, so do not use this to copy the answer. mentions is the only channel for "
            "dispatching roles; end the current turn when the returned dispatched (roles actually "
            "started) is non-empty; chain_budget gives the runs used and the limit of this "
            "collaboration chain"
        ),
        # 执行角色也能发消息,但 mentions 只能是主控(capabilities 换成受限说明)
        "orchestrator_only": False,
        "arguments": {
            "channel": "short channel id within the project",
            "content": "message body; any @ in it never dispatches",
            "mentions": ("array of role ids to dispatch explicitly; omit it to post without "
                         "dispatching"),
        },
    },
    "channel.runs.list": {
        "description": (
            "On demand, list queued/running/waiting_user role runs in a channel (the current "
            "channel by default; pass channel for another project channel); run status is not "
            "preloaded into the chat context"
        ),
        "orchestrator_only": True,
        "automation_allowed": False,
        "arguments": {"channel": "short channel id within the project; defaults to the current channel"},
    },
    "channel.run.stop": {
        "description": ("Stop the given active role run in the current channel; query it first "
                        "to get the run_id"),
        "orchestrator_only": True,
        "automation_allowed": False,
        "arguments": {"run_id": "positive integer run id returned by channel.runs.list"},
    },
    "channel.history": {
        "description": (
            "On demand, read a channel's recent messages (the current channel by default; pass "
            "channel for another project channel); records use the channel-history.json format, "
            "for checking dispatches, progress and reports; do not call it repeatedly as a wait loop"
        ),
        "orchestrator_only": True,
        "automation_allowed": False,
        "arguments": {
            "channel": "short channel id within the project; defaults to the current channel",
            "limit": "how many latest messages to return; default 20, at most 200",
            "before_id": "only return earlier messages with id below this value, for paging back",
        },
    },
    "channel.list": {
        "description": (
            "On demand, list project channels (id, name, purpose, workdir, archive state, last "
            "message time, active run count); the channel list is not preloaded into the chat "
            "context, so check it before creating a channel to avoid duplicates"
        ),
        "orchestrator_only": True,
        "arguments": {"scope": "active (default)/archived/all"},
    },
    "channel.create": {
        "description": "Create a project channel (check with channel.list first that the id is free)",
        "orchestrator_only": False,
        "arguments": {"id": "short channel id",
                      "name": "name; 'group/topic' files it under a collapsible sidebar group",
                      "purpose": "purpose",
                      "workdir": "optional project repository directory"},
    },
    "channel.update": {
        "description": ("Change a project channel's name, purpose or workdir; archived and "
                        "page-conversation channels cannot be changed, and a new workdir takes "
                        "effect from the next run in that channel"),
        "orchestrator_only": True,
        "arguments": {
            "id": "short channel id; defaults to the current channel",
            "name": "new name", "purpose": "new purpose",
            "workdir": ("project repository path or a subdirectory of one; an empty string "
                        "switches back to the platform's built-in workspace"),
        },
    },
    "channel.archive": {
        "description": ("Archive a project channel: it becomes read-only, drops out of "
                        "channel.list's default scope and its persistent runtimes are stopped; "
                        "refused for general, page-conversation channels and channels with "
                        "active runs (so never the current channel)"),
        "orchestrator_only": True,
        "arguments": {"id": "short channel id"},
    },
    "channel.restore": {
        "description": "Restore an archived project channel (find it with channel.list scope=archived)",
        "orchestrator_only": True,
        "arguments": {"id": "short channel id"},
    },
    "dashboard.save": {
        "description": "Create or update a project dashboard (widget grid or task board)",
        "orchestrator_only": True,
        "arguments": {
            "id": "short dashboard id", "name": "name", "description": "purpose",
            "layout": "optional widget array (widgets form)",
            "kind": "optional widgets/taskboard",
            "source": ("taskboard data source id (the built-in `built-in` or a custom source's "
                       "short id)"),
            "filters": ("taskboard filter column array [{title,query,color}]; query is a label "
                        "expression (& | ! and parentheses; `property: *` matches tasks with "
                        "that property, and a bare term like `owner` also matches any "
                        "`owner: ...` label); when creating, defaults to status columns generated "
                        "from the data source's status values"),
            "group_by": ("optional property name the board page groups columns by by default: "
                         "one column per value plus a trailing \"unset\" column, covering every "
                         "task in the board's scope (filters do not narrow it); viewers can "
                         "switch grouping and filters for themselves without changing it"),
        },
    },
    "dashboard.delete": {
        "description": "Move a project dashboard to the unified recycle bin",
        "orchestrator_only": True,
        "arguments": {"id": "short dashboard id"},
    },
    "dashboard.list": {
        "description": ("On demand, list project dashboards (dashboards are not in the "
                        "workspace); pass id to get one dashboard's full configuration (layout, "
                        "filters, group_by) before changing it with dashboard.save"),
        "orchestrator_only": True,
        "arguments": {"id": "optional short dashboard id"},
    },
    "board_source.save": {
        "description": (
            "Create or update a custom task data source; cards are synced by id as that "
            "source's tasks (add/update/delete); suited to automation scripts that "
            "periodically sync external lists such as GitCode/GitHub issues, then use "
            "dashboard.save to create a task board bound to the source. To add, change or "
            "remove single tasks of a source, use task.create with source (and external_id "
            "when the item exists externally) and task.update/task.delete instead of "
            "resending the whole card list"
        ),
        "orchestrator_only": True,
        "arguments": {
            "id": "short data source id (must not clash with built-in sources)",
            "name": "name", "description": "purpose",
            "columns": ("optional array of status values [{value,color}] (legacy "
                        "{key,title,color} accepted); defaults to 待处理/处理中/已阻塞/已完成 "
                        "on creation, omitted on update keeps the existing ones"),
            "cards": ("optional card array, synced as a whole by id against the source's "
                      "tasks' external_id: tasks with an external_id absent from the list are "
                      "deleted, tasks without one are left alone. Each card needs id (title too when it creates a task), "
                      "optionally summary, status (status text), labels, properties, "
                      "updated_at (the item's update time in the external system, stored as "
                      "the task's external_updated_at: Unix seconds or ISO 8601 with a time "
                      "zone), meta (array of strings) and url (external "
                      "link). Existing tasks only change "
                      "what a card carries: omit status to keep the local status (new tasks "
                      "default to the first status value); plain labels in labels replace the "
                      "task's plain labels as a set, `property: value` labels and properties "
                      "({property: value | [values] | null}) replace only those properties, "
                      "null clears. New tasks go through the project's automatic handling "
                      "rules. Removed tasks are deleted permanently with their briefs, so "
                      "never save a partial list"),
            "mode": "create/update/upsert; default upsert",
        },
    },
    "board_source.delete": {
        "description": ("Delete a custom board data source (refused while a dashboard still "
                        "references it)"),
        "orchestrator_only": True,
        "arguments": {"id": "short data source id"},
    },
    "board_source.list": {
        "description": ("On demand, list custom task data sources with their status values, "
                        "task counts and the dashboards bound to them; the tasks themselves are "
                        "in the tasks/ snapshot"),
        "orchestrator_only": True,
        "arguments": {},
    },
    "guideline.save": {
        "description": "Save a complete guideline Markdown file",
        "orchestrator_only": True,
        "arguments": {"markdown": "full text with name/description frontmatter",
                      "enabled": "whether it is enabled",
                      "original_name": "name before renaming"},
    },
    "guideline.delete": {
        "description": ("Move a guideline Markdown file to the unified recycle bin, keeping its "
                        "Git history"),
        "orchestrator_only": True,
        "arguments": {"name": "guideline name"},
    },
    "guideline.history": {
        "description": ("Read a guideline's version history: without revision, list its "
                        "versions newest first; with revision, return that version's full "
                        "markdown"),
        "orchestrator_only": True,
        "arguments": {
            "name": "guideline name",
            "revision": "optional revision from the version list",
            "limit": "without revision: how many versions to list; default 20, at most 100",
        },
    },
    "guideline.list": {
        "description": ("On demand, list all project guidelines including disabled ones (the "
                        "workspace guidelines/ view only has enabled ones); pass name to get the "
                        "full markdown, e.g. to edit or re-enable it with guideline.save"),
        "orchestrator_only": True,
        "arguments": {"name": "optional guideline name"},
    },
    "skill.save": {
        "description": "Save a complete skill Markdown file",
        "orchestrator_only": True,
        "arguments": {"id": "skill id", "markdown": "the complete SKILL.md",
                      "enabled": "whether it is enabled"},
    },
    "skill.delete": {
        "description": "Move a complete skill package to the unified recycle bin",
        "orchestrator_only": True,
        "arguments": {"id": "skill id"},
    },
    "skill.history": {
        "description": ("Read a skill package's version history: without revision, list its "
                        "versions newest first; with revision, return that version's SKILL.md "
                        "and file list, and with file also that file's content"),
        "orchestrator_only": True,
        "arguments": {
            "id": "skill id",
            "revision": "optional revision from the version list",
            "file": "with revision: optional path inside the package, e.g. scripts/run.sh",
            "limit": "without revision: how many versions to list; default 20, at most 100",
        },
    },
    "skill.list": {
        "description": ("On demand, list all project skills including disabled ones (the "
                        "workspace skills/ view only has enabled ones); pass id to get the full "
                        "SKILL.md and the package's file list, e.g. to edit or re-enable it with "
                        "skill.save"),
        "orchestrator_only": True,
        "arguments": {"id": "optional skill id"},
    },
    "automation.save": {
        "description": (
            "Create or update a project automation script; it is triggered by cron, by webhook "
            "deliveries on the platform's dedicated webhook port, or manually, and it calls "
            "platform actions with its own token"
        ),
        "orchestrator_only": True,
        "arguments": {
            "id": "short script id", "name": "name", "description": "purpose",
            "script": ("full script text (run as an executable when it has a shebang, "
                       "otherwise with bash)"),
            "cron": "five-field crontab; an empty string means no schedule",
            "enabled": "whether scheduled (cron) triggering is enabled",
            "webhook": ("true accepts webhook deliveries (can be combined with cron); the "
                        "result gives webhook_path, and the signing secret is shown only to "
                        "humans on the automation page, so ask a human to configure the "
                        "external system"),
            "actions": "allowlist array of actions it may call; omit it to keep the current value",
            "timeout_seconds": "timeout of a single run in seconds",
        },
    },
    "automation.delete": {
        "description": "Delete a project automation script and its run records",
        "orchestrator_only": True,
        "arguments": {"id": "short script id"},
    },
    "automation.list": {
        "description": ("On demand, list project automation scripts (schedule, enabled, actions "
                        "allowlist, last run); pass id to get the full script and its recent "
                        "runs with the tail of their output, e.g. to fix a failing script; "
                        "page back with before_id through all kept runs (run_count)"),
        "orchestrator_only": True,
        "arguments": {
            "id": "optional short script id",
            "runs": "with id: how many recent runs to return; default 5, at most 20",
            "before_id": ("with id: only return earlier runs with id below this value, "
                          "for paging back"),
        },
    },
    "automation.run": {
        "description": ("Trigger a manual run of an automation script now (also when scheduling "
                        "is disabled); it runs in the background, so check the result later "
                        "with automation.list and the id instead of waiting in a loop"),
        "orchestrator_only": True,
        "automation_allowed": False,
        "arguments": {"id": "short script id"},
    },
    "recycle.list": {
        "description": "List the current project's unified recycle bin",
        "orchestrator_only": True,
        "arguments": {},
    },
    "recycle.restore": {
        "description": "Restore a resource from the project recycle bin",
        "orchestrator_only": True,
        "arguments": {"id": "recycle bin entry id"},
    },
    "recycle.purge": {
        "description": "Permanently delete a project recycle bin entry",
        "orchestrator_only": True,
        "arguments": {"id": "recycle bin entry id"},
    },
}

AUTOMATION_ACTIONS = frozenset(
    name for name, definition in ACTION_DEFINITIONS.items()
    if definition.get("automation_allowed", True))

def _message_publish_without_mentions(reason: str) -> dict:
    """不能提及任何角色时列出的 message.publish 说明:不给出 mentions 参数
    (服务端在 _publish_message 里同样拒绝)。"""
    return {
        "description": (
            "Post a message to a project channel, only for posting to other channels (such as a "
            f"brief in a new channel); {reason}; your final reply is already published to the "
            "trigger channel by the platform, so do not use this to copy the answer"
        ),
        "arguments": {
            "channel": "short channel id within the project",
            "content": "message body; any @ in it is plain text and never starts a role",
        },
    }


# 无主控项目里角色能发消息但不能派发
PEER_MESSAGE_PUBLISH = _message_publish_without_mentions(
    "this project has no orchestrator, so roles cannot dispatch each other and only humans "
    "start roles")
# 仅人工点名的角色不进 Agent 协作图,有主控时也不能提及主控
MANUAL_ONLY_MESSAGE_PUBLISH = _message_publish_without_mentions(
    "you are a human-mention-only role, so you cannot start other roles")


def _executor_message_publish(orchestrator_id: str) -> dict:
    """有主控项目的执行角色:mentions 只能是主控,主控的回复不回到发起方。"""
    return {
        "description": (
            "Post a message to a project channel, for posting to other channels (such as a "
            "brief in a channel you just created) or bringing in the orchestrator; your final "
            "reply is already published to the trigger channel by the platform (and handed back "
            "to the orchestrator when it dispatched you), so do not use this to copy the answer. "
            f"mentions may only name the orchestrator @{orchestrator_id}, which then handles "
            "the message in that channel; its replies stay there and are not returned to you"
        ),
        "arguments": {
            "channel": "short channel id within the project",
            "content": "message body; any @ in it never starts a role",
            "mentions": (f"optional array whose only allowed id is \"{orchestrator_id}\"; omit "
                         "it to post without starting anyone"),
        },
    }


# 有主控项目的执行角色没有 channel.list,去掉"先查重"的指引
EXECUTOR_CHANNEL_CREATE = {
    "description": ("Create a project channel; an id that is already in use returns "
                    "already_exists"),
    "arguments": ACTION_DEFINITIONS["channel.create"]["arguments"],
}

ACTION_ARGUMENTS = {
    "task.create": {
        "title", "summary", "body", "labels", "channel_ids", "status", "source",
        "url", "meta", "external_id", "external_updated_at",
    },
    "task.update": {
        "id", "snapshot_updated_at", "title", "summary", "body", "status",
        "labels", "properties", "channel_ids", "url", "meta", "external_id",
        "external_updated_at",
    },
    "task.brief": {"id", "content", "status"},
    "task.delete": {"id"},
    "task.archive": {"id"},
    "task.restore": {"id"},
    "document.publish": {
        "path", "content", "content_base64", "overwrite", "message",
    },
    "document.rename": {"source", "target"},
    "document.delete": {"path"},
    "document.history": {"path", "revision", "limit"},
    "message.publish": {"channel", "content", "mentions"},
    "channel.runs.list": {"channel"},
    "channel.run.stop": {"run_id"},
    "channel.history": {"channel", "limit", "before_id"},
    "channel.list": {"scope"},
    "channel.create": {"id", "name", "purpose", "workdir"},
    "channel.update": {"id", "name", "purpose", "workdir"},
    "channel.archive": {"id"},
    "channel.restore": {"id"},
    "dashboard.save": {"id", "name", "description", "layout", "mode",
                       "kind", "source", "filters", "group_by"},
    "dashboard.delete": {"id"},
    "dashboard.list": {"id"},
    "board_source.save": {"id", "name", "description", "columns", "cards",
                          "mode"},
    "board_source.delete": {"id"},
    "board_source.list": set(),
    "guideline.save": {"markdown", "enabled", "original_name"},
    "guideline.delete": {"name"},
    "guideline.list": {"name"},
    "guideline.history": {"name", "revision", "limit"},
    "skill.save": {
        "id", "markdown", "enabled", "name", "description", "instructions",
    },
    "skill.delete": {"id"},
    "skill.list": {"id"},
    "skill.history": {"id", "revision", "file", "limit"},
    "automation.save": {
        "id", "name", "description", "script", "cron", "enabled", "webhook", "actions",
        "timeout_seconds",
    },
    "automation.delete": {"id"},
    "automation.list": {"id", "runs", "before_id"},
    "automation.run": {"id"},
    "recycle.list": set(),
    "recycle.restore": {"id"},
    "recycle.purge": {"id"},
}

# 成功的写操作要在发起调用的会话中留下可见回执。停止动作本身已经生成聊天
# 消息;查询动作是只读操作,这些动作都不额外插入回执。message.publish 单独
# 处理:发到当前频道时消息本身就是痕迹,发到其他频道时才在源频道留回执。
CONVERSATION_RECEIPT_ACTIONS = (
    frozenset(ACTION_DEFINITIONS) - {
        "message.publish", "channel.runs.list", "channel.run.stop",
        "channel.history", "channel.list", "recycle.list", "dashboard.list",
        "board_source.list", "guideline.list", "skill.list", "automation.list",
        "document.history", "guideline.history", "skill.history",
    }
)


class AgentActionService:
    """统一执行 Agent 可请求的 MissionCrew 平台动作。"""

    def __init__(self, store: Store, post_message: Callable[..., int],
                 stop_run: Callable[..., dict],
                 history_records: Optional[Callable[..., list[dict]]] = None,
                 stop_channel_sessions: Optional[Callable[[str], int]] = None):
        self.store = store
        self._post_message = post_message
        self._stop_run = stop_run
        # (channel, role_id, limit, before_id) -> 与 channel-history.json 同构的
        # 消息记录;由聊天引擎提供,保证脱敏口径与工作区历史文件一致
        self._history_records = history_records
        # 归档频道前停掉其持久 Runtime;None = 没有聊天引擎(测试),无长驻实例
        self._stop_channel_sessions = stop_channel_sessions or (lambda _channel_id: 0)
        # 自动化调度器在服务装配时绑定(它自身依赖本服务签发脚本令牌)
        self._trigger_automation: Optional[Callable[..., int]] = None

    def bind_automation_trigger(self, trigger: Callable[..., int]) -> None:
        self._trigger_automation = trigger

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def allowed_actions(self, project: Project, role_id: str) -> list[str]:
        # 无主控项目里每个角色都拥有主控级动作;派发另由 can_dispatch 把关
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
        actions = {
            name: {key: value for key, value in ACTION_DEFINITIONS[name].items()
                   if key not in {"orchestrator_only", "automation_allowed"}}
            for name in allowed
        }
        if not identity.is_automation and not project.can_dispatch(identity.role_id):
            if "message.publish" in actions:
                actions["message.publish"] = self._restricted_message_publish(
                    project, identity.role_id)
            if "channel.create" in actions and not project.controls_platform(identity.role_id):
                actions["channel.create"] = EXECUTOR_CHANNEL_CREATE
        return {
            "project_id": identity.project_id,
            "channel_id": identity.channel_id,
            "role_id": identity.role_id,
            "token_id": identity.token_id,
            "actions": actions,
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
        receipt_context: dict = {
            "action": action,
            "role_id": identity.role_id,
            "run_id": context.run_id,
        }
        if action == "message.publish":
            # 跨频道派发的消息只存在于目标频道;主控被停止后再唤起时,源频道
            # 历史里若没有这条回执就会误判"尚未派发"而重复派工。
            target = str(result.get("channel_id") or "")
            if not context.channel_id or target == context.channel_id:
                return
            receipt_context["channel"] = target
            receipt_context["message_id"] = int(result.get("message_id") or 0)
        elif action not in CONVERSATION_RECEIPT_ACTIONS:
            return
        summary = str(result.get("summary") or f"已完成 {action}")
        content = (
            f"@{identity.role_id} 使用 MissionCrew Tool · `{action}`：{summary}"
        )
        try:
            self._post_message(
                context.channel_id, "platform", content, author_type="platform",
                root_id=context.root_id, depth=context.depth + 1,
                mention_spans=[], context={"agent_tool": receipt_context},
                kind="agent_tool",
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
            "task.archive": self._archive_task,
            "task.restore": self._restore_task,
            "document.publish": self._publish_document,
            "document.rename": self._rename_document,
            "document.delete": self._delete_document,
            "document.history": self._document_history,
            "message.publish": self._publish_message,
            "channel.runs.list": self._list_channel_runs,
            "channel.run.stop": self._stop_channel_run,
            "channel.history": self._channel_history,
            "channel.list": self._list_channels,
            "channel.create": self._create_channel,
            "channel.update": self._update_channel,
            "channel.archive": self._archive_channel,
            "channel.restore": self._restore_channel,
            "dashboard.save": self._save_dashboard,
            "dashboard.delete": self._delete_dashboard,
            "dashboard.list": self._list_dashboards,
            "board_source.save": self._save_board_source,
            "board_source.delete": self._delete_board_source,
            "board_source.list": self._list_board_sources,
            "guideline.save": self._save_guideline,
            "guideline.delete": self._delete_guideline,
            "guideline.list": self._list_guidelines,
            "guideline.history": self._guideline_history,
            "skill.save": self._save_skill,
            "skill.delete": self._delete_skill,
            "skill.list": self._list_skills,
            "skill.history": self._skill_history,
            "automation.save": self._save_automation,
            "automation.delete": self._delete_automation,
            "automation.list": self._list_automations,
            "automation.run": self._run_automation,
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

    def _project_channel(self, project: Project, raw_channel: str,
                         fallback: str = "") -> Channel:
        """按短 id 或全 id 找项目内频道;raw 为空时回退到 fallback(当前频道)。"""
        raw_channel = str(raw_channel or "").strip() or fallback
        if not raw_channel:
            raise AgentToolError("invalid_arguments", "需要提供 channel")
        channel_id = (raw_channel if raw_channel.startswith(f"{project.id}:")
                      else f"{project.id}:{raw_channel}")
        channel = self.store.get_channel(channel_id) or self.store.get_channel(raw_channel)
        if channel is None or channel.project_id != project.id:
            raise AgentToolError(
                "channel_not_found", f"频道不存在或不属于本项目: {raw_channel}", 404)
        return channel

    def _list_channel_runs(self, project: Project, identity: AgentIdentity,
                           arguments: dict, context: AgentRunContext) -> dict:
        """只在显式调用时返回频道的活动 Run 快照;默认当前频道,可指定项目内
        其他频道以核对派发是否已开工。停止仍只对当前频道的运行开放。"""
        self._require_chat_identity(identity)
        channel = self._project_channel(
            project, arguments.get("channel", ""), identity.channel_id)
        is_current_channel = channel.id == identity.channel_id
        runs = []
        for row in self.store.active_chat_runs(channel.id):
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
                "stoppable": is_current_channel and run_id != context.run_id,
            })
        where = "当前 Channel" if is_current_channel else f"频道 #{channel.name}"
        return {
            "summary": f"{where}有 {len(runs)} 个活动角色运行",
            "channel_id": channel.id,
            "is_current_channel": is_current_channel,
            "runs": runs,
        }

    def _channel_history(self, project: Project, identity: AgentIdentity,
                         arguments: dict, _context: AgentRunContext) -> dict:
        """按需读取频道最近消息;默认当前频道,可指定项目内其他频道。"""
        self._require_chat_identity(identity)
        channel = self._project_channel(
            project, arguments.get("channel", ""), identity.channel_id)
        limit = arguments.get("limit", 20)
        before_id = arguments.get("before_id", 0)
        for name, value in (("limit", limit), ("before_id", before_id)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AgentToolError("invalid_arguments", f"{name} 必须是非负整数")
        limit = min(limit or 20, 200)
        if self._history_records is None:
            raise AgentToolError("unsupported_action", "当前部署不支持读取频道历史", 501)
        records = self._history_records(channel, identity.role_id, limit, before_id)
        url = channel_resource_url(project.id, channel.id)
        return {
            "summary": (f"频道 [#{channel.name}]({url}) 最近 {len(records)} 条消息"
                        + (f"(id < {before_id})" if before_id else "")),
            "channel_id": channel.id,
            "channel_name": channel.name,
            "is_current_channel": channel.id == identity.channel_id,
            "message_count": self.store.count_messages(channel.id),
            "resource_url": url,
            "messages": records,
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
        from .tasks import TaskConflictError, auto_process_task, create_task

        try:
            task = create_task(
                self.store, project.id, **arguments,
                actor=identity.actor,
                fallback_channel_id=identity.channel_id,
            )
        except TaskConflictError as exc:
            raise self._task_conflict(exc) from exc
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
        from .tasks import TaskConflictError, update_task

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
        except TaskConflictError as exc:
            raise self._task_conflict(exc) from exc
        except ValueError as exc:
            if "重新读取" in str(exc):
                # 刷新快照并带回当前版本,调用方核对后用新的 updated_at 当场重试
                self._refresh_task_snapshot(project.id, identity)
                raise AgentToolError(
                    "version_conflict",
                    f"{exc}；tasks/ 快照已刷新，error.details.task 是当前版本，"
                    "核对后用其中的 updated_at 作为 snapshot_updated_at 重试",
                    409, details={"task": task.to_dict()}) from exc
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

    @staticmethod
    def _task_conflict(exc) -> "AgentToolError":
        # 带回已占用该 external_id 的任务,调用方改用 task.update 更新它而不是再建一张
        return AgentToolError(
            "already_exists", f"{exc}；error.details.task 是已有任务，请改用 task.update",
            409, details={"task": exc.existing.to_dict()})

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
        task = self._project_task(project, arguments)
        item = recycle_task(
            self.store, project, task, actor=identity.actor)
        self._refresh_task_snapshot(project.id, identity)
        return {
            "summary": f"已将任务 {task.title} 移入项目回收站",
            "deleted": True,
            "recycle_item": item,
            "resource_url": recycle_bin_url(project.id),
        }


    def _project_task(self, project: Project, arguments: dict) -> Task:
        task_id = str(arguments.get("id", "")).strip()
        task = self.store.get_task(task_id)
        if task is None or task.project_id != project.id:
            raise AgentToolError("task_not_found", f"任务不存在: {task_id}", 404)
        return task

    def _archive_task(self, project: Project, identity: AgentIdentity,
                      arguments: dict, _context: AgentRunContext) -> dict:
        from .tasks import archive_task

        task = self._project_task(project, arguments)
        was_archived = task.archived
        archive_task(self.store, task, actor=identity.actor)
        self._refresh_task_snapshot(project.id, identity)
        url = task_resource_url(project.id, task.id)
        return {
            "summary": (f"任务 [{task.title}]({url}) 已是归档状态" if was_archived
                        else f"已归档任务 [{task.title}]({url})"),
            "task": task.to_dict(), "resource_url": url,
        }

    def _restore_task(self, project: Project, identity: AgentIdentity,
                      arguments: dict, _context: AgentRunContext) -> dict:
        from .tasks import restore_task

        task = self._project_task(project, arguments)
        was_archived = task.archived
        restore_task(self.store, task, actor=identity.actor)
        self._refresh_task_snapshot(project.id, identity)
        url = task_resource_url(project.id, task.id)
        return {
            "summary": (f"已恢复归档任务 [{task.title}]({url})" if was_archived
                        else f"任务 [{task.title}]({url}) 未归档，无需恢复"),
            "task": task.to_dict(), "resource_url": url,
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

    def _is_manual_only(self, project: Project, role_id: str) -> bool:
        role = self.store.get_role(project.id, role_id)
        return bool(role and role.manual_only)

    def _restricted_message_publish(self, project: Project, role_id: str) -> dict:
        """不能任意派发的角色看到的 message.publish 说明,与 _ensure_can_mention 同口径。"""
        if not project.has_orchestrator:
            return PEER_MESSAGE_PUBLISH
        if self._is_manual_only(project, role_id):
            return MANUAL_ONLY_MESSAGE_PUBLISH
        return _executor_message_publish(project.orchestrator_role_id)

    def _ensure_can_mention(self, project: Project, role_id: str,
                            mentions: list[str]) -> None:
        """主控可提及任意角色;有主控时执行角色只能提及主控;无主控时、以及仅人工
        点名的角色不能启动任何角色。"""
        if project.can_dispatch(role_id):
            return
        if not project.has_orchestrator:
            raise AgentToolError(
                "permission_denied",
                "本项目没有主控,角色之间不能派发;需要其他角色时请在最终回复里说明,"
                "由人类点名", 403)
        if self._is_manual_only(project, role_id):
            raise AgentToolError(
                "permission_denied",
                "仅人工点名的角色不能启动其他角色;需要协作时请在最终回复里说明,"
                "由人类点名", 403)
        denied = [target for target in mentions
                  if not project.can_mention(role_id, target)]
        if denied:
            raise AgentToolError(
                "permission_denied",
                f"mentions 只能是主控 @{project.orchestrator_role_id};需要 "
                + "、".join(f"@{target}" for target in dict.fromkeys(denied))
                + " 参与时请在消息里写明,由主控派发", 403)

    def _publish_message(self, project: Project, identity: AgentIdentity,
                         arguments: dict, context: AgentRunContext) -> dict:
        raw_channel = str(arguments.get("channel", "")).strip()
        content = arguments.get("content", "")
        if not raw_channel or not isinstance(content, str) or not content.strip():
            raise AgentToolError(
                "invalid_arguments", "message.publish 需要 channel 和非空 content")
        channel = self._project_channel(project, raw_channel)
        mentions = arguments.get("mentions", [])
        if (not isinstance(mentions, list)
                or not all(isinstance(item, str) for item in mentions)):
            raise AgentToolError("invalid_arguments", "mentions 必须是角色 id 数组")
        if mentions and not identity.is_automation:
            self._ensure_can_mention(project, identity.role_id, mentions)
        unique_mentions: list[str] = []
        for role_id in mentions:
            if not identity.is_automation and role_id == identity.role_id:
                raise AgentToolError("invalid_arguments", "不能调度自己")
            target_role = self.store.get_role(project.id, role_id)
            # 仅人工点名的角色对 Agent 不存在;自动化脚本是人类配置的,按人类规则放行
            if (target_role is None or not target_role.enabled
                    or (target_role.manual_only and not identity.is_automation)):
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
            "message_id": message_id, "channel_id": channel.id,
            "resource_url": url,
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
                result["summary"] += (
                    "，已派发 " + "、".join(f"@{r}" for r in result["dispatched"]))
                # resume 是给调用方 Agent 的提示,与 Prompt 一样用英文;summary 会展示给人类
                if identity.is_automation or project.can_dispatch(identity.role_id):
                    result["handoff"] = "end_turn"
                    result["resume"] = (
                        "Do not ask still-running roles for interim status; MissionCrew "
                        "automatically starts a new turn for you with the results once the "
                        "dispatched roles finish or fail"
                    )
                else:
                    # 执行角色只能提及主控,主控的回复不会交回发起方
                    result["resume"] = (
                        "The orchestrator handles this message in that channel and its replies "
                        "are not returned to you; do not wait for it, finish your own task"
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


    def _update_channel(self, project: Project, identity: AgentIdentity,
                        arguments: dict, _context: AgentRunContext) -> dict:
        if not str(arguments.get("id") or "").strip() and not identity.channel_id:
            raise AgentToolError("invalid_arguments", "需要提供频道 id")
        channel = self._project_channel(
            project, arguments.get("id", ""), identity.channel_id)
        fields: dict = {}
        for key in ("name", "purpose"):
            if key in arguments:
                fields[key] = str(arguments.get(key) or "")
        if "workdir" in arguments:
            requested = str(arguments.get("workdir") or "").strip()
            # 空值 = 改回平台内置工作区;非空必须落在项目代码仓内(与 channel.create 同口径)
            fields["workdir"] = (self._resolve_channel_workdir(project, requested)
                                 if requested else None)
        if not fields:
            raise AgentToolError(
                "invalid_arguments", "channel.update 至少需要 name、purpose、workdir 之一")
        try:
            changed = channel_ops.update_channel(
                self.store, channel, actor=identity.actor, **fields)
        except channel_ops.ChannelChangeError as exc:
            raise AgentToolError(exc.code, str(exc), 409) from exc
        url = channel_resource_url(project.id, channel.id)
        labels = {"name": "名称", "purpose": "用途", "workdir": "工作目录"}
        if changed:
            summary = (f"已更新频道 [#{channel.name}]({url})的"
                       + "、".join(labels[key] for key in changed))
            if "workdir" in changed:
                summary += f"(新工作目录 {channel.workdir or '平台内置工作区'}，从下一轮执行起生效)"
        else:
            summary = f"频道 [#{channel.name}]({url}) 没有变化"
        return {"summary": summary, "changed": changed,
                "channel": channel.to_dict(), "resource_url": url}

    def _archive_channel(self, project: Project, identity: AgentIdentity,
                         arguments: dict, _context: AgentRunContext) -> dict:
        channel = self._project_channel(project, arguments.get("id", ""))
        if channel.content_kind:
            raise AgentToolError(
                "content_channel", "页面对话频道随内容页存在，不能经 Agent Tool 归档", 409)
        try:
            stopped = channel_ops.archive_channel(
                self.store, channel, actor=identity.actor,
                stop_sessions=self._stop_channel_sessions)
        except channel_ops.ChannelChangeError as exc:
            raise AgentToolError(exc.code, str(exc), 409) from exc
        url = channel_resource_url(project.id, channel.id)
        return {
            "summary": f"已归档频道 [#{channel.name}]({url})",
            "channel": channel.to_dict(), "stopped_runtimes": stopped,
            "resource_url": url,
        }

    def _restore_channel(self, project: Project, identity: AgentIdentity,
                         arguments: dict, _context: AgentRunContext) -> dict:
        channel = self._project_channel(project, arguments.get("id", ""))
        restored = channel_ops.restore_channel(
            self.store, channel, actor=identity.actor)
        url = channel_resource_url(project.id, channel.id)
        return {
            "summary": (f"已恢复归档频道 [#{channel.name}]({url})" if restored
                        else f"频道 [#{channel.name}]({url}) 未归档，无需恢复"),
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
            try:
                board.group_by = board_sources.validate_group_by(
                    arguments.get("group_by"))
            except ValueError as exc:
                raise AgentToolError("invalid_arguments", str(exc))
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


    def _list_dashboards(self, project: Project, _identity: AgentIdentity,
                         arguments: dict, _context: AgentRunContext) -> dict:
        """不带 id 列出摘要;带 id 返回 dashboard.save 可直接改写的完整配置。"""
        def short_id(board: Board) -> str:
            return board.id.removeprefix(f"{project.id}:")

        if str(arguments.get("id") or "").strip():
            raw_id = self._control_id(arguments.get("id"))
            board = self.store.get_board(f"{project.id}:{raw_id}")
            if board is None or board.project_id != project.id:
                raise AgentToolError("not_found", "面板不存在", 404)
            url = dashboard_resource_url(project.id, board.id)
            return {"summary": f"面板 [{board.name}]({url}) 的完整配置",
                    "dashboard": {**board.to_dict(), "id": short_id(board),
                                  "resource_url": url}}
        rows = []
        for board in self.store.list_boards(project.id):
            row = {"id": short_id(board), "name": board.name,
                   "description": board.description, "kind": board.kind,
                   "updated_at": board.updated_at,
                   "resource_url": dashboard_resource_url(project.id, board.id)}
            if board.kind == "taskboard":
                row.update(source=board.source, group_by=board.group_by,
                           filters=[f.get("title", "") for f in board.filters])
            else:
                row["widgets"] = [{"id": w.id, "type": w.type, "title": w.title}
                                  for w in board.layout]
            rows.append(row)
        return {"summary": f"项目 {project.id} 共 {len(rows)} 个面板",
                "dashboards": rows}

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


    def _list_board_sources(self, project: Project, _identity: AgentIdentity,
                            _arguments: dict, _context: AgentRunContext) -> dict:
        boards = [b for b in self.store.list_boards(project.id) if b.kind == "taskboard"]
        rows = []
        for record in self.store.list_board_datasources(project.id):
            short = record.id.removeprefix(f"{project.id}:")
            rows.append({
                "id": short, "name": record.name, "description": record.description,
                "columns": record.status_values,
                "task_count": len(self.store.list_tasks(project.id, source_id=short)),
                "dashboards": [b.id.removeprefix(f"{project.id}:")
                               for b in boards if b.source == short],
                "updated_at": record.updated_at,
            })
        return {"summary": f"项目 {project.id} 共 {len(rows)} 个自定义任务数据源",
                "sources": rows}

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
                webhook=arguments.get("webhook"),
                actions=arguments.get("actions"),
                timeout_seconds=arguments.get("timeout_seconds"),
                actor=identity.actor,
                created_by_role_id=identity.role_id,
            )
        except ValueError as exc:
            raise AgentToolError("invalid_arguments", str(exc), 400) from exc
        triggers = [f"cron `{automation.cron}`"] if automation.cron else []
        if automation.webhook:
            triggers.append("webhook")
        # 密钥只给人看:Agent 拿到的配置里没有 webhook_secret
        return {
            "summary": (f"已{'创建' if created else '更新'}自动化脚本 "
                        f"{automation.name}({' + '.join(triggers) or '仅手动触发'})"),
            "automation": {**automation.public_dict(),
                           "webhook_path": webhook_path(automation)},
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


    def _project_automation(self, project: Project, arguments: dict):
        from .automations import normalize_automation_id

        try:
            full_id = normalize_automation_id(project.id, str(arguments.get("id", "")))
        except ValueError as exc:
            raise AgentToolError("invalid_arguments", str(exc)) from exc
        automation = self.store.get_automation(full_id)
        if automation is None or automation.project_id != project.id:
            raise AgentToolError("not_found", "自动化脚本不存在", 404)
        return automation

    def _list_automations(self, project: Project, _identity: AgentIdentity,
                          arguments: dict, _context: AgentRunContext) -> dict:
        """不带 id 列出摘要;带 id 返回脚本全文与最近运行(输出只留末尾)。"""
        def summary_row(automation) -> dict:
            return {
                "id": automation.id.removeprefix(f"{project.id}:"),
                "name": automation.name, "description": automation.description,
                "cron": automation.cron, "enabled": automation.enabled,
                "webhook": automation.webhook,
                "webhook_path": webhook_path(automation) if automation.webhook else "",
                "actions": automation.actions,
                "timeout_seconds": automation.timeout_seconds,
                "last_run_at": automation.last_run_at,
                "last_status": automation.last_status,
                "resource_url": automation_resource_url(project.id, automation.id),
            }

        if str(arguments.get("id") or "").strip():
            automation = self._project_automation(project, arguments)
            limit = arguments.get("runs", 5)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
                raise AgentToolError("invalid_arguments", "runs 必须是 1..20 的整数")
            before_id = arguments.get("before_id", 0)
            if not isinstance(before_id, int) or isinstance(before_id, bool) or before_id < 0:
                raise AgentToolError("invalid_arguments", "before_id 必须是非负整数")
            runs = []
            for row in self.store.list_automation_runs(automation.id, limit, before_id):
                run = {key: row.get(key) for key in (
                    "id", "trigger", "trigger_detail", "status", "exit_code", "error",
                    "started_at", "finished_at")}
                for stream in ("stdout", "stderr"):
                    text = str(row.get(stream) or "")
                    run[stream] = text[-AUTOMATION_OUTPUT_TAIL:]
                    run[f"{stream}_truncated"] = len(text) > AUTOMATION_OUTPUT_TAIL
                runs.append(run)
            return {"summary": (f"自动化脚本 {automation.name} 的配置与最近 {len(runs)} 次运行"
                                + (f"(id < {before_id})" if before_id else "")),
                    "automation": {**summary_row(automation), "script": automation.script},
                    "run_count": self.store.count_automation_runs(automation.id),
                    "runs": runs}
        rows = [summary_row(item) for item in self.store.list_automations(project.id)]
        return {"summary": f"项目 {project.id} 共 {len(rows)} 个自动化脚本",
                "automations": rows}

    def _run_automation(self, project: Project, identity: AgentIdentity,
                        arguments: dict, _context: AgentRunContext) -> dict:
        automation = self._project_automation(project, arguments)
        if self._trigger_automation is None:
            raise AgentToolError(
                "unavailable", "自动化调度器未就绪，请稍后重试", 503, retryable=True)
        try:
            run_id = self._trigger_automation(automation, trigger="manual")
        except ValueError as exc:
            raise AgentToolError("in_use", str(exc), 409) from exc
        self.store.audit(
            identity.actor, "automation_run_requested",
            detail=f"project={project.id} automation={automation.id} run={run_id}")
        return {
            "summary": (f"已手动触发自动化脚本 {automation.name}(运行记录 #{run_id})，"
                        "在后台执行"),
            "run_id": run_id,
            "resource_url": automation_resource_url(project.id, automation.id),
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


    def _list_guidelines(self, project: Project, _identity: AgentIdentity,
                         arguments: dict, _context: AgentRunContext) -> dict:
        sync_guideline_library(self.store, project)
        name = str(arguments.get("name") or "").strip()
        if name:
            for guideline in project.guidelines:
                if guideline.name == name:
                    url = guideline_resource_url(project.id, guideline.name)
                    return {"summary": f"准则 [{guideline.name}]({url}) 全文",
                            "guideline": {"name": guideline.name,
                                          "description": guideline.description,
                                          "enabled": guideline.enabled,
                                          "markdown": guideline.render_markdown(),
                                          "resource_url": url}}
            raise AgentToolError("not_found", f"准则不存在: {name}", 404)
        rows = [{"name": g.name, "description": g.description, "enabled": g.enabled,
                 "resource_url": guideline_resource_url(project.id, g.name)}
                for g in project.guidelines]
        enabled = sum(1 for row in rows if row["enabled"])
        return {"summary": f"项目 {project.id} 共 {len(rows)} 篇准则(启用 {enabled} 篇)",
                "guidelines": rows}

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


    def _list_skills(self, project: Project, _identity: AgentIdentity,
                     arguments: dict, _context: AgentRunContext) -> dict:
        project, _issues = sync_project_skill_library(self.store, project)
        skill_id = str(arguments.get("id") or "").strip()
        if skill_id:
            skill = next((item for item in project.skills if item.id == skill_id), None)
            if skill is None:
                raise AgentToolError("not_found", f"Skill 不存在: {skill_id}", 404)
            markdown, files = skill_package(project.id, skill.id)
            url = skill_resource_url(project.id, skill.id)
            return {"summary": f"Skill [{skill.name or skill.id}]({url}) 的 SKILL.md 与文件清单",
                    "skill": {"id": skill.id, "name": skill.name,
                              "description": skill.description, "enabled": skill.enabled,
                              "markdown": markdown, "files": files, "resource_url": url}}
        rows = [{"id": item.id, "name": item.name, "description": item.description,
                 "enabled": item.enabled,
                 "resource_url": skill_resource_url(project.id, item.id)}
                for item in project.skills]
        enabled = sum(1 for row in rows if row["enabled"])
        return {"summary": f"项目 {project.id} 共 {len(rows)} 个 Skill(启用 {enabled} 个)",
                "skills": rows}


    @staticmethod
    def _history_limit(arguments: dict) -> int:
        limit = arguments.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise AgentToolError("invalid_arguments", "limit 必须是 1..100 的整数")
        return limit

    @staticmethod
    def _history_content(data: bytes) -> dict:
        """历史版本正文:UTF-8 文本原样返回,否则给 base64(与 document.publish 对称)。"""
        if len(data) > MAX_HISTORY_READ_BYTES:
            raise AgentToolError(
                "file_too_large",
                f"该版本超过 {MAX_HISTORY_READ_BYTES // 1024 // 1024} MB，请在 Web 端下载", 413)
        try:
            return {"content": data.decode("utf-8")}
        except UnicodeDecodeError:
            return {"content_base64": base64.b64encode(data).decode("ascii")}

    def _document_history(self, project: Project, _identity: AgentIdentity,
                          arguments: dict, _context: AgentRunContext) -> dict:
        path = str(arguments.get("path") or "").strip()
        if not path:
            raise AgentToolError("invalid_arguments", "需要提供 path")
        library = library_for(project.id)
        url = document_resource_url(project.id, path)
        revision = str(arguments.get("revision") or "").strip()
        if revision:
            try:
                data = library.read_history_bytes(path, revision)
            except FileNotFoundError as exc:
                raise AgentToolError("not_found", str(exc), 404) from exc
            return {"summary": f"文档 {path} 在版本 {revision[:10]} 的内容",
                    "path": path, "revision": revision, "resource_url": url,
                    **self._history_content(data)}
        versions = library.history(path, self._history_limit(arguments))
        if not versions:
            raise AgentToolError("not_found", f"文档没有历史版本: {path}", 404)
        return {"summary": f"文档 {path} 最近 {len(versions)} 个版本",
                "path": path, "versions": versions, "resource_url": url}

    def _guideline_history(self, project: Project, _identity: AgentIdentity,
                           arguments: dict, _context: AgentRunContext) -> dict:
        name = str(arguments.get("name") or "").strip()
        if not name:
            raise AgentToolError("invalid_arguments", "需要提供 name")
        sync_guideline_library(self.store, project)
        url = guideline_resource_url(project.id, name)
        revision = str(arguments.get("revision") or "").strip()
        try:
            if revision:
                version = read_guideline_version(self.store, project, name, revision)
                return {"summary": f"准则 {name} 在版本 {revision[:10]} 的全文",
                        "name": name, "resource_url": url,
                        "version": {key: version[key] for key in (
                            "revision", "created_at", "actor", "message")},
                        "markdown": version["markdown"]}
            versions = guideline_history(
                self.store, project, name, self._history_limit(arguments))
        except FileNotFoundError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        return {"summary": f"准则 {name} 最近 {len(versions)} 个版本",
                "name": name, "versions": versions, "resource_url": url}

    def _skill_history(self, project: Project, _identity: AgentIdentity,
                       arguments: dict, _context: AgentRunContext) -> dict:
        skill_id = self._control_id(arguments.get("id"))
        url = skill_resource_url(project.id, skill_id)
        revision = str(arguments.get("revision") or "").strip()
        file = str(arguments.get("file") or "").strip()
        if file and not revision:
            raise AgentToolError("invalid_arguments", "file 需要和 revision 一起提供")
        try:
            if revision:
                package = read_skill_version(self.store, project, skill_id, revision)
                result = {"summary": f"Skill {skill_id} 在版本 {revision[:10]} 的内容",
                          "id": skill_id, "revision": package["revision"],
                          "markdown": package["markdown"], "files": package["files"],
                          "resource_url": url}
                if file:
                    if file not in package["files"]:
                        raise AgentToolError(
                            "not_found", f"该版本的 Skill 包里没有文件: {file}", 404)
                    data = skill_version_library(project.id).read_bytes(
                        skill_id, file, package["revision"])
                    result.update(file=file, **self._history_content(data))
                return result
            versions = skill_history(
                self.store, project, skill_id, self._history_limit(arguments))
        except FileNotFoundError as exc:
            raise AgentToolError("not_found", str(exc), 404) from exc
        return {"summary": f"Skill {skill_id} 最近 {len(versions)} 个版本",
                "id": skill_id, "versions": versions, "resource_url": url}

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
