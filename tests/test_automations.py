"""自动化脚本:cron 解析、脚本执行、专属 token 与统一调度入口。"""
from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.agent_tools import AgentToolError
from missioncrew.collab.automations import (AutomationScheduler,
                                            AutomationService,
                                            delete_automation, save_automation)
from missioncrew.collab.chat import ChatEngine
from missioncrew.core.cron import next_cron_time, parse_cron, validate_cron


def _ts(*args) -> float:
    return datetime(*args).timestamp()


# ---- cron 解析与下次触发 ----

def test_cron_parse_supports_standard_syntax():
    minutes, hours, days, months, weekdays = parse_cron("*/15 9-18 1,15 * 1-5")
    assert minutes == frozenset({0, 15, 30, 45})
    assert hours == frozenset(range(9, 19))
    assert days == frozenset({1, 15})
    assert months == frozenset(range(1, 13))
    assert weekdays == frozenset({1, 2, 3, 4, 5})
    # 7 与 0 都是周日
    assert 0 in parse_cron("0 0 * * 7")[4]


@pytest.mark.parametrize("expr", [
    "", "* * * *", "60 * * * *", "* 24 * * *", "a * * * *",
    "5-1 * * * *", "* * 0 * *", "* * * 13 *",
])
def test_cron_rejects_invalid_expressions(expr):
    with pytest.raises(ValueError):
        validate_cron(expr)


def test_next_cron_time_daily_and_minute():
    base = _ts(2026, 8, 14, 10, 30)                      # 周五
    assert next_cron_time("0 3 * * *", base) == _ts(2026, 8, 15, 3, 0)
    assert next_cron_time("* * * * *", base) == _ts(2026, 8, 14, 10, 31)
    # 周约束:下一个周一
    assert next_cron_time("0 9 * * 1", base) == _ts(2026, 8, 17, 9, 0)
    # 日与周同时受限按"或":15 号(周六)先于下周一
    assert next_cron_time("0 9 15 * 1", base) == _ts(2026, 8, 15, 9, 0)


# ---- 定义维护 ----

def test_save_automation_validates_and_merges(seeded):
    automation, created = save_automation(
        seeded, "webshop", id="daily-report", name="日报",
        script="echo hi", cron="0 9 * * *")
    assert created and automation.id == "webshop:daily-report"
    assert automation.enabled and automation.cron == "0 9 * * *"

    updated, created = save_automation(
        seeded, "webshop", id="daily-report", cron="", enabled=False)
    assert not created
    assert updated.script == "echo hi"      # 未提供字段保留现值
    assert updated.cron == "" and not updated.enabled

    with pytest.raises(ValueError, match="cron"):
        save_automation(seeded, "webshop", id="bad", script="x", cron="not cron")
    with pytest.raises(ValueError, match="未知动作"):
        save_automation(seeded, "webshop", id="bad", script="x",
                        actions=["task.create", "no.such"])
    with pytest.raises(ValueError, match="未知动作"):
        save_automation(seeded, "webshop", id="chat-only", script="x",
                        actions=["channel.runs.list", "channel.run.stop"])
    with pytest.raises(ValueError, match="script 不能为空"):
        save_automation(seeded, "webshop", id="empty")


# ---- 脚本执行(子进程 + 运行记录) ----

def _wait_run(store, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = store.get_automation_run(run_id)
        if row and row["status"] != "running":
            return row
        time.sleep(0.05)
    raise AssertionError("automation run did not finish in time")


def test_automation_runs_script_and_records_output(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    automation, _ = save_automation(
        seeded, "webshop", id="hello",
        script='echo "hello $MISSIONCREW_PROJECT_ID"; echo oops >&2')
    run_id = service.trigger(automation)
    row = _wait_run(seeded, run_id)
    assert row["status"] == "succeeded" and row["exit_code"] == 0
    assert "hello webshop" in row["stdout"]
    assert "oops" in row["stderr"]
    refreshed = seeded.get_automation(automation.id)
    assert refreshed.last_status == "succeeded" and refreshed.last_run_at > 0
    # 运行结束后一次性 token 已撤销
    rows = seeded._query(
        "SELECT revoked_at FROM agent_tokens WHERE kind='automation'")
    assert rows and all(row["revoked_at"] is not None for row in rows)


def test_automation_failure_and_shebang(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    failing, _ = save_automation(
        seeded, "webshop", id="failing", script="exit 3")
    row = _wait_run(seeded, service.trigger(failing))
    assert row["status"] == "failed" and row["exit_code"] == 3

    python_script, _ = save_automation(
        seeded, "webshop", id="pyscript",
        script="#!/usr/bin/env python3\nprint('from python')\n")
    row = _wait_run(seeded, service.trigger(python_script))
    assert row["status"] == "succeeded"
    assert "from python" in row["stdout"]


# ---- 脚本专属 token 与平台动作 ----

def test_automation_token_scopes_and_message_publish(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    tools = chat.agent_tools
    automation, _ = save_automation(
        seeded, "webshop", id="poster", script="true",
        actions=["message.publish", "task.create"])
    token = tools.issue_automation_token(automation, 600)
    identity = tools.authenticate(token)
    assert identity.is_automation and identity.actor == "automation:webshop:poster"

    # 白名单外动作拒绝
    with pytest.raises(AgentToolError) as denied:
        tools.execute(identity, "dashboard.save",
                      {"id": "x", "name": "x"}, None, "req-denied")
    assert denied.value.code == "permission_denied"

    # 无提及:只发消息,不触发任何角色
    result = tools.execute(identity, "message.publish",
                           {"channel": "general", "content": "巡检正常"},
                           None, "req-msg")
    message = seeded.get_message(result["message_id"])
    assert message["author_type"] == "automation"
    assert message["author"] == automation.id
    assert seeded._query("SELECT * FROM chat_runs") == []

    # 显式提及单个角色:直接派发该角色,结果不再交回主控
    result = tools.execute(identity, "message.publish",
                           {"channel": "general", "content": "请检查库存",
                            "mentions": ["dev"]},
                           None, "req-dispatch")
    chat.wait_idle()
    runs = seeded._query("SELECT role_id FROM chat_runs ORDER BY id")
    assert [row["role_id"] for row in runs] == ["dev"]
    assert result["dispatched"] == ["dev"]


def test_automation_refreshes_custom_board_source_and_board_binds_it(seeded):
    """脚本身份创建/整体刷新自定义数据源,看板绑定后按状态标签分列筛选。"""
    chat = ChatEngine(seeded, max_workers=2)
    tools = chat.agent_tools
    automation, _ = save_automation(
        seeded, "webshop", id="issue-sync", script="true",
        actions=["board_source.save", "board_source.delete"])
    token = tools.issue_automation_token(automation, 600)
    identity = tools.authenticate(token)

    result = tools.execute(identity, "board_source.save", {
        "id": "gitcode-issues", "name": "GitCode Issues",
        "description": "定时同步的 Issue 列表",
        "cards": [
            {"id": "42", "title": "登录页崩溃", "status": "in_progress",
             "labels": ["bug"], "url": "https://gitcode.com/x/y/issues/42"},
            {"id": "43", "title": "支持导出", "labels": ["feature"]},
        ],
    }, None, "req-src")
    assert "GitCode Issues" in result["summary"]
    record = seeded.get_board_datasource("webshop:gitcode-issues")
    assert record is not None and len(record.cards) == 2
    assert record.cards[1]["status"] == "open"   # 缺省落到第一个状态列

    with TestClient(create_app()) as client:
        # 数据源清单包含自定义源;看板可直接绑定
        sources = client.get("/api/projects/webshop/board_sources").json()
        custom = next(s for s in sources if s["id"] == "gitcode-issues")
        assert custom["custom"] is True and custom["cards"] == 2

        saved = client.post("/api/projects/webshop/boards", json={
            "id": "issues", "name": "Issue 看板", "kind": "taskboard",
            "source": "gitcode-issues", "layout": []})
        assert saved.status_code == 200, saved.text
        data = client.get("/api/projects/webshop/boards/issues/data").json()
        assert data["source"]["name"] == "GitCode Issues"
        assert data["source"]["updated_at"] == record.updated_at
        by_title = {c["title"]: [x["title"] for x in c["cards"]]
                    for c in data["columns"]}
        assert by_title["处理中"] == ["登录页崩溃"]
        assert by_title["待处理"] == ["支持导出"]
        assert data["columns"][1]["cards"][0]["url"].endswith("/issues/42")

        # 卡片校验:未知状态拒绝,数据源内容不被破坏
        with pytest.raises(AgentToolError) as bad:
            tools.execute(identity, "board_source.save", {
                "id": "gitcode-issues",
                "cards": [{"id": "1", "title": "x", "status": "closed"}],
            }, None, "req-bad")
        assert bad.value.code == "invalid_arguments"
        assert len(seeded.get_board_datasource("webshop:gitcode-issues").cards) == 2

        # 仍被面板引用时删除拒绝;面板移除后可删除
        with pytest.raises(AgentToolError) as in_use:
            tools.execute(identity, "board_source.delete",
                          {"id": "gitcode-issues"}, None, "req-del-1")
        assert in_use.value.code == "in_use"
        client.delete("/api/projects/webshop/boards/issues")
        result = tools.execute(identity, "board_source.delete",
                               {"id": "gitcode-issues"}, None, "req-del-2")
        assert result["deleted"] is True
        assert seeded.get_board_datasource("webshop:gitcode-issues") is None


def test_orchestrator_creates_taskboard_bound_to_custom_source(seeded):
    """主控经 dashboard.save 传 kind/source/filters 创建任务看板。"""
    chat = ChatEngine(seeded, max_workers=2)
    tools = chat.agent_tools
    project = seeded.get_project("webshop")
    channel = seeded.get_channel("general")
    from missioncrew.collab.agent_tools import AgentIdentity, AgentRunContext
    identity = AgentIdentity(
        token_id="t", project_id="webshop", channel_id=channel.id,
        role_id=project.orchestrator_role_id,
        issued_scopes=tuple(tools.allowed_actions(
            project, project.orchestrator_role_id)))
    context = AgentRunContext(run_id=0, channel_id=channel.id, root_id=0, depth=0)

    tools._execute_action(identity, "board_source.save", {
        "id": "sync-list", "name": "同步列表",
        "cards": [{"id": "a", "title": "条目A", "labels": ["p0"]}],
    }, context)
    # 内置源 id 不可覆盖
    with pytest.raises(AgentToolError):
        tools._execute_action(identity, "board_source.save",
                              {"id": "tasks", "name": "x"}, context)

    result = tools._execute_action(identity, "dashboard.save", {
        "id": "sync-board", "name": "同步看板", "kind": "taskboard",
        "source": "sync-list",
        "filters": [{"title": "P0", "query": "p0 & !已完成"}],
    }, context)
    board = seeded.get_board("webshop:sync-board")
    assert board.kind == "taskboard" and board.source == "sync-list"
    assert board.filters == [{"title": "P0", "query": "p0 & !已完成",
                              "color": ""}]
    assert "同步看板" in result["summary"]
    # 未知数据源拒绝
    with pytest.raises(AgentToolError):
        tools._execute_action(identity, "dashboard.save", {
            "id": "sync-board", "source": "nope"}, context)


def test_automation_token_rejected_after_delete(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    tools = chat.agent_tools
    automation, _ = save_automation(
        seeded, "webshop", id="gone", script="true")
    token = tools.issue_automation_token(automation, 600)
    delete_automation(seeded, tools, "webshop", "gone")
    with pytest.raises(AgentToolError):
        tools.authenticate(token)


def test_orchestrator_can_save_automation_via_action(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    tools = chat.agent_tools
    project = seeded.get_project("webshop")
    channel = seeded.get_channel("general")
    from missioncrew.collab.agent_tools import AgentIdentity, AgentRunContext
    identity = AgentIdentity(
        token_id="t", project_id="webshop", channel_id=channel.id,
        role_id=project.orchestrator_role_id,
        issued_scopes=tuple(tools.allowed_actions(
            project, project.orchestrator_role_id)))
    context = AgentRunContext(run_id=0, channel_id=channel.id, root_id=0, depth=0)
    result = tools._execute_action(identity, "automation.save", {
        "id": "board-sync", "name": "面板同步",
        "script": "echo sync", "cron": "*/30 * * * *",
    }, context)
    assert "面板同步" in result["summary"]
    assert seeded.get_automation("webshop:board-sync") is not None
    result = tools._execute_action(
        identity, "automation.delete", {"id": "board-sync"}, context)
    assert result["deleted"] is True


# ---- REST API 与统一调度入口 ----

def test_automation_api_crud_and_manual_run(seeded):
    with TestClient(create_app()) as client:
        saved = client.post("/api/projects/webshop/automations", json={
            "id": "report", "name": "巡检", "script": "echo ok",
            "cron": "0 8 * * *",
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["next_run_at"] is not None

        listed = client.get("/api/projects/webshop/automations").json()
        assert [a["id"] for a in listed["automations"]] == ["webshop:report"]
        assert client.get("/api/overview").json()["automations"]

        run = client.post("/api/projects/webshop/automations/report/run")
        assert run.status_code == 200, run.text
        run_id = run.json()["run_id"]
        detail = {"status": "running"}
        deadline = time.time() + 10
        while time.time() < deadline:
            detail = client.get(
                f"/api/projects/webshop/automations/report/runs/{run_id}").json()
            if detail["status"] != "running":
                break
            time.sleep(0.05)
        assert detail["status"] == "succeeded"
        assert "ok" in detail["stdout"]

        runs = client.get(
            "/api/projects/webshop/automations/report/runs").json()["runs"]
        assert runs and runs[0]["id"] == run_id

        deleted = client.delete("/api/projects/webshop/automations/report")
        assert deleted.json()["deleted"] is True
        assert client.get(
            "/api/projects/webshop/automations").json()["automations"] == []


def test_scheduler_fires_due_automation_and_reschedules(seeded):
    fired = []

    class _FakeService:
        def trigger(self, automation, trigger="manual"):
            fired.append((automation.id, trigger))
            return 1

        def shutdown(self):
            pass

    automation, _ = save_automation(
        seeded, "webshop", id="tick", script="true", cron="* * * * *")
    scheduler = AutomationScheduler(seeded, _FakeService())
    now = time.time()
    scheduler._tick(now)                     # 首次遇见:只登记下一次触发
    assert not fired
    key = f"automation:{automation.id}"
    cron, nxt = scheduler._next[key]
    assert nxt > now
    scheduler._next[key] = (cron, now - 1)   # 模拟到期
    scheduler._tick(now)
    assert fired == [(automation.id, "cron")]
    assert scheduler._next[key][1] > now     # 已重排下一次

    # 停用后从调度表中移除
    save_automation(seeded, "webshop", id="tick", enabled=False)
    scheduler._tick(time.time())
    assert key not in scheduler._next


def test_scheduler_runs_system_jobs(seeded):
    calls = []

    class _FakeService:
        def trigger(self, automation, trigger="manual"):
            return 1

        def shutdown(self):
            pass

    scheduler = AutomationScheduler(seeded, _FakeService())
    scheduler.register_system_job("cleanup", "0 4 * * *", lambda: calls.append(1))
    now = time.time()
    scheduler._tick(now)
    scheduler._next["system:cleanup"] = ("0 4 * * *", now - 1)
    scheduler._tick(now)
    assert calls == [1]
