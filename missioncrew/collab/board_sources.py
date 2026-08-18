"""任务看板数据源注册表:看板从数据源取卡片,标签表达式在服务端过滤。

每个数据源声明列定义(按卡片 status 分列)与取数函数,产出统一卡片结构:
id/title/summary/status/labels/updated_at/meta,外加打开方式(task_id 打开
平台任务详情,url 打开外部链接)。前端只做通用渲染,不感知数据来自哪里;
新增数据源(如 GitHub 同步的 Issue 列表)= 定义 BoardSource 并加进 SOURCES,
不动面板存取与渲染链路。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..core import label_query


@dataclass(frozen=True)
class BoardSource:
    """一个看板数据源:列定义 + 取数函数。"""

    id: str
    name: str
    description: str
    # ({"key","title","color"},...):卡片按 status == key 归列,颜色是 CSS 值
    columns: tuple
    # (store, project_id) -> 标准化卡片列表(未经标签表达式过滤)
    fetch: Callable[..., list]


def _fetch_tasks(store, project_id: str) -> list[dict]:
    cards = []
    for task in store.list_tasks():
        if task.project_id != project_id or task.archived:
            continue
        meta = [task.id]
        for channel_id in task.channel_ids:
            channel = store.get_channel(channel_id)
            meta.append(f"#{channel.name or channel.id}" if channel else channel_id)
        cards.append({
            "id": task.id, "title": task.title, "summary": task.summary,
            "status": task.status, "labels": list(task.labels),
            "updated_at": task.updated_at, "meta": meta,
            "task_id": task.id,
        })
    return cards


TASKS_SOURCE = BoardSource(
    id="tasks", name="项目任务",
    description="本项目的 Task 列表,按状态分列",
    columns=(
        {"key": "open", "title": "待处理", "color": "var(--muted)"},
        {"key": "in_progress", "title": "处理中", "color": "var(--accent)"},
        {"key": "blocked", "title": "已阻塞", "color": "var(--bad)"},
        {"key": "done", "title": "已完成", "color": "var(--ok)"},
    ),
    fetch=_fetch_tasks,
)

SOURCES: dict[str, BoardSource] = {s.id: s for s in (TASKS_SOURCE,)}
DEFAULT_SOURCE = TASKS_SOURCE.id


def describe_sources() -> list[dict]:
    """创建/编辑看板时展示的可选数据源清单。"""
    return [{"id": s.id, "name": s.name, "description": s.description,
             "columns": list(s.columns)} for s in SOURCES.values()]


def resolve_board_data(store, project_id: str, source_id: str, query: str) -> dict:
    """解析看板数据:取数 -> 标签表达式过滤;表达式非法抛 ValueError。"""
    source = SOURCES.get(source_id)
    if source is None:
        raise ValueError(f"未知数据源: {source_id}")
    ast = label_query.parse(query)
    cards = [card for card in source.fetch(store, project_id)
             if label_query.matches(ast, card.get("labels", []))]
    return {"source": {"id": source.id, "name": source.name},
            "columns": list(source.columns), "cards": cards}
