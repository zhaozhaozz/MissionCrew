"""项目内容页与专属 Channel 的稳定绑定。"""
from __future__ import annotations

import hashlib

from ..core.models import Channel, Project
from ..core.store import Store


CONTENT_KIND_LABELS = {
    "docs": "文档",
    "guidelines": "准则",
    "skills": "Skill",
}


def content_channel(
        store: Store, project_id: str, content_kind: str,
        content_key: str) -> Channel | None:
    """按持久化绑定查找内容频道，不依赖频道 id 的生成方式。"""
    return next(
        (channel for channel in store.list_channels(project_id)
         if channel.content_kind == content_kind
         and channel.content_key == content_key),
        None,
    )


def ensure_content_channel(
        store: Store, project: Project, content_kind: str, content_key: str,
        label: str = "") -> tuple[Channel, bool]:
    """返回内容页的专属频道；重复解析同一内容时保持同一个频道和消息历史。"""
    if content_kind not in CONTENT_KIND_LABELS:
        raise ValueError(f"不支持的内容类型：{content_kind}")
    key = str(content_key).strip()
    if not key:
        raise ValueError("内容键不能为空")

    existing = content_channel(store, project.id, content_kind, key)
    title = str(label or key).strip() or key
    name = f"{CONTENT_KIND_LABELS[content_kind]} · {title}"
    purpose = f"围绕{CONTENT_KIND_LABELS[content_kind]}「{title}」的页面内协作"
    if existing is not None:
        if existing.name != name or existing.purpose != purpose:
            existing.name = name
            existing.purpose = purpose
            store.put_channel(existing)
        return existing, False

    digest = hashlib.sha256(
        f"{content_kind}\0{key}".encode("utf-8")).hexdigest()[:16]
    channel_id = f"{project.id}:content-{content_kind}-{digest}"
    channel = store.get_channel(channel_id)
    if channel is not None:
        if channel.content_kind != content_kind or channel.content_key != key:
            raise ValueError("内容频道 id 冲突")
        return channel, False

    channel = Channel(
        id=channel_id,
        name=name,
        project_id=project.id,
        purpose=purpose,
        content_kind=content_kind,
        content_key=key,
    )
    store.put_channel(channel)
    return channel, True


def rebind_content_channel(
        store: Store, project: Project, content_kind: str, old_key: str,
        new_key: str, label: str = "") -> Channel | None:
    """内容重命名时迁移绑定，使原频道、消息和 Runtime 会话继续沿用。"""
    old = str(old_key).strip()
    new = str(new_key).strip()
    if not old or not new or old == new:
        return None
    channel = content_channel(store, project.id, content_kind, old)
    if channel is None:
        return None
    target = content_channel(store, project.id, content_kind, new)
    if target is not None and target.id != channel.id:
        return target
    channel.content_key = new
    title = str(label or new).strip() or new
    channel.name = f"{CONTENT_KIND_LABELS[content_kind]} · {title}"
    channel.purpose = f"围绕{CONTENT_KIND_LABELS[content_kind]}「{title}」的页面内协作"
    store.put_channel(channel)
    return channel


def ensure_project_content_channels(store: Store, project: Project) -> int:
    """为项目当前已有的每篇文档、准则和 Skill 补齐专属频道。"""
    from .documents import library_for

    created = 0
    for item in library_for(project.id).list_files():
        _channel, is_new = ensure_content_channel(
            store, project, "docs", item["path"], item["path"])
        created += int(is_new)
    for guideline in project.guidelines:
        _channel, is_new = ensure_content_channel(
            store, project, "guidelines", guideline.name, guideline.name)
        created += int(is_new)
    for skill in project.skills:
        _channel, is_new = ensure_content_channel(
            store, project, "skills", skill.id, skill.name or skill.id)
        created += int(is_new)
    return created
