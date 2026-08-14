"""五段 crontab 表达式解析与下次触发时间计算(本地时区)。

支持标准五段语法:分 时 日 月 周,每段可用 ``*``、数值、区间 ``a-b``、
列表 ``a,b,c`` 与步长 ``*/n`` / ``a-b/n``。周 0 和 7 都表示周日。
与标准 cron 一致:日与周同时受限时按"或"匹配。
"""
from __future__ import annotations

from datetime import datetime, timedelta

# (下限, 上限, 别名表)
_FIELD_SPECS = (
    (0, 59, {}),                                   # minute
    (0, 23, {}),                                   # hour
    (1, 31, {}),                                   # day of month
    (1, 12, {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
             "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}),
    (0, 7, {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5,
            "sat": 6}),                            # weekday, 7 == 0 == 周日
)


def _parse_value(token: str, low: int, high: int, names: dict) -> int:
    key = token.strip().lower()
    if key in names:
        return names[key]
    try:
        value = int(key)
    except ValueError as exc:
        raise ValueError(f"cron 字段值无效: {token!r}") from exc
    if not low <= value <= high:
        raise ValueError(f"cron 字段值超出范围 {low}-{high}: {token!r}")
    return value


def _parse_field(field: str, low: int, high: int, names: dict) -> frozenset[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"cron 字段为空段: {field!r}")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            step = _parse_value(step_text, 1, high, {})
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")
            start = _parse_value(start_text, low, high, names)
            end = _parse_value(end_text, low, high, names)
            if end < start:
                raise ValueError(f"cron 区间上界小于下界: {field!r}")
        else:
            start = end = _parse_value(part, low, high, names)
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expr: str) -> tuple[frozenset[int], ...]:
    fields = str(expr or "").split()
    if len(fields) != 5:
        raise ValueError("cron 表达式必须是五段: 分 时 日 月 周")
    parsed = tuple(
        _parse_field(field, low, high, names)
        for field, (low, high, names) in zip(fields, _FIELD_SPECS)
    )
    minutes, hours, days, months, weekdays = parsed
    # 7 与 0 都是周日,归一化成 0 便于匹配
    if 7 in weekdays:
        weekdays = frozenset(weekdays - {7} | {0})
    return minutes, hours, days, months, weekdays


def validate_cron(expr: str) -> None:
    parse_cron(expr)


def next_cron_time(expr: str, after: float) -> float:
    """返回严格晚于 ``after`` 的下一次触发时间戳(本地时区)。"""
    parsed = parse_cron(expr)
    raw_fields = str(expr).split()
    day_restricted = raw_fields[2] != "*"
    weekday_restricted = raw_fields[4] != "*"
    moment = datetime.fromtimestamp(after).replace(
        second=0, microsecond=0) + timedelta(minutes=1)
    # 上限四年可覆盖 2 月 29 日等最稀疏的合法组合
    limit = moment + timedelta(days=4 * 366)
    minutes, hours, days, months, weekdays = parsed
    while moment < limit:
        if moment.month not in months:
            # 跳到下个月 1 日 00:00,避免逐分钟扫描
            if moment.month == 12:
                moment = moment.replace(
                    year=moment.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                moment = moment.replace(
                    month=moment.month + 1, day=1, hour=0, minute=0)
            continue
        if not _day_matches(moment, days, weekdays,
                            day_restricted, weekday_restricted):
            moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if moment.hour not in hours:
            moment = (moment + timedelta(hours=1)).replace(minute=0)
            continue
        if moment.minute not in minutes:
            moment = moment + timedelta(minutes=1)
            continue
        return moment.timestamp()
    raise ValueError(f"cron 表达式在可预见范围内不会触发: {expr!r}")


def _day_matches(moment: datetime, days: frozenset[int],
                 weekdays: frozenset[int], day_restricted: bool,
                 weekday_restricted: bool) -> bool:
    cron_weekday = (moment.weekday() + 1) % 7
    day_ok = moment.day in days
    weekday_ok = cron_weekday in weekdays
    if day_restricted and weekday_restricted:
        return day_ok or weekday_ok
    return day_ok and weekday_ok
