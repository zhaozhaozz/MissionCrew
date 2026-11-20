"""运行过程事件的写缓冲:Runtime 流式输出只进内存,后台线程按批落库。

Runtime 的管道读线程每收到一个增量就 emit 一次。逐条同步落库意味着读线程要
排队等数据库锁、等提交刷盘,慢下来时 CLI 的输出积压在管道里,进程退出后还没
读完的尾部(往往正是最终回复和结果事件)就丢了。这里让 emit 只做内存追加:

- 同一运行连续的同类非结构化事件就地合并,一批只落一行一次提交;
- 后台线程每 ``interval`` 秒把积压交给 Store 写一次,空闲即退出,不常驻;
- Store 在读事件、改运行状态之前先排空缓冲,调用方仍能读到自己写过的内容。

真正的 SQL 与"运行已结束则丢弃迟到事件"的判断都在 Store 的写入回调里,
本模块只管攒批与触发。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class PendingRunEvent:
    run_id: int
    kind: str
    created_at: float          # 本条第一个片段到达的时间,落成新行时用它
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.parts)


class RunEventBuffer:
    def __init__(self, drain: Callable[[], None],
                 mergeable: Callable[[str], bool], interval: float = 0.25):
        """``drain`` 由 Store 提供:持数据库锁调用 ``take()`` 并写入,保证批次之间
        不会乱序;``mergeable`` 判断某类事件能否与相邻同类事件拼接。"""
        self._drain = drain
        self._mergeable = mergeable
        self._interval = interval
        self._lock = threading.Lock()
        self._pending: list[PendingRunEvent] = []
        self._tail: dict[int, PendingRunEvent] = {}   # run_id -> 该运行最后一条待写事件
        self._flusher: Optional[threading.Thread] = None

    def add(self, run_id: int, kind: str, text: str) -> None:
        with self._lock:
            last = self._tail.get(run_id)
            if last is not None and last.kind == kind and self._mergeable(kind):
                last.parts.append(text)
            else:
                entry = PendingRunEvent(run_id, kind, time.time(), [text])
                self._pending.append(entry)
                self._tail[run_id] = entry
            if self._flusher is None:
                self._flusher = threading.Thread(
                    target=self._run, name="run-event-flusher", daemon=True)
                self._flusher.start()

    def take(self) -> list[PendingRunEvent]:
        """取走全部待写事件(按到达顺序)。只应在 Store 的数据库锁内调用。"""
        with self._lock:
            batch, self._pending, self._tail = self._pending, [], {}
        return batch

    def _run(self) -> None:
        while True:
            time.sleep(self._interval)
            try:
                self._drain()
            except Exception:
                # 过程事件是尽力而为的日志,写失败只能丢弃这一批,不能让线程死掉
                logger.exception("运行过程事件批量落库失败")
            # 退出判断与 add() 的启动判断在同一把锁里,不会漏掉刚追加的事件
            with self._lock:
                if not self._pending:
                    self._flusher = None
                    return
