"""任务看板数据源注册表:看板从数据源取卡片,标签筛选在服务端完成。

数据源分两类:内置源(如 tasks,取数函数实时读平台数据)与自定义源
(BoardDataSource,由主控 board_source.save 创建、自动化脚本整体刷新卡片,
适合定时同步 GitCode/GitHub Issue 这类外部列表)。两类产出统一卡片结构:
id/title/summary/status/labels/updated_at/meta,外加打开方式(task_id 打开
平台任务详情,url 打开外部链接)。

看板列由 filters([{title,query,color}],每列一个标签表达式)定义;匹配时
卡片的状态 key 与状态列标题(待处理/处理中/已阻塞/已完成 等)都作为可筛选
标签参与,filters 为空时回退按数据源状态列生成默认列。前端只做通用渲染,
不感知数据来自任务还是外部同步列表。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..core import label_query
from ..core.models import DEFAULT_BOARD_COLUMNS

MAX_BOARD_FILTERS = 20
MAX_SOURCE_COLUMNS = 12
MAX_SOURCE_CARDS = 1000
# 卡片允许的字段:与内置任务源产出的标准卡片结构一致(外链卡片用 url)
SOURCE_CARD_FIELDS = {"id", "title", "summary", "status", "labels",
                      "updated_at", "meta", "url"}
# 列 key/标题不能包含表达式运算符:标题会作为标签参与表达式匹配
_OPERATOR_CHARS = set("&|!()")


@dataclass(frozen=True)
class BoardSource:
    """一个内置看板数据源:状态列定义 + 取数函数。"""

    id: str
    name: str
    description: str
    # ({"key","title","color"},...):title 兼作状态标签,颜色是 CSS 值
    columns: tuple
    # (store, project_id) -> 标准化卡片列表(未经表达式过滤)
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
    description="本项目的 Task 列表",
    columns=tuple(dict(c) for c in DEFAULT_BOARD_COLUMNS),
    fetch=_fetch_tasks,
)

SOURCES: dict[str, BoardSource] = {s.id: s for s in (TASKS_SOURCE,)}
DEFAULT_SOURCE = TASKS_SOURCE.id


def custom_source_full_id(project_id: str, source_id: str) -> str:
    return f"{project_id}:{source_id}"


def get_custom_source(store, project_id: str, source_id: str):
    record = store.get_board_datasource(
        custom_source_full_id(project_id, source_id))
    if record is not None and record.project_id != project_id:
        return None
    return record


def source_exists(store, project_id: str, source_id: str) -> bool:
    return (source_id in SOURCES
            or get_custom_source(store, project_id, source_id) is not None)


def source_columns(store, project_id: str, source_id: str) -> list[dict]:
    if source_id in SOURCES:
        return [dict(c) for c in SOURCES[source_id].columns]
    record = get_custom_source(store, project_id, source_id)
    if record is None:
        raise ValueError(f"未知数据源: {source_id}")
    return [dict(c) for c in record.columns]


def describe_sources(store=None, project_id: Optional[str] = None) -> list[dict]:
    """创建/编辑看板时展示的可选数据源清单(内置 + 本项目自定义)。"""
    items = [{"id": s.id, "name": s.name, "description": s.description,
              "columns": [dict(c) for c in s.columns], "custom": False}
             for s in SOURCES.values()]
    if store is not None and project_id:
        for record in store.list_board_datasources(project_id):
            items.append({
                "id": record.id.removeprefix(f"{project_id}:"),
                "name": record.name or record.id,
                "description": record.description,
                "columns": [dict(c) for c in record.columns],
                "custom": True, "cards": len(record.cards),
                "updated_at": record.updated_at,
            })
    return items


def default_filters(columns) -> list[dict]:
    """按数据源状态列生成默认筛选列:列标题即状态标签。"""
    return [{"title": str(c.get("title") or c.get("key") or ""),
             "query": str(c.get("title") or c.get("key") or ""),
             "color": str(c.get("color") or "")} for c in columns]


def validate_filters(raw) -> list[dict]:
    """校验筛选列定义并归一化;非法抛 ValueError。"""
    if not isinstance(raw, list):
        raise ValueError("filters 必须是数组")
    if len(raw) > MAX_BOARD_FILTERS:
        raise ValueError(f"筛选列最多 {MAX_BOARD_FILTERS} 个")
    filters = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("filters 每项必须是 {title,query,color} 对象")
        unknown = set(item) - {"title", "query", "color"}
        if unknown:
            raise ValueError(f"筛选列含未知字段: {', '.join(sorted(unknown))}")
        query = str(item.get("query") or "").strip()
        try:
            label_query.parse(query)
        except ValueError as exc:
            raise ValueError(f"筛选列表达式不合法: {exc}") from exc
        filters.append({
            "title": str(item.get("title") or "").strip() or query,
            "query": query,
            "color": str(item.get("color") or ""),
        })
    return filters


def _clean_tag_text(value, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{what}不能为空")
    if set(text) & _OPERATOR_CHARS:
        raise ValueError(f"{what}不能包含表达式运算符 & | ! ( ):{text}")
    return text


def validate_source_columns(raw) -> list[dict]:
    """校验自定义数据源的状态列;key/标题兼作状态标签,不允许运算符。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("columns 必须是非空数组")
    if len(raw) > MAX_SOURCE_COLUMNS:
        raise ValueError(f"状态列最多 {MAX_SOURCE_COLUMNS} 个")
    columns, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("columns 每项必须是 {key,title,color} 对象")
        unknown = set(item) - {"key", "title", "color"}
        if unknown:
            raise ValueError(f"状态列含未知字段: {', '.join(sorted(unknown))}")
        key = _clean_tag_text(item.get("key"), "状态列 key")
        if key in seen:
            raise ValueError(f"状态列 key 重复: {key}")
        seen.add(key)
        title = _clean_tag_text(item.get("title") or key, "状态列标题")
        columns.append({"key": key, "title": title,
                        "color": str(item.get("color") or "")})
    return columns


def validate_source_cards(raw, columns) -> list[dict]:
    """校验并归一化自定义数据源卡片(整体替换语义);非法抛 ValueError。"""
    if not isinstance(raw, list):
        raise ValueError("cards 必须是数组")
    if len(raw) > MAX_SOURCE_CARDS:
        raise ValueError(f"卡片最多 {MAX_SOURCE_CARDS} 张")
    known_status = {str(c.get("key")) for c in columns}
    default_status = str(columns[0].get("key")) if columns else ""
    cards, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("cards 每项必须是对象")
        unknown = set(item) - SOURCE_CARD_FIELDS
        if unknown:
            raise ValueError(f"卡片含未知字段: {', '.join(sorted(unknown))}")
        card_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not card_id or not title:
            raise ValueError("每张卡片必须有 id 和 title")
        if card_id in seen:
            raise ValueError(f"卡片 id 重复: {card_id}")
        seen.add(card_id)
        status = str(item.get("status") or "").strip() or default_status
        if status not in known_status:
            raise ValueError(
                f"卡片 {card_id} 的 status `{status}` 不在状态列中,"
                f"可用: {', '.join(sorted(known_status))}")
        labels_raw = item.get("labels") or []
        if (not isinstance(labels_raw, list)
                or not all(isinstance(x, str) for x in labels_raw)):
            raise ValueError(f"卡片 {card_id} 的 labels 必须是字符串数组")
        labels = list(dict.fromkeys(x.strip() for x in labels_raw if x.strip()))
        meta_raw = item.get("meta") or []
        if not isinstance(meta_raw, list):
            raise ValueError(f"卡片 {card_id} 的 meta 必须是数组")
        card = {
            "id": card_id, "title": title,
            "summary": str(item.get("summary") or ""),
            "status": status, "labels": labels,
            "updated_at": float(item.get("updated_at") or time.time()),
            "meta": [str(x) for x in meta_raw],
        }
        url = str(item.get("url") or "").strip()
        if url:
            card["url"] = url
        cards.append(card)
    return cards


def _status_tags(status: str, columns) -> list[str]:
    """卡片状态映射出的可筛选标签:状态 key 与其状态列标题。"""
    tags = [status] if status else []
    for col in columns:
        if col.get("key") == status:
            title = str(col.get("title") or "")
            if title and title not in tags:
                tags.append(title)
    return tags


def resolve_board_data(store, project_id: str, source_id: str,
                       filters: Optional[list] = None) -> dict:
    """解析看板数据:取数 -> 按筛选列分列。

    卡片可命中多列(筛选列是标签视角,不是互斥状态);表达式非法抛 ValueError。
    """
    if source_id in SOURCES:
        source = SOURCES[source_id]
        name = source.name
        columns = [dict(c) for c in source.columns]
        cards = source.fetch(store, project_id)
    else:
        record = get_custom_source(store, project_id, source_id)
        if record is None:
            raise ValueError(f"未知数据源: {source_id}")
        name = record.name or source_id
        columns = [dict(c) for c in record.columns]
        cards = [dict(card) for card in record.cards]

    # (card, 可筛选标签集合=labels + 状态标签)
    matchable = [
        (card, list(card.get("labels") or [])
         + _status_tags(str(card.get("status") or ""), columns))
        for card in cards
    ]

    column_filters = (validate_filters(filters) if filters
                      else default_filters(columns))
    out_columns = []
    for index, item in enumerate(column_filters):
        ast = label_query.parse(item["query"])
        out_columns.append({
            "key": f"f{index}", "title": item["title"], "query": item["query"],
            "color": item["color"] or "var(--muted)",
            "cards": [card for card, tags in matchable
                      if label_query.matches(ast, tags)],
        })
    # 可筛选标签全集(含状态标签),供前端做筛选输入建议
    labels = {str(tag) for _, tags in matchable for tag in tags}
    labels.update(str(c.get("title") or "") for c in columns)
    labels.discard("")
    return {"source": {"id": source_id, "name": name},
            "columns": out_columns,
            "labels": sorted(labels),
            "filters": column_filters if filters else []}
