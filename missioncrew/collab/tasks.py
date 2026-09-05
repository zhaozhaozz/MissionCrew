"""Issue 化 Task 的统一写入、状态简报与 Channel 派发逻辑。"""
from __future__ import annotations

import time
from typing import Optional

from .resource_urls import task_resource_url
from ..core import label_query
from ..core.models import (Channel, Task, new_id, normalize_labels, status_of,
                           with_status)
from ..core.store import Store

DEFAULT_STATUS = "待处理"
DONE_STATUS = "已完成"
DISPATCHED_STATUS = "处理中"


class TaskDispatchError(ValueError):
    """多 Channel 派发只完成一部分时，保留已经产生的消息。"""


def _text(value: object, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    result = value.strip() if required else value
    if required and not result:
        raise ValueError(f"{field} 必须是非空字符串")
    return result


def _labels(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("labels 必须是字符串数组")
    return normalize_labels(value)


def task_channels(store: Store, project_id: str, channel_ids: object,
                  *, fallback_channel_id: str = "") -> list[Channel]:
    """校验绑定 Channel；新任务可回退到当前或项目 general Channel。"""
    if not isinstance(channel_ids, list) or not all(
            isinstance(item, str) for item in channel_ids):
        raise ValueError("channel_ids 必须是字符串数组")
    ids = list(dict.fromkeys(item.strip() for item in channel_ids if item.strip()))
    available = [channel for channel in store.list_channels(project_id)
                 if not channel.archived]
    if not ids:
        fallback = next(
            (channel for channel in available if channel.id == fallback_channel_id),
            None,
        ) or next(
            (channel for channel in available
             if channel.id == "general" or channel.id.endswith(":general")),
            available[0] if available else None,
        )
        # 频道绑定可选:没有可回退频道时返回空绑定,派发时再要求频道
        ids = [fallback.id] if fallback is not None else []
    result = []
    for channel_id in ids:
        channel = (store.get_channel(channel_id)
                   or store.get_channel(f"{project_id}:{channel_id}"))
        if channel is None or channel.project_id != project_id:
            raise ValueError(f"Channel 不存在或不属于当前项目: {channel_id}")
        if channel.archived:
            raise ValueError(f"Task 不能绑定已归档 Channel: {channel_id}")
        if channel.id not in {item.id for item in result}:
            result.append(channel)
    return result


def create_task(store: Store, project_id: str, *, title: object,
                summary: object = "", body: object = "", labels: object = None,
                channel_ids: object = None, status: object = None,
                actor: str = "human", fallback_channel_id: str = "") -> Task:
    if store.get_project(project_id) is None:
        raise ValueError("项目不存在")
    channels = task_channels(
        store, project_id, channel_ids if channel_ids is not None else [],
        fallback_channel_id=fallback_channel_id,
    )
    # 状态即标签:显式 status 参数优先,其次保留 labels 里已有的状态标签,
    # 两者都没有时落默认状态。状态文本不做合法性校验。
    label_list = _labels(labels if labels is not None else [])
    status_text = str(status).strip() if status is not None else ""
    if status_text or not status_of(label_list):
        label_list = with_status(label_list, status_text or DEFAULT_STATUS)
    task = Task(
        id=new_id("t"), project_id=project_id,
        title=_text(title, "title", required=True),
        summary=_text(summary, "summary"), body=_text(body, "body"),
        labels=label_list,
        channel_ids=[channel.id for channel in channels],
    )
    store.put_task(task)
    store.audit(actor, "task_created", task.id, f"project={project_id}")
    return task


def update_task(store: Store, task: Task, *, snapshot_updated_at: object,
                changes: dict, actor: str = "human") -> Task:
    if task.archived:
        raise ValueError("Task 已归档，请先恢复后再修改")
    if (not isinstance(snapshot_updated_at, (int, float))
            or isinstance(snapshot_updated_at, bool)
            or abs(float(snapshot_updated_at) - task.updated_at) > 1e-6):
        raise ValueError("任务已被其他执行更新，请重新读取后再修改")
    if "title" in changes:
        task.title = _text(changes["title"], "title", required=True)
    if "summary" in changes:
        task.summary = _text(changes["summary"], "summary")
    if "body" in changes:
        task.body = _text(changes["body"], "body")
    if "labels" in changes:
        task.labels = _labels(changes["labels"])
    if "status" in changes:
        task.labels = with_status(
            task.labels, _text(changes["status"], "status", required=True))
    if "channel_ids" in changes:
        ids = changes["channel_ids"]
        if isinstance(ids, list) and not ids:
            task.channel_ids = []   # 频道绑定可选:显式空列表即解除绑定
        else:
            task.channel_ids = [channel.id for channel in task_channels(
                store, task.project_id, ids)]
    store.put_task(task)
    store.audit(actor, "task_updated", task.id, f"project={task.project_id}")
    return task


def add_task_brief(store: Store, task: Task, *, content: object,
                   status: Optional[str] = None, author: str = "human",
                   author_type: str = "human") -> dict:
    if task.archived:
        raise ValueError("Task 已归档，请先恢复后再追加状态简报")
    text = _text(content, "content", required=True)
    status_text = str(status).strip() if status is not None else ""
    if status_text and status_text != status_of(task.labels):
        task.labels = with_status(task.labels, status_text)
    brief = store.add_task_brief(
        task.id, author, author_type, text,
        status_text or status_of(task.labels))
    # 即使状态不变，新增进展也必须刷新最近活动时间和乐观锁版本。
    store.put_task(task)
    store.audit(author, "task_brief_added", task.id,
                f"project={task.project_id} "
                f"status={status_text or status_of(task.labels)}")
    return brief


def _task_block(task: Task) -> str:
    return (
        f"请处理 Task [{task.id} · {task.title}]"
        f"({task_resource_url(task.project_id, task.id)})。\n\n"
        f"**简介**\n{task.summary or '（无）'}\n\n"
        f"**正文**\n{task.body or '（无）'}\n\n"
        "请在本 Channel 中协调处理，并通过 Task 编辑或状态简报同步进展。"
    )


def _require_enabled_role(store: Store, project_id: str, role_id: str) -> None:
    role = store.get_role(project_id, role_id)
    if role is None:
        raise ValueError(f"角色不存在: @{role_id}")
    if not role.enabled:
        raise ValueError(f"角色已停用，请先启用: @{role_id}")


def dispatch_task(store: Store, chat, task: Task, *, message: str = "",
                  mention_spans: Optional[list[dict]] = None,
                  target_role_ids: Optional[list[str]] = None,
                  author: str = "human",
                  author_type: str = "human") -> tuple[list[dict], dict]:
    """把 Task 作为普通 Channel 消息派发。

    三种派发形态:
    - ``mention_spans``:message 内含角色选择器生成的结构化提及,提及目标
      即派发目标(单角色直达、多角色由聊天路由收敛给主控);
    - ``target_role_ids``:自动规则等程序化调用,由平台生成 ``@角色`` 前缀;
    - 两者都为空:默认交给项目主控,message 作为本次补充;无主控项目必须点名角色。
    """
    if task.archived:
        raise ValueError("Task 已归档，请先恢复后再派发")
    if status_of(task.labels) == DONE_STATUS:
        raise ValueError("已完成 Task 不能再次派发；请先重新打开")
    project = store.get_project(task.project_id)
    if project is None:
        raise ValueError("项目不存在")
    extra = message.strip()
    spans = [dict(item) for item in mention_spans or []]
    targets = [str(item) for item in dict.fromkeys(target_role_ids or [])]

    if spans:
        target_ids = list(dict.fromkeys(
            str(span.get("role_id", "")) for span in spans))
        for role_id in target_ids:
            _require_enabled_role(store, task.project_id, role_id)
        # 用户消息在前,提及范围保持原位;Task 详情追加在后
        content = (extra + "\n\n" if extra else "") + _task_block(task)
        legal_spans: Optional[list[dict]] = spans
    elif targets:
        for role_id in targets:
            _require_enabled_role(store, task.project_id, role_id)
        prefix = " ".join(f"@{role_id}" for role_id in targets)
        content = prefix + " " + _task_block(task)
        if extra:
            content += f"\n\n**处理要求**\n{extra}"
        legal_spans = []
        offset = 0
        for role_id in targets:
            legal_spans.append({"role_id": role_id, "start": offset,
                                "end": offset + len(role_id) + 1})
            offset += len(role_id) + 2   # "@role" + 分隔空格
        target_ids = targets
    else:
        lead_id = project.orchestrator_role_id
        if not lead_id:
            raise ValueError("本项目没有主控,请 @ 指定处理角色")
        lead = store.get_role(project.id, lead_id)
        if lead is None:
            raise ValueError(f"项目主控角色不存在: @{lead_id}")
        if not lead.enabled:
            raise ValueError(f"项目主控角色已停用，请先启用: @{lead_id}")
        content = f"@{lead_id} " + _task_block(task)
        if extra:
            content += f"\n\n**本次补充**\n{extra}"
        legal_spans = [{
            "role_id": lead_id, "start": 0, "end": len(lead_id) + 1,
        }]
        target_ids = [lead_id]

    channels = task_channels(store, task.project_id, task.channel_ids)
    if not channels:
        raise ValueError("Task 派发需要至少一个可用 Channel")
    task.channel_ids = [channel.id for channel in channels]
    previous_labels = list(task.labels)
    task.labels = with_status(task.labels, DISPATCHED_STATUS)
    store.put_task(task)
    handled_by = "、".join(f"@{role_id}" for role_id in target_ids)
    sent: list[dict] = []
    try:
        for channel in channels:
            message_id = chat.post(
                channel.id, author, content, author_type=author_type,
                mention_spans=legal_spans,
            )
            sent.append({"channel_id": channel.id, "message_id": message_id})
    except ValueError as exc:
        if sent:
            store.add_task_brief(
                task.id, "platform", "system",
                f"已向 {len(sent)} 个 Channel 的 {handled_by} 派发；"
                f"后续派发失败：{exc}",
                status_of(task.labels),
            )
            store.put_task(task)
            raise TaskDispatchError(str(exc)) from exc
        else:
            task.labels = previous_labels
            store.put_task(task)
            raise

    brief = store.add_task_brief(
        task.id, author, author_type,
        f"已交给 {handled_by} 处理：" + "、".join(channel.name or channel.id
                                                for channel in channels),
        status_of(task.labels),
    )
    store.put_task(task)
    store.audit(author, "task_dispatched", task.id,
                f"project={task.project_id} targets={','.join(target_ids)} "
                f"channels={','.join(task.channel_ids)}")
    return sent, brief


def matching_auto_rule(project, task: Task):
    """返回第一条标签表达式命中 Task 的启用规则;不命中返回 None。"""
    for rule in getattr(project, "task_auto_rules", []):
        if not (rule.enabled and rule.query):
            continue
        try:
            if label_query.matches(rule.query, task.labels):
                return rule
        except ValueError:
            continue   # 规则表达式非法时跳过,不阻塞任务创建
    return None


def auto_process_task(store: Store, chat, task: Task) -> Optional[dict]:
    """新建 Task 命中自动处理规则时立即派发;派发失败不影响 Task 创建。"""
    project = store.get_project(task.project_id)
    if project is None or task.archived \
            or status_of(task.labels) == DONE_STATUS:
        return None
    rule = matching_auto_rule(project, task)
    if rule is None:
        return None
    try:
        # 新规则带结构化提及,与人工"交给主控处理"同一通道;
        # 旧规则只有 role_ids,平台生成 @前缀保持兼容
        sent, brief = dispatch_task(
            store, chat, task, message=rule.prompt,
            mention_spans=rule.mentions or None,
            target_role_ids=None if rule.mentions else rule.role_ids,
            author="task-rule", author_type="automation",
        )
    except (TaskDispatchError, ValueError) as exc:
        store.add_task_brief(
            task.id, "platform", "system",
            f"自动处理规则(`{rule.query}`)派发失败：{exc}",
            status_of(task.labels))
        store.put_task(task)
        store.audit("platform", "task_auto_dispatch_failed", task.id,
                    f"project={task.project_id} rule={rule.query} error={exc}")
        return None
    store.audit("platform", "task_auto_dispatched", task.id,
                f"project={task.project_id} rule={rule.query} "
                f"roles={','.join(rule.role_ids) or '(orchestrator)'}")
    return {"rule_query": rule.query, "sent": sent, "brief": brief}


def archive_task(store: Store, task: Task, *, actor: str = "human") -> Task:
    """把 Task 从活跃视图和 Agent 快照中收起，但保留全部内容。"""
    if not task.archived:
        task.archived = True
        task.archived_at = time.time()
        store.put_task(task)
        store.audit(
            actor, "task_archived", task.id, f"project={task.project_id}")
    return task


def restore_task(store: Store, task: Task, *, actor: str = "human") -> Task:
    """恢复归档 Task，并把恢复动作计入最近活动。"""
    if task.archived:
        task.archived = False
        task.archived_at = 0.0
        store.put_task(task)
        store.audit(
            actor, "task_restored", task.id, f"project={task.project_id}")
    return task
