"""把 Runtime 账户限额与项目角色启停安全联动。"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RoleUsageLinkage:
    """按角色的显式开关应用限额，只恢复由本机制自动停用的角色。"""

    _FABLE_MARKERS = ("fable", "fabel")

    def __init__(self, store, usage_loader: Callable[..., dict]):
        self.store = store
        self.usage_loader = usage_loader
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._refresh_requested = threading.Event()
        self._thread: threading.Thread | None = None

    def state(self) -> dict:
        blocks = self.store.list_role_usage_blocks()
        linked_roles = [role for role in self.store.list_roles()
                        if role.usage_linkage_enabled]
        deadlines = [float(item["disabled_until"]) for item in blocks
                     if float(item["disabled_until"] or 0) > 0]
        return {
            "linked_role_count": len(linked_roles),
            "auto_disabled_count": len(blocks),
            "next_reset_at": min(deadlines) if deadlines else None,
            "roles": blocks,
        }

    def role_updated(self, role, previous=None) -> None:
        """关闭联动或改绑执行组合时，清除旧限额对角色的控制。"""
        with self._guard:
            block = next((item for item in self.store.list_role_usage_blocks()
                          if item["project_id"] == role.project_id
                          and item["role_id"] == role.id), None)
            binding_changed = bool(previous and (
                previous.runtime_id != role.runtime_id
                or previous.model != role.model))
            if block and (not role.usage_linkage_enabled or binding_changed):
                reason = ("role_linkage_disabled" if not role.usage_linkage_enabled
                          else "role_binding_changed")
                self._restore(block, reason)
        self._wake.set()

    @classmethod
    def _is_fable(cls, value: object) -> bool:
        text = str(value or "").lower()
        return any(marker in text for marker in cls._FABLE_MARKERS)

    @classmethod
    def _blocking_windows(cls, snapshot: dict, role, now: float) -> list[dict]:
        """Fable 使用独立周限额；Claude 的会话窗口仍约束所有模型。"""
        is_claude = str(snapshot.get("adapter") or "") == "claude_code"
        role_is_fable = cls._is_fable(role.model)
        result = []
        for window in snapshot.get("windows") or []:
            try:
                used = float(window.get("used_percent", 0))
                resets_at = float(window.get("resets_at") or 0)
            except (TypeError, ValueError):
                continue
            # 已过重置点的满额快照属于旧周期，不能再次停用刚恢复的角色。
            if used < 100 or (resets_at and resets_at <= now):
                continue
            key_and_label = f"{window.get('key', '')} {window.get('label', '')}".lower()
            fable_window = cls._is_fable(key_and_label)
            general_week = ("week" in key_and_label or "本周" in key_and_label)
            if is_claude and fable_window != role_is_fable and (
                    fable_window or (general_week and role_is_fable)):
                continue
            result.append(window)
        return result

    @staticmethod
    def _deadline(windows: list[dict]) -> float:
        resets = []
        for window in windows:
            try:
                value = float(window.get("resets_at") or 0)
            except (TypeError, ValueError):
                value = 0
            if not value:
                return 0.0
            resets.append(value)
        return max(resets, default=0.0)

    def _restore(self, block: dict, reason: str) -> bool:
        changed = self.store.auto_enable_role_after_usage(
            block["project_id"], block["role_id"])
        if changed:
            self.store.audit(
                "platform", "role_usage_auto_enabled",
                detail=(f"project={block['project_id']} role={block['role_id']} "
                        f"backend={block['backend_id']} reason={reason}"))
        return changed

    def _restore_unlinked(self) -> int:
        linked = {(role.project_id, role.id) for role in self.store.list_roles()
                  if role.usage_linkage_enabled}
        return sum(
            self._restore(block, "role_linkage_disabled")
            for block in self.store.list_role_usage_blocks()
            if (block["project_id"], block["role_id"]) not in linked
        )

    def _restore_due(self, now: float | None = None) -> int:
        current_time = time.time() if now is None else now
        return sum(
            self._restore(block, "reset_reached")
            for block in self.store.list_role_usage_blocks()
            if block["disabled_until"]
            and float(block["disabled_until"]) <= current_time
        )

    def reconcile(self, usage: dict | None = None, now: float | None = None) -> dict:
        """应用一次限额快照；可由后台计时器或 API 刷新共同调用。"""
        with self._guard:
            current_time = time.time() if now is None else now
            blocks = {
                (item["project_id"], item["role_id"]): item
                for item in self.store.list_role_usage_blocks()
            }
            roles = self.store.list_roles()
            role_keys = {(role.project_id, role.id) for role in roles}

            # 删除角色后的残留记录不会保留；正常删除路径也会同步清理。
            for key, block in list(blocks.items()):
                if key not in role_keys:
                    self._restore(block, "role_deleted")
                    blocks.pop(key, None)

            linked_roles = []
            for role in roles:
                key = (role.project_id, role.id)
                if role.usage_linkage_enabled:
                    linked_roles.append(role)
                elif key in blocks:
                    self._restore(blocks.pop(key), "role_linkage_disabled")

            if not linked_roles:
                return self.state()
            if usage is None:
                try:
                    usage = self.usage_loader(refresh=True)
                except Exception:
                    usage = {"usage": []}
            snapshots = {
                str(item.get("backend_id") or ""): item
                for item in usage.get("usage") or []
                if item.get("status") == "ok"
            }

            for role in linked_roles:
                key = (role.project_id, role.id)
                block = blocks.get(key)
                snapshot = snapshots.get(role.runtime_id)
                if snapshot is None:
                    if block and (block["backend_id"] != role.runtime_id or (
                            block["disabled_until"]
                            and float(block["disabled_until"]) <= current_time)):
                        self._restore(block, "reset_reached")
                    continue

                windows = self._blocking_windows(snapshot, role, current_time)
                if not windows:
                    if block:
                        self._restore(block, "usage_available")
                    continue

                window_keys = [str(window.get("key") or window.get("label") or "window")
                               for window in windows]
                changed = self.store.auto_disable_role_for_usage(
                    role.project_id, role.id, role.runtime_id,
                    window_keys, self._deadline(windows))
                if changed:
                    self.store.audit(
                        "platform", "role_usage_auto_disabled",
                        detail=(f"project={role.project_id} role={role.id} "
                                f"backend={role.runtime_id} "
                                f"windows={','.join(window_keys)}"))
            return self.state()

    def _next_delay(self) -> float | None:
        deadlines = [float(item["disabled_until"])
                     for item in self.store.list_role_usage_blocks()
                     if float(item["disabled_until"] or 0) > 0]
        if not deadlines:
            return None
        return max(0.2, min(deadlines) - time.time())

    def request_refresh(self) -> None:
        """Runtime 执行结束时唤醒一次刷新；连续完成事件会被合并。"""
        if not any(role.usage_linkage_enabled for role in self.store.list_roles()):
            return
        self._refresh_requested.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._restore_unlinked()
                self._restore_due()
                if self._refresh_requested.is_set():
                    self._refresh_requested.clear()
                    self.reconcile()
            except Exception:
                # 后台联动不能影响主服务；下次任务完成或页面刷新时重试。
                pass
            self._wake.wait(self._next_delay())
            self._wake.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._refresh_requested.clear()
        self._thread = threading.Thread(
            target=self._run, name="role-usage-linkage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
