"""MissionCrew 资源的稳定 Web URL。

这些 URL 是面向聊天消息和 Web 导航的公开标识，不对应平台数据目录。
Runtime 读写仍使用 ExecutionConfig 中显式授权的真实路径。
"""
from __future__ import annotations

import re
from urllib.parse import quote


_PROJECT_ID_RE = re.compile(r"[\w-]+")
RESOURCE_TYPES = frozenset({
    "documents", "channels", "tasks", "dashboards", "guidelines", "skills",
})


def missioncrew_project_url(project_id: str) -> str:
    """返回项目资源 URL 的公共前缀。"""
    if not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("项目 id 只能包含字母、数字、下划线、连字符")
    return f"/resources/{quote(project_id, safe='')}"


def missioncrew_resource_url(project_id: str, resource_type: str,
                             *segments: str) -> str:
    """用逐段编码的稳定标识构造 MissionCrew 资源 URL。"""
    if resource_type not in RESOURCE_TYPES:
        raise ValueError(f"未知 MissionCrew 资源类型: {resource_type}")
    base = f"{missioncrew_project_url(project_id)}/{resource_type}"
    clean_segments = []
    for segment in segments:
        value = str(segment).strip()
        if not value or value in (".", "..") or "/" in value or "\\" in value:
            raise ValueError("资源 URL 标识必须是非空的单一路径段")
        clean_segments.append(quote(value, safe=""))
    return "/".join((base, *clean_segments))


def _short_project_id(project_id: str, item_id: str) -> str:
    return str(item_id).removeprefix(f"{project_id}:")


def channel_resource_url(project_id: str, channel_id: str) -> str:
    return missioncrew_resource_url(
        project_id, "channels", _short_project_id(project_id, channel_id))


def task_resource_url(project_id: str, task_id: str) -> str:
    return missioncrew_resource_url(project_id, "tasks", task_id)


def dashboard_resource_url(project_id: str, board_id: str | None = None) -> str:
    """返回面板 URL；``tasks`` 是平台内置任务看板。"""
    if board_id is None:
        return missioncrew_resource_url(project_id, "dashboards")
    return missioncrew_resource_url(
        project_id, "dashboards", _short_project_id(project_id, board_id))


def guideline_resource_url(project_id: str, guideline_name: str) -> str:
    return missioncrew_resource_url(project_id, "guidelines", guideline_name)


def skill_resource_url(project_id: str, skill_id: str,
                       relative: str | None = None) -> str:
    segments = [skill_id]
    if relative:
        parts = relative.replace("\\", "/").split("/")
        if any(not part or part in (".", "..") for part in parts):
            raise ValueError("Skill 文件路径必须是 Skill 目录内的相对路径")
        segments.extend(parts)
    return missioncrew_resource_url(project_id, "skills", *segments)
