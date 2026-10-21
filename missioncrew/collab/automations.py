"""自动化脚本:统一定时任务入口、脚本子进程执行与专属 token。

脚本由主控(automation.save)或人类(Web/API)维护,内容持久化在平台数据库;
执行时落盘到平台数据目录并以子进程运行,通过一次性签发的 automation token
调用 Agent Tool API,动作范围受脚本自身的 actions 白名单约束。触发来源有
cron、webhook(见 webhooks.py)与手动三种,经 MISSIONCREW_TRIGGER 告诉脚本。
"""
from __future__ import annotations

import json
import subprocess
import sys
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from .agent_tools import (AUTOMATION_ACTIONS, AgentActionService,
                          CONTROL_ID_RE, default_agent_tool_url)
from ..core.config import mc_home
from ..core.cron import next_cron_time, validate_cron
from ..core.models import Automation
from ..core.store import Store
from .webhooks import new_webhook_secret

# 单次运行 stdout/stderr 各自的持久化上限;超出部分保留尾部(更接近失败现场)
AUTOMATION_OUTPUT_LIMIT = 20000
# 运行记录按时间保留(每个脚本最近一条除外),不限条数
AUTOMATION_RUNS_RETENTION_SECONDS = 7 * 24 * 3600
# 脚本运行期间到达的 webhook 事件在内存里排队的上限;服务重启时丢弃
WEBHOOK_QUEUE_LIMIT = 20
# webhook 事件文件名,位于脚本工作目录,运行结束即删除
WEBHOOK_EVENT_FILE = ".webhook-event.json"


def _clip_output(text: str) -> str:
    if len(text) <= AUTOMATION_OUTPUT_LIMIT:
        return text
    return ("…(输出过长,已截断,仅保留末尾)\n"
            + text[-AUTOMATION_OUTPUT_LIMIT:])


def normalize_automation_id(project_id: str, raw_id: str) -> str:
    short = str(raw_id or "").strip().removeprefix(f"{project_id}:")
    if not CONTROL_ID_RE.fullmatch(short):
        raise ValueError("自动化脚本 id 只能包含字母、数字、下划线、连字符")
    return f"{project_id}:{short}"


def save_automation(store: Store, project_id: str, *, id: str,
                    name: Optional[str] = None,
                    description: Optional[str] = None,
                    script: Optional[str] = None,
                    cron: Optional[str] = None,
                    enabled: Optional[bool] = None,
                    webhook: Optional[bool] = None,
                    actions: Optional[list] = None,
                    timeout_seconds: Optional[int] = None,
                    actor: str = "human",
                    created_by_role_id: str = "") -> tuple[Automation, bool]:
    """新建或按字段合并更新自动化脚本;返回 (automation, 是否新建)。"""
    if store.get_project(project_id) is None:
        raise ValueError("项目不存在")
    full_id = normalize_automation_id(project_id, id)
    existing = store.get_automation(full_id)
    if existing is not None and existing.project_id != project_id:
        raise ValueError("自动化脚本不属于当前项目")
    created = existing is None
    automation = existing or Automation(
        id=full_id, project_id=project_id,
        created_by_role_id=created_by_role_id)

    if name is not None:
        automation.name = str(name).strip()
    if not automation.name:
        automation.name = full_id.removeprefix(f"{project_id}:")
    if description is not None:
        automation.description = str(description)
    if script is not None:
        if not isinstance(script, str):
            raise ValueError("script 必须是字符串")
        automation.script = script
    if cron is not None:
        cron_text = str(cron).strip()
        if cron_text:
            validate_cron(cron_text)
        automation.cron = cron_text
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        automation.enabled = enabled
    if webhook is not None:
        if not isinstance(webhook, bool):
            raise ValueError("webhook 必须是布尔值")
        automation.webhook = webhook
        # 首次开启时生成密钥;关闭后保留,重新开启时外部系统的配置仍然有效
        if webhook and not automation.webhook_secret:
            automation.webhook_secret = new_webhook_secret()
    if actions is not None:
        if (not isinstance(actions, list)
                or not all(isinstance(item, str) for item in actions)):
            raise ValueError("actions 必须是动作名数组")
        unknown = sorted(set(actions) - AUTOMATION_ACTIONS)
        if unknown:
            raise ValueError(f"actions 含未知动作: {', '.join(unknown)}")
        automation.actions = list(dict.fromkeys(actions))
    if timeout_seconds is not None:
        automation.timeout_seconds = timeout_seconds
    if not automation.script.strip():
        raise ValueError("script 不能为空")
    # 触发 __post_init__ 的取值校验(timeout 等)
    automation = Automation.from_dict(automation.to_dict())
    store.put_automation(automation)
    store.audit(actor, "automation_saved",
                detail=f"project={project_id} automation={automation.id} "
                       f"cron={automation.cron or '(manual)'} "
                       f"enabled={automation.enabled} webhook={automation.webhook}")
    return automation, created


def rotate_webhook_secret(store: Store, automation: Automation,
                          actor: str = "human") -> Automation:
    """换一个新密钥;外部系统要同步改成新值,旧密钥签名的投递随即被拒。"""
    automation.webhook_secret = new_webhook_secret()
    store.put_automation(automation)
    store.audit(actor, "automation_webhook_secret_rotated",
                detail=f"project={automation.project_id} automation={automation.id}")
    return automation


def delete_automation(store: Store, agent_tools: AgentActionService,
                      project_id: str, automation_id: str,
                      actor: str = "human") -> Automation:
    full_id = normalize_automation_id(project_id, automation_id)
    automation = store.get_automation(full_id)
    if automation is None or automation.project_id != project_id:
        raise ValueError(f"自动化脚本不存在: {automation_id}")
    agent_tools.revoke_automation_tokens(automation.id)
    store.delete_automation(automation.id)
    store.audit(actor, "automation_deleted",
                detail=f"project={project_id} automation={automation.id}")
    return automation


def automation_workdir(automation: Automation) -> Path:
    short = automation.id.removeprefix(f"{automation.project_id}:")
    return mc_home() / "automations" / automation.project_id / short


class AutomationService:
    """脚本运行入口:并发去重、webhook 排队、运行记录、token 签发与回收。

    同一脚本同时只跑一次。cron 与手动触发撞上正在运行时直接拒绝;webhook
    投递不能丢,先在内存里排队(每个脚本最多 WEBHOOK_QUEUE_LIMIT 条),本次
    结束后按到达顺序接着跑,脚本以最新定义为准。
    """

    def __init__(self, store: Store, agent_tools: AgentActionService):
        self.store = store
        self.agent_tools = agent_tools
        self._pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="automation")
        self._running: set[str] = set()
        self._pending: dict[str, deque[dict]] = {}
        self._guard = threading.Lock()

    def running_ids(self) -> set[str]:
        with self._guard:
            return set(self._running)

    def prune_runs(self) -> None:
        """清掉超出保留期的运行记录;每次运行结束和每小时的系统任务都会调用。"""
        self.store.prune_automation_runs(
            time.time() - AUTOMATION_RUNS_RETENTION_SECONDS)

    def trigger(self, automation: Automation, trigger: str = "manual") -> int:
        """启动一次后台运行并返回运行记录 id;同一脚本不并发。"""
        with self._guard:
            if automation.id in self._running:
                raise ValueError("该脚本上一次运行尚未结束")
            self._running.add(automation.id)
        try:
            return self._launch(automation, trigger, None)
        except Exception:
            with self._guard:
                self._running.discard(automation.id)
            raise

    def trigger_webhook(self, automation: Automation, event: dict) -> Optional[int]:
        """webhook 投递触发:空闲时立即运行并返回运行记录 id,运行中则排队返回 None。"""
        with self._guard:
            if automation.id in self._running:
                queue = self._pending.setdefault(automation.id, deque())
                if len(queue) >= WEBHOOK_QUEUE_LIMIT:
                    raise ValueError("该脚本排队的 webhook 事件已满,请稍后重试")
                queue.append(event)
                return None
            self._running.add(automation.id)
        try:
            return self._launch(automation, "webhook", event)
        except Exception:
            with self._guard:
                self._running.discard(automation.id)
            raise

    def _launch(self, automation: Automation, trigger: str,
                event: Optional[dict]) -> int:
        """建运行记录并提交执行;调用方已把脚本登记为运行中。"""
        detail = ""
        if event is not None:
            detail = " ".join(part for part in (
                event.get("provider"), event.get("event"), event.get("delivery")) if part)
        run_id = self.store.start_automation_run(
            automation.id, automation.project_id, trigger, detail)
        self.store.touch_automation_run_state(automation.id, "running")
        self.store.audit(
            "platform", "automation_run_started",
            detail=(f"project={automation.project_id} "
                    f"automation={automation.id} trigger={trigger} "
                    f"run={run_id}" + (f" event={detail}" if detail else "")))
        self._pool.submit(self._execute, automation, run_id, trigger, event)
        return run_id

    def _execute(self, automation: Automation, run_id: int, trigger: str,
                 event: Optional[dict]) -> None:
        try:
            status, exit_code, stdout, stderr, error = self._run_script(
                automation, trigger, event)
        except Exception as exc:   # 平台侧异常也要落进运行记录,不能无声丢失
            status, exit_code, stdout, stderr, error = (
                "failed", None, "", "", f"平台执行脚本失败: {exc}")
        try:
            # 先更新脚本的最近状态再结束运行记录:看到运行已结束的读者
            # (页面、Agent、测试)读到的 last_status 一定已是本次结果
            self.store.touch_automation_run_state(automation.id, status)
            self.store.finish_automation_run(
                run_id, status, exit_code=exit_code,
                stdout=_clip_output(stdout), stderr=_clip_output(stderr),
                error=error)
            self.prune_runs()
            self.store.audit(
                "platform", "automation_run_finished",
                detail=(f"project={automation.project_id} "
                        f"automation={automation.id} run={run_id} "
                        f"status={status} exit={exit_code}"))
        finally:
            self._release(automation.id)

    def _release(self, automation_id: str) -> None:
        """本次运行结束:有排队的 webhook 事件就接着跑下一个,否则解除运行中。"""
        while True:
            with self._guard:
                queue = self._pending.get(automation_id)
                event = queue.popleft() if queue else None
                if queue is not None and not queue:
                    self._pending.pop(automation_id, None)
                if event is None:
                    self._running.discard(automation_id)
                    return
            # 排队期间脚本可能被改、关掉 webhook 或删除:按最新定义决定
            automation = self.store.get_automation(automation_id)
            if automation is None:
                reason = "脚本已删除"
            elif not automation.webhook:
                reason = "webhook 已关闭"
            else:
                try:
                    self._launch(automation, "webhook", event)
                    return
                except Exception as exc:
                    reason = f"启动失败: {exc}"
            self.store.audit(
                "platform", "automation_webhook_dropped",
                detail=f"automation={automation_id} delivery={event.get('delivery', '')} "
                       f"reason={reason}")

    def _run_script(self, automation: Automation, trigger: str = "manual",
                    event: Optional[dict] = None
                    ) -> tuple[str, Optional[int], str, str, str]:
        workdir = automation_workdir(automation)
        workdir.mkdir(parents=True, exist_ok=True)
        script_path = workdir / "script"
        script_path.write_text(automation.script, encoding="utf-8")
        script_path.chmod(0o700)
        event_file = workdir / WEBHOOK_EVENT_FILE
        event_file.unlink(missing_ok=True)   # 不让上一次的事件残留给非 webhook 运行
        if event is not None:
            event_file.touch(mode=0o600)
            event_file.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")

        token = self.agent_tools.issue_automation_token(
            automation, automation.timeout_seconds + 300)
        token_file = workdir / ".agent-tool-token"
        token_file.touch(mode=0o600, exist_ok=True)
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)

        env = dict(os.environ)
        env.update({
            "MISSIONCREW_AGENT_TOOL_URL": default_agent_tool_url(),
            "MISSIONCREW_AGENT_TOKEN_FILE": str(token_file),
            "MISSIONCREW_AGENT_TOOL_PYTHON": sys.executable,
            "MISSIONCREW_PROJECT_ID": automation.project_id,
            "MISSIONCREW_AUTOMATION_ID": automation.id,
            "MISSIONCREW_TRIGGER": trigger,
        })
        for key in ("MISSIONCREW_WEBHOOK_EVENT_FILE", "MISSIONCREW_WEBHOOK_PROVIDER",
                    "MISSIONCREW_WEBHOOK_EVENT", "MISSIONCREW_WEBHOOK_DELIVERY"):
            env.pop(key, None)
        if event is not None:
            env.update({
                "MISSIONCREW_WEBHOOK_EVENT_FILE": str(event_file),
                "MISSIONCREW_WEBHOOK_PROVIDER": str(event.get("provider") or ""),
                "MISSIONCREW_WEBHOOK_EVENT": str(event.get("event") or ""),
                "MISSIONCREW_WEBHOOK_DELIVERY": str(event.get("delivery") or ""),
            })
        # 有 shebang 时按可执行文件运行(支持 python 等),否则统一交给 bash
        command = ([str(script_path)] if automation.script.startswith("#!")
                   else ["bash", str(script_path)])
        try:
            completed = subprocess.run(
                command, cwd=workdir, env=env, capture_output=True,
                text=True, timeout=automation.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            return ("timeout", None,
                    self._expired_text(exc.stdout), self._expired_text(exc.stderr),
                    f"脚本超过 {automation.timeout_seconds}s 未结束,已终止")
        finally:
            self.agent_tools.revoke_automation_tokens(automation.id)
            token_file.unlink(missing_ok=True)
            event_file.unlink(missing_ok=True)
        status = "succeeded" if completed.returncode == 0 else "failed"
        error = ("" if completed.returncode == 0
                 else f"脚本退出码 {completed.returncode}")
        return (status, completed.returncode,
                completed.stdout or "", completed.stderr or "", error)

    @staticmethod
    def _expired_text(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value or "")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class AutomationScheduler:
    """统一定时任务入口:所有 cron 触发的周期工作都从这里调度。

    目前调度项目自动化脚本;系统级周期任务可通过 register_system_job 挂入,
    与脚本共用同一线程与 cron 语义。停机期间错过的触发不补跑:服务恢复后
    从当前时间重新计算下一次触发。
    """

    TICK_SECONDS = 20.0

    def __init__(self, store: Store, service: AutomationService):
        self.store = store
        self.service = service
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # key -> (cron, 下一次触发时间);cron 变更即从当前时间重算
        self._next: dict[str, tuple[str, float]] = {}
        self._system_jobs: dict[str, tuple[str, Callable[[], None]]] = {}

    def register_system_job(self, job_id: str, cron: str,
                            fn: Callable[[], None]) -> None:
        validate_cron(cron)
        self._system_jobs[f"system:{job_id}"] = (cron, fn)
        self._wake.set()

    def poke(self) -> None:
        """配置变更后立即重新计算调度,不必等下一个 tick。"""
        self._wake.set()

    def _entries(self) -> list[tuple[str, str, "Automation | Callable[[], None]"]]:
        entries: list[tuple[str, str, Automation | Callable[[], None]]] = []
        for automation in self.store.list_automations():
            if automation.enabled and automation.cron.strip():
                entries.append(
                    (f"automation:{automation.id}", automation.cron, automation))
        for key, (cron, fn) in self._system_jobs.items():
            entries.append((key, cron, fn))
        return entries

    def _tick(self, now: float) -> float:
        entries = self._entries()
        seen: set[str] = set()
        for key, cron, payload in entries:
            seen.add(key)
            known = self._next.get(key)
            if known is None or known[0] != cron:
                try:
                    self._next[key] = (cron, next_cron_time(cron, now))
                except ValueError:
                    self._next[key] = (cron, float("inf"))
                continue
            if known[1] <= now:
                self._fire(key, payload)
                self._next[key] = (cron, next_cron_time(cron, now))
        for key in set(self._next) - seen:
            self._next.pop(key, None)
        upcoming = [item[1] for item in self._next.values()
                    if item[1] != float("inf")]
        if not upcoming:
            return self.TICK_SECONDS
        return max(1.0, min(self.TICK_SECONDS, min(upcoming) - now))

    def _fire(self, key: str,
              payload: "Automation | Callable[[], None]") -> None:
        try:
            if callable(payload):
                payload()
            else:
                self.service.trigger(payload, trigger="cron")
        except ValueError as exc:
            # 上一次运行未结束等业务性跳过,记审计不中断调度
            self.store.audit("platform", "automation_run_skipped",
                             detail=f"job={key} reason={exc}")
        except Exception as exc:
            self.store.audit("platform", "automation_schedule_failed",
                             detail=f"job={key} error={exc}")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                wait_seconds = self._tick(time.time())
            except Exception:
                # 调度线程不能因数据异常退出;下个 tick 重试
                wait_seconds = self.TICK_SECONDS
            self._wake.wait(wait_seconds)
            self._wake.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="automation-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self.service.shutdown()
