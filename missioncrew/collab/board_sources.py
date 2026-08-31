"""任务看板数据源:所有卡片都来自统一的 tasks 表,按 source_id 圈定范围。

数据源分两类:内置源 `built-in`(平台原生任务)与自定义源(BoardDataSource,
由主控 board_source.save 创建、自动化脚本按 external_id upsert 任务,适合
定时同步 GitCode/GitHub Issue 这类外部列表)。状态就是 `status: 文本` 标签,
数据源只声明状态取值的顺序与颜色,用于生成默认筛选列。

看板列有两种模式:filters([{title,query,color}],每列一个标签表达式)或
group_by(按属性取值动态分列,末尾追加「未设置」列);标签匹配在服务端完成,
前端只做通用渲染。
"""
from __future__ import annotations

import time
from typing import Optional

from ..core import label_query
from ..core.models import (BUILTIN_SOURCE_ID, BUILTIN_STATUS_VALUES,
                           LEGACY_STATUS_TEXT, STATUS_PROPERTY, Task, new_id,
                           normalize_labels, normalize_status_values,
                           status_of, with_status)

MAX_BOARD_FILTERS = 20
MAX_SOURCE_STATUS_VALUES = 12
MAX_SOURCE_CARDS = 1000
# 同步卡片允许的字段(status 是状态文本;旧脚本发状态列 key 也能映射)
SOURCE_CARD_FIELDS = {"id", "title", "summary", "status", "labels",
                      "updated_at", "meta", "url"}
# 状态取值不能包含表达式运算符:状态文本要参与标签表达式匹配
_OPERATOR_CHARS = set("&|!()")

# 内置源沿用过的旧 id,读取时归一化
_LEGACY_BUILTIN_IDS = {"tasks", BUILTIN_SOURCE_ID}


def normalize_source_id(source_id: str) -> str:
    text = str(source_id or "").strip()
    return BUILTIN_SOURCE_ID if text in _LEGACY_BUILTIN_IDS or not text else text


def custom_source_full_id(project_id: str, source_id: str) -> str:
    return f"{project_id}:{source_id}"


def get_custom_source(store, project_id: str, source_id: str):
    record = store.get_board_datasource(
        custom_source_full_id(project_id, source_id))
    if record is not None and record.project_id != project_id:
        return None
    return record


def source_exists(store, project_id: str, source_id: str) -> bool:
    source_id = normalize_source_id(source_id)
    return (source_id == BUILTIN_SOURCE_ID
            or get_custom_source(store, project_id, source_id) is not None)


def source_status_values(store, project_id: str, source_id: str) -> list[dict]:
    source_id = normalize_source_id(source_id)
    if source_id == BUILTIN_SOURCE_ID:
        return [dict(c) for c in BUILTIN_STATUS_VALUES]
    record = get_custom_source(store, project_id, source_id)
    if record is None:
        raise ValueError(f"未知数据源: {source_id}")
    return [dict(c) for c in record.status_values]


def describe_sources(store=None, project_id: Optional[str] = None) -> list[dict]:
    """创建/编辑看板时展示的可选数据源清单(内置 + 本项目自定义)。"""
    items = [{"id": BUILTIN_SOURCE_ID, "name": "项目任务",
              "description": "本项目的平台原生 Task",
              "status_values": [dict(c) for c in BUILTIN_STATUS_VALUES],
              "custom": False}]
    if store is not None and project_id:
        for record in store.list_board_datasources(project_id):
            short_id = record.id.removeprefix(f"{project_id}:")
            items.append({
                "id": short_id,
                "name": record.name or record.id,
                "description": record.description,
                "status_values": [dict(c) for c in record.status_values],
                "custom": True,
                "cards": len(store.list_tasks(project_id, source_id=short_id)),
                "updated_at": record.updated_at,
            })
    return items


def default_filters(status_values) -> list[dict]:
    """按数据源状态取值生成默认筛选列:一列一个状态标签表达式。"""
    return [{"title": str(c.get("value") or ""),
             "query": f"{STATUS_PROPERTY}: {c.get('value')}",
             "color": str(c.get("color") or "")}
            for c in status_values if str(c.get("value") or "").strip()]


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
    """校验数据源状态取值声明;兼容旧状态列 {key,title,color} 写法。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("columns 必须是非空数组")
    if len(raw) > MAX_SOURCE_STATUS_VALUES:
        raise ValueError(f"状态取值最多 {MAX_SOURCE_STATUS_VALUES} 个")
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("columns 每项必须是 {value,color}(或旧版 {key,title,color}) 对象")
        unknown = set(item) - {"value", "key", "title", "color"}
        if unknown:
            raise ValueError(f"状态取值含未知字段: {', '.join(sorted(unknown))}")
        _clean_tag_text(item.get("value") or item.get("title")
                        or item.get("key"), "状态取值")
    values = normalize_status_values(raw)
    if not values:
        raise ValueError("columns 必须声明至少一个状态取值")
    return values


def _column_key_map(raw_columns) -> dict[str, str]:
    """旧脚本卡片用状态列 key 表示状态;建 key -> 状态文本 的映射。

    没有显式列声明时,旧内置四态 key(open/in_progress/...)仍按惯例映射。
    """
    mapping = {}
    for item in raw_columns or []:
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            text = str(item.get("value") or item.get("title") or key).strip()
            if key and text:
                mapping[key] = text
    return mapping or dict(LEGACY_STATUS_TEXT)


def validate_source_cards(raw) -> list[dict]:
    """校验并归一化同步卡片;非法抛 ValueError。状态文本不做取值校验。"""
    if not isinstance(raw, list):
        raise ValueError("cards 必须是数组")
    if len(raw) > MAX_SOURCE_CARDS:
        raise ValueError(f"卡片最多 {MAX_SOURCE_CARDS} 张")
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
        labels_raw = item.get("labels") or []
        if (not isinstance(labels_raw, list)
                or not all(isinstance(x, str) for x in labels_raw)):
            raise ValueError(f"卡片 {card_id} 的 labels 必须是字符串数组")
        meta_raw = item.get("meta") or []
        if not isinstance(meta_raw, list):
            raise ValueError(f"卡片 {card_id} 的 meta 必须是数组")
        cards.append({
            "id": card_id, "title": title,
            "summary": str(item.get("summary") or ""),
            "status": str(item.get("status") or "").strip(),
            "labels": normalize_labels(labels_raw),
            "updated_at": float(item.get("updated_at") or time.time()),
            "meta": [str(x) for x in meta_raw],
            "url": str(item.get("url") or "").strip(),
        })
    return cards


def sync_source_tasks(store, project_id: str, source_id: str,
                      cards: list[dict], raw_columns=None,
                      status_values=None, auto_process=None) -> dict:
    """把同步卡片 upsert 成该源的任务(整体同步语义)。

    卡片按 external_id 匹配既有任务:命中则整卡覆盖(标签含状态,最后写入
    者生效,本地简报与频道绑定保留);未命中则新建,新任务会走项目自动处理
    规则(auto_process 回调);本次未出现的 external_id 任务连带简报删除。
    """
    key_text = _column_key_map(raw_columns)
    declared = [str(c.get("value") or "") for c in status_values or []]
    default_status = (next((str(item.get("value") or item.get("title")
                                or item.get("key") or "")
                            for item in raw_columns or []
                            if isinstance(item, dict)), "")
                      or (declared[0] if declared else "待处理"))
    existing = {task.external_id: task
                for task in store.list_tasks(project_id, source_id=source_id)
                if task.external_id}
    created, updated = 0, 0
    seen_ids = set()
    for card in cards:
        external_id = card["id"]
        seen_ids.add(external_id)
        status_raw = card["status"]
        status_text = key_text.get(status_raw, status_raw) or default_status
        labels = with_status(card["labels"], status_text) \
            if status_text or not status_of(card["labels"]) else card["labels"]
        task = existing.get(external_id)
        if task is None:
            task = Task(
                id=new_id("t"), project_id=project_id, title=card["title"],
                source_id=source_id, external_id=external_id,
                summary=card["summary"], labels=labels,
                url=card["url"], meta=card["meta"],
                created_at=card["updated_at"], updated_at=card["updated_at"],
            )
            store.put_task(task)
            created += 1
            if auto_process is not None:
                auto_process(task)
        else:
            task.title = card["title"]
            task.summary = card["summary"]
            task.labels = labels
            task.url = card["url"]
            task.meta = card["meta"]
            store.put_task(task)
            updated += 1
    removed = 0
    for external_id, task in existing.items():
        if external_id not in seen_ids:
            store.delete_task(task.id)
            removed += 1
    return {"created": created, "updated": updated, "removed": removed}


def _task_card(task: Task) -> dict:
    card = {
        "id": task.id, "title": task.title, "summary": task.summary,
        "status": status_of(task.labels), "labels": list(task.labels),
        "updated_at": task.updated_at, "meta": list(task.meta),
        "task_id": task.id,
    }
    if task.url:
        card["url"] = task.url
    return card


def _builtin_meta(store, task: Task) -> list[str]:
    meta = [task.id]
    for channel_id in task.channel_ids:
        channel = store.get_channel(channel_id)
        meta.append(f"#{channel.name or channel.id}" if channel else channel_id)
    return meta


def _group_columns(tasks: list[Task], prop: str,
                   status_values: list[dict]) -> list[dict]:
    """按属性取值动态分列;status 属性按数据源声明的顺序与颜色排前。"""
    prop = str(prop or "").strip()
    declared = ([{"value": c["value"], "color": c.get("color") or ""}
                 for c in status_values]
                if prop.lower() == STATUS_PROPERTY else [])
    seen = {item["value"].lower() for item in declared}
    extras = []
    for task in tasks:
        for label in task.labels:
            label_prop, value = label_query.split_label(label)
            if label_prop.lower() == prop.lower() and value.lower() not in seen:
                seen.add(value.lower())
                extras.append({"value": value, "color": ""})
    extras.sort(key=lambda item: item["value"])
    columns = [{"title": item["value"], "query": f"{prop}: {item['value']}",
                "color": item["color"]} for item in declared + extras]
    columns.append({"title": f"未设置 {prop}", "query": f"!{prop}: *",
                    "color": ""})
    return columns


def resolve_board_data(store, project_id: str, source_id: str,
                       filters: Optional[list] = None,
                       group_by: str = "") -> dict:
    """解析看板数据:按源取任务 -> 按筛选列或分组属性分列。

    卡片可命中多列(列是标签视角,不是互斥状态);表达式非法抛 ValueError。
    """
    source_id = normalize_source_id(source_id)
    if source_id == BUILTIN_SOURCE_ID:
        source_info = {"id": source_id, "name": "项目任务"}
        status_values = [dict(c) for c in BUILTIN_STATUS_VALUES]
    else:
        record = get_custom_source(store, project_id, source_id)
        if record is None:
            raise ValueError(f"未知数据源: {source_id}")
        source_info = {
            "id": source_id,
            "name": record.name or source_id,
            "updated_at": record.updated_at,
        }
        status_values = [dict(c) for c in record.status_values]
    tasks = store.list_tasks(project_id, include_archived=False,
                             source_id=source_id)
    if source_id == BUILTIN_SOURCE_ID:
        for task in tasks:
            if not task.meta:
                task.meta = _builtin_meta(store, task)

    if str(group_by or "").strip():
        column_filters = _group_columns(tasks, group_by, status_values)
    else:
        column_filters = (validate_filters(filters) if filters
                          else default_filters(status_values))
    out_columns = []
    for index, item in enumerate(column_filters):
        ast = label_query.parse(item["query"])
        out_columns.append({
            "key": f"f{index}", "title": item["title"], "query": item["query"],
            "color": item["color"] or "var(--muted)",
            "cards": [_task_card(task) for task in tasks
                      if label_query.matches(ast, task.labels)],
        })
    # 可筛选标签全集,供前端做筛选输入建议
    labels = {label for task in tasks for label in task.labels}
    labels.update(f"{STATUS_PROPERTY}: {c['value']}" for c in status_values)
    labels.discard("")
    return {"source": source_info,
            "columns": out_columns,
            "labels": sorted(labels),
            "filters": column_filters if filters and not group_by else [],
            "group_by": str(group_by or "").strip()}
