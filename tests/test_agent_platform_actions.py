"""Agent Tool 维护平台配置:频道生命周期、Task 归档、配置读取与自动化试跑。"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.agent_tools import AgentToolError
from missioncrew.collab.automations import AutomationService, save_automation
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.guidelines import save_guideline
from missioncrew.collab.skills import save_project_skill_markdown
from missioncrew.collab.tasks import create_task
from missioncrew.core.models import (Board, BoardDataSource, BoardWidget, Channel,
                                     Task)


def _identity(store, chat: ChatEngine, role_id: str = "lead"):
    """在 general 频道起一个活动 Run,返回 (身份, run_id)。"""
    channel = store.get_channel("general")
    root_id = store.add_message(channel.id, "human", "human", f"@{role_id} 执行", [role_id])
    run_id = store.add_chat_run(channel.id, role_id, root_id, root_id, 0)
    store.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(channel, store.get_role("webshop", role_id),
                            store.get_backend("std-1"), root_id, run_id=run_id)
    token = Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"]).read_text().strip()
    return chat.agent_tools.authenticate(token), run_id


def _call(chat, identity, run_id, action, arguments=None):
    return chat.agent_tools.execute(identity, action, arguments or {}, run_id,
                                    f"{action.replace('.', '-')}-{time.time_ns()}")


def _receipts(store, action):
    return [m for m in store.list_messages("general")
            if m["kind"] == "agent_tool" and f"`{action}`" in m["content"]]


def test_channel_update_changes_fields_within_project_repos(seeded, tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    project = seeded.get_project("webshop")
    project.repos = [str(repo)]
    seeded.put_project(project)
    seeded.put_channel(Channel(id="webshop:hotfix", name="hotfix", project_id="webshop"))
    chat = ChatEngine(seeded)
    identity, run_id = _identity(seeded, chat)

    result = _call(chat, identity, run_id, "channel.update", {
        "id": "hotfix", "name": "结算修复", "purpose": "修结算页",
        "workdir": str(repo / "pkg")})
    channel = seeded.get_channel("webshop:hotfix")
    assert (channel.name, channel.purpose, channel.workdir) == (
        "结算修复", "修结算页", str(repo / "pkg"))
    assert result["changed"] == ["name", "purpose", "workdir"]
    assert "从下一轮执行起生效" in result["summary"]
    assert _receipts(seeded, "channel.update")

    # 不传 id 默认当前频道;workdir 空串改回平台内置工作区
    _call(chat, identity, run_id, "channel.update", {"name": "大厅二号"})
    assert seeded.get_channel("general").name == "大厅二号"
    cleared = _call(chat, identity, run_id, "channel.update",
                    {"id": "hotfix", "workdir": ""})
    assert seeded.get_channel("webshop:hotfix").workdir is None
    assert cleared["changed"] == ["workdir"]

    with pytest.raises(AgentToolError) as outside:
        _call(chat, identity, run_id, "channel.update",
              {"id": "hotfix", "workdir": str(tmp_path)})
    assert outside.value.code == "permission_denied"
    with pytest.raises(AgentToolError) as empty:
        _call(chat, identity, run_id, "channel.update", {"id": "hotfix"})
    assert empty.value.code == "invalid_arguments"


def test_channel_update_refuses_archived_and_page_channels(seeded):
    seeded.put_channel(Channel(id="webshop:old", name="old", project_id="webshop",
                               archived=True))
    seeded.put_channel(Channel(id="webshop:docs-x", name="文档 x", project_id="webshop",
                               content_kind="docs", content_key="x.md"))
    chat = ChatEngine(seeded)
    identity, run_id = _identity(seeded, chat)
    for channel_id, code in (("old", "channel_archived"), ("docs-x", "content_channel")):
        with pytest.raises(AgentToolError) as refused:
            _call(chat, identity, run_id, "channel.update",
                  {"id": channel_id, "name": "改名"})
        assert refused.value.code == code


def test_channel_archive_and_restore_lifecycle(seeded):
    seeded.put_channel(Channel(id="webshop:done", name="done", project_id="webshop"))
    chat = ChatEngine(seeded)
    stopped = []
    chat.agent_tools._stop_channel_sessions = lambda channel_id: stopped.append(channel_id) or 2
    identity, run_id = _identity(seeded, chat)

    archived = _call(chat, identity, run_id, "channel.archive", {"id": "done"})
    assert seeded.get_channel("webshop:done").archived is True
    assert stopped == ["webshop:done"] and archived["stopped_runtimes"] == 2
    listed = _call(chat, identity, run_id, "channel.list", {"scope": "archived"})
    assert [row["id"] for row in listed["channels"]] == ["done"]

    restored = _call(chat, identity, run_id, "channel.restore", {"id": "done"})
    assert seeded.get_channel("webshop:done").archived is False
    assert "已恢复" in restored["summary"]

    # general 不能归档;当前频道有本轮活动运行,同样被拒,不会停掉自己
    for channel_id, code in (("general", "general_channel"),):
        with pytest.raises(AgentToolError) as refused:
            _call(chat, identity, run_id, "channel.archive", {"id": channel_id})
        assert refused.value.code == code
    seeded.put_channel(Channel(id="webshop:busy", name="busy", project_id="webshop"))
    busy_root = seeded.add_message("webshop:busy", "human", "human", "@dev 干活", ["dev"])
    seeded.add_chat_run("webshop:busy", "dev", busy_root, busy_root, 0)
    with pytest.raises(AgentToolError) as busy:
        _call(chat, identity, run_id, "channel.archive", {"id": "busy"})
    assert busy.value.code == "in_use"
    assert stopped == ["webshop:done"]


def test_channel_management_is_orchestrator_only(seeded):
    chat = ChatEngine(seeded)
    identity, run_id = _identity(seeded, chat, "dev")
    actions = chat.agent_tools.capabilities(identity)["actions"]
    for action in ("channel.update", "channel.archive", "channel.restore",
                   "task.archive", "dashboard.list", "automation.run", "guideline.list"):
        assert action not in actions
    with pytest.raises(AgentToolError) as denied:
        _call(chat, identity, run_id, "channel.update", {"name": "x"})
    assert denied.value.code == "permission_denied"


def test_task_archive_and_restore(seeded):
    task = create_task(seeded, "webshop", title="收尾的任务")
    chat = ChatEngine(seeded)
    identity, run_id = _identity(seeded, chat)
    archived = _call(chat, identity, run_id, "task.archive", {"id": task.id})
    assert seeded.get_task(task.id).archived is True
    assert archived["task"]["archived"] is True
    restored = _call(chat, identity, run_id, "task.restore", {"id": task.id})
    assert seeded.get_task(task.id).archived is False and "已恢复" in restored["summary"]
    with pytest.raises(AgentToolError) as missing:
        _call(chat, identity, run_id, "task.archive", {"id": "nope"})
    assert missing.value.code == "task_not_found"


def test_dashboard_and_board_source_lists(seeded):
    seeded.put_board_datasource(BoardDataSource(
        id="webshop:issues", project_id="webshop", name="Issue 同步"))
    seeded.put_task(Task(id="t-issue-1", project_id="webshop", title="外部 issue",
                         source_id="issues"))
    seeded.put_board(Board(id="webshop:issue-board", project_id="webshop", name="Issue 看板",
                           kind="taskboard", source="issues",
                           filters=[{"title": "待处理", "query": "status: 待处理"}]))
    seeded.put_board(Board(id="webshop:report", project_id="webshop", name="周报",
                           layout=[BoardWidget(id="intro", type="markdown", title="说明",
                                               content={"markdown": "# hi"})]))
    chat = ChatEngine(seeded)
    identity, run_id = _identity(seeded, chat)

    listed = _call(chat, identity, run_id, "dashboard.list")
    rows = {row["id"]: row for row in listed["dashboards"]}
    assert rows["issue-board"]["source"] == "issues"
    assert rows["issue-board"]["filters"] == ["待处理"]
    assert rows["report"]["widgets"] == [{"id": "intro", "type": "markdown", "title": "说明"}]
    assert "layout" not in rows["report"]
    full = _call(chat, identity, run_id, "dashboard.list", {"id": "report"})["dashboard"]
    assert full["id"] == "report" and full["layout"][0]["content"] == {"markdown": "# hi"}
    with pytest.raises(AgentToolError) as missing:
        _call(chat, identity, run_id, "dashboard.list", {"id": "ghost"})
    assert missing.value.code == "not_found"

    sources = _call(chat, identity, run_id, "board_source.list")["sources"]
    assert sources == [{**sources[0], "id": "issues", "name": "Issue 同步",
                        "task_count": 1, "dashboards": ["issue-board"]}]
    # 只读清单不在频道里留回执
    assert not _receipts(seeded, "dashboard.list") and not _receipts(seeded, "board_source.list")


def test_guideline_and_skill_lists_include_disabled(seeded):
    project = seeded.get_project("webshop")
    markdown = "---\nname: 发布流程\ndescription: 发布前检查\n---\n\n先跑 CI。\n"
    save_guideline(seeded, project, markdown, enabled=False, actor="human")
    save_project_skill_markdown(
        seeded, seeded.get_project("webshop"), "release-notes",
        "---\nname: 发布说明\ndescription: 写发布说明\n---\n\n按模板写。\n",
        enabled=False, actor="human")
    chat = ChatEngine(seeded)
    identity, run_id = _identity(seeded, chat)

    guidelines = {g["name"]: g for g in _call(
        chat, identity, run_id, "guideline.list")["guidelines"]}
    assert guidelines["发布流程"]["enabled"] is False
    full = _call(chat, identity, run_id, "guideline.list", {"name": "发布流程"})["guideline"]
    assert "先跑 CI。" in full["markdown"] and full["markdown"].startswith("---\n")
    # 读到的全文可直接交回 guideline.save 重新启用
    _call(chat, identity, run_id, "guideline.save",
          {"markdown": full["markdown"], "enabled": True})
    assert next(g for g in seeded.get_project("webshop").guidelines
                if g.name == "发布流程").enabled is True

    skills = {s["id"]: s for s in _call(chat, identity, run_id, "skill.list")["skills"]}
    assert skills["release-notes"]["enabled"] is False
    package = _call(chat, identity, run_id, "skill.list", {"id": "release-notes"})["skill"]
    assert "按模板写。" in package["markdown"] and package["files"] == ["SKILL.md"]
    assert "path" not in package     # 不回传本机路径
    with pytest.raises(AgentToolError) as missing:
        _call(chat, identity, run_id, "skill.list", {"id": "ghost"})
    assert missing.value.code == "not_found"


def _wait_run(store, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = store.get_automation_run(run_id)
        if row and row["status"] != "running":
            return row
        time.sleep(0.05)
    raise AssertionError("automation run did not finish")


def test_automation_list_and_run_from_agent(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    chat.agent_tools.bind_automation_trigger(service.trigger)
    save_automation(seeded, "webshop", id="sync", name="同步",
                    script="printf 'line%.0s' $(seq 3000); echo; echo failed >&2; exit 3",
                    cron="")
    identity, run_id = _identity(seeded, chat)

    triggered = _call(chat, identity, run_id, "automation.run", {"id": "sync"})
    assert triggered["run_id"] > 0 and _receipts(seeded, "automation.run")
    row = _wait_run(seeded, triggered["run_id"])
    assert row["status"] == "failed"

    rows = _call(chat, identity, run_id, "automation.list")["automations"]
    assert [(a["id"], a["last_status"]) for a in rows] == [("sync", "failed")]
    assert "script" not in rows[0]
    detail = _call(chat, identity, run_id, "automation.list", {"id": "sync", "runs": 1})
    assert "exit 3" in detail["automation"]["script"]
    [run] = detail["runs"]
    assert run["exit_code"] == 3 and "failed" in run["stderr"]
    assert run["stdout_truncated"] is True and len(run["stdout"]) == 4000
    with pytest.raises(AgentToolError) as bad:
        _call(chat, identity, run_id, "automation.list", {"id": "sync", "runs": 50})
    assert bad.value.code == "invalid_arguments"
    with pytest.raises(AgentToolError) as missing:
        _call(chat, identity, run_id, "automation.run", {"id": "ghost"})
    assert missing.value.code == "not_found"


def test_automation_list_pages_back_through_kept_runs(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    save_automation(seeded, "webshop", id="hook", name="钩子", script="exit 0", cron="")
    ids = [seeded.start_automation_run("webshop:hook", "webshop", "webhook", f"push #{n}")
           for n in range(25)]
    identity, run_id = _identity(seeded, chat)

    first = _call(chat, identity, run_id, "automation.list", {"id": "hook", "runs": 20})
    assert first["run_count"] == 25
    assert [run["id"] for run in first["runs"]] == ids[:4:-1]
    older = _call(chat, identity, run_id, "automation.list",
                  {"id": "hook", "runs": 20, "before_id": first["runs"][-1]["id"]})
    assert [run["id"] for run in older["runs"]] == ids[4::-1]
    assert older["runs"][-1]["trigger_detail"] == "push #0"
    with pytest.raises(AgentToolError) as bad:
        _call(chat, identity, run_id, "automation.list", {"id": "hook", "before_id": -1})
    assert bad.value.code == "invalid_arguments"


def test_automation_run_is_not_allowed_in_script_allowlist(seeded):
    with pytest.raises(ValueError):
        save_automation(seeded, "webshop", id="loop", script="echo",
                        actions=["automation.run"])


def test_read_only_list_actions_leave_no_receipts(seeded):
    """channel.list 曾在频道里留回执;只读查询都不应留痕。"""
    chat = ChatEngine(seeded)
    identity, run_id = _identity(seeded, chat)
    for action in ("channel.list", "guideline.list", "skill.list", "automation.list"):
        _call(chat, identity, run_id, action)
        assert not _receipts(seeded, action), action


def test_channel_patch_api_shares_agent_rules(seeded):
    seeded.put_channel(Channel(id="webshop:docs-x", name="文档 x", project_id="webshop",
                               content_kind="docs", content_key="x.md"))
    seeded.put_channel(Channel(id="webshop:topic", name="topic", project_id="webshop"))
    with TestClient(create_app()) as client:
        updated = client.patch("/api/chat/channels/webshop:topic",
                               json={"name": "新话题", "workdir": "~/code/any"})
        assert updated.status_code == 200, updated.text
        assert (updated.json()["name"], updated.json()["workdir"]) == ("新话题", "~/code/any")
        assert updated.json()["purpose"] == ""      # 没传的字段保持不变
        cleared = client.patch("/api/chat/channels/webshop:topic", json={"workdir": ""})
        assert cleared.json()["workdir"] is None
        assert client.patch("/api/chat/channels/webshop:docs-x",
                            json={"name": "x"}).status_code == 409
        assert client.post("/api/chat/channels/webshop:topic/archive").status_code == 200
        assert client.patch("/api/chat/channels/webshop:topic",
                            json={"name": "y"}).status_code == 409
        assert client.post("/api/chat/channels/general/archive").status_code == 409
        assert client.patch("/api/chat/channels/webshop:nope",
                            json={"name": "y"}).status_code == 404


# 侧栏频道条目的「•••」菜单:装载真实的 ui.js 判定函数与 channels.js,按频道类型取菜单项
_CHANNEL_MENU_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const context = {
  localStorage: { getItem: () => null },
  document: { addEventListener() {} },
  window: { addEventListener() {} },
  esc: value => String(value ?? ""),
  currentChan: "", currentTab: "chat",
};
vm.createContext(context);
const ui = fs.readFileSync(process.argv[1] + "/ui.js", "utf8");
for (const name of ["channelIsGeneral", "channelIsContent"])
  vm.runInContext(ui.match(new RegExp(`const ${name} = .*`))[0].replace("const", "var"), context);
vm.runInContext(fs.readFileSync(process.argv[1] + "/channels.js", "utf8"), context);
const channels = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(channels.map(channel => {
  const html = context.channelSidebarItem(channel);
  return [...html.matchAll(/onclick="(\w+)\(this\.dataset\.channelId\)">([^<]+)</g)]
    .map(match => [match[1], match[2]]);
})));
"""


def test_sidebar_channel_menu_offers_edit_where_allowed():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the browser renderers")
    channels = [
        {"id": "webshop:topic", "name": "topic"},
        {"id": "general", "name": "大厅"},
        {"id": "webshop:old", "name": "old", "archived": True},
        {"id": "webshop:docs-x", "name": "x", "content_kind": "docs", "content_key": "x.md"},
    ]
    result = subprocess.run(
        [node, "-e", _CHANNEL_MENU_SCRIPT,
         str(Path(__file__).resolve().parents[1] / "missioncrew" / "web" / "js")],
        input=json.dumps(channels), text=True, capture_output=True, check=True)
    topic, general, archived, content = json.loads(result.stdout)

    assert topic == [["openChannelEditDialog", "编辑频道"],
                     ["archiveChannel", "归档频道"], ["deleteChannel", "删除频道"]]
    # general 不能归档/删除,菜单只剩编辑;归档与内容专属频道不能编辑
    assert general == [["openChannelEditDialog", "编辑频道"]]
    assert archived == [["restoreChannel", "恢复频道"], ["deleteChannel", "删除频道"]]
    assert content == [["archiveChannel", "归档频道"], ["deleteChannel", "删除频道"]]


# 频道名 group/topic 的侧栏分组:装载真实的 channels.js,把渲染结果拆成逐行的摘要;
# 浏览器里已收起 webshop 的 ops 分组,选中频道后再渲染一次
_CHANNEL_GROUP_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const channels = JSON.parse(fs.readFileSync(0, "utf8"));
const stored = { "mc.channelGroupsCollapsed": JSON.stringify([JSON.stringify(["webshop", "ops"])]) };
const context = {
  localStorage: { getItem: key => stored[key] ?? null, setItem: (key, value) => { stored[key] = value; } },
  document: { addEventListener() {} },
  window: { addEventListener() {} },
  esc: value => String(value ?? ""),
  overview: { channels }, currentProject: "webshop", currentChan: "webshop:ops-deploy",
  currentTab: "chat",
};
vm.createContext(context);
const ui = fs.readFileSync(process.argv[1] + "/ui.js", "utf8");
for (const name of ["channelIsGeneral", "channelIsContent"])
  vm.runInContext(ui.match(new RegExp(`const ${name} = .*`))[0].replace("const", "var"), context);
vm.runInContext(fs.readFileSync(process.argv[1] + "/channels.js", "utf8"), context);
const rows = () => context.channelSidebarHtml(channels).split('<div class="side-item').slice(1)
  .map(chunk => {
    const classes = chunk.slice(0, chunk.indexOf('"')).trim().split(/\s+/);
    const marker = chunk.match(/<span class="channel-running-marker"([^>]*)>/);
    return [
      chunk.match(/data-group="([^"]*)"/)?.[1] ?? chunk.match(/data-channel-id="([^"]*)"/)[1],
      chunk.match(/<span class="channel-name">([^<]*)</)[1].trim(),
      classes.filter(name => ["channel-subitem", "holds-current", "selected"].includes(name)),
      chunk.match(/<span class="channel-group-count">(\d+)</)?.[1] ?? null,
      marker ? !/\bhidden\b/.test(marker[1]) : null,
    ];
  });
const collapsed = rows();
context.expandChannelGroupOf("webshop:ops-deploy");
process.stdout.write(JSON.stringify({ collapsed, expanded: rows(),
                                      stored: stored["mc.channelGroupsCollapsed"] }));
"""


def test_sidebar_groups_channels_by_slash_in_name():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the browser renderers")
    channels = [
        {"id": "webshop:general", "name": "general"},
        {"id": "webshop:backend-api", "name": "backend/api"},
        {"id": "webshop:misc", "name": "misc"},
        {"id": "webshop:backend-db", "name": " backend / db"},
        {"id": "webshop:ops-deploy", "name": "ops/deploy", "active_run_count": 1},
        {"id": "webshop:docs-x", "name": "文档 · design/arch.md",
         "content_kind": "docs", "content_key": "design/arch.md"},
        {"id": "webshop:edge", "name": "/edge"},
    ]
    result = subprocess.run(
        [node, "-e", _CHANNEL_GROUP_SCRIPT,
         str(Path(__file__).resolve().parents[1] / "missioncrew" / "web" / "js")],
        input=json.dumps(channels), text=True, capture_output=True, check=True)
    data = json.loads(result.stdout)

    # 分组出现在第一个成员的位置,组内条目只写话题;页面对话频道与空组名不分组
    head = [
        ["webshop:general", "general", [], None, False],
        ["backend", "backend", [], "2", None],
        ["webshop:backend-api", "api", ["channel-subitem"], None, False],
        ["webshop:backend-db", "db", ["channel-subitem"], None, False],
        ["webshop:misc", "misc", [], None, False],
    ]
    tail = [
        ["webshop:docs-x", "文档 · design/arch.md", [], None, False],
        ["webshop:edge", "/edge", [], None, False],
    ]
    # 收起的分组藏起成员,标题标出含当前频道并汇总运行标记
    assert data["collapsed"] == head + [
        ["ops", "ops", ["holds-current"], "1", True]] + tail
    # 选中收起分组里的频道会展开该组,并记回浏览器
    assert data["expanded"] == head + [
        ["ops", "ops", [], "1", None],
        ["webshop:ops-deploy", "deploy", ["channel-subitem", "selected"], None, True],
    ] + tail
    assert json.loads(data["stored"]) == []


def test_document_history_lists_versions_and_reads_old_content(seeded):
    """普通角色也能读文档历史(与 document.publish 同权),只读不留回执。"""
    chat = ChatEngine(seeded)
    identity, run_id = _identity(seeded, chat, "dev")
    for content in ("# v1\n", "# v2\n"):
        _call(chat, identity, run_id, "document.publish",
              {"path": "specs/plan.md", "content": content, "overwrite": True})
    _call(chat, identity, run_id, "document.publish",
          {"path": "bin/blob.dat", "content_base64": "AP8=", "overwrite": True})

    listed = _call(chat, identity, run_id, "document.history", {"path": "specs/plan.md"})
    versions = listed["versions"]
    assert len(versions) == 2 and versions[0]["created_at"] >= versions[1]["created_at"]
    old = _call(chat, identity, run_id, "document.history",
                {"path": "specs/plan.md", "revision": versions[-1]["revision"]})
    assert old["content"] == "# v1\n"
    blob_rev = _call(chat, identity, run_id, "document.history",
                     {"path": "bin/blob.dat"})["versions"][0]["revision"]
    blob = _call(chat, identity, run_id, "document.history",
                 {"path": "bin/blob.dat", "revision": blob_rev})
    assert blob["content_base64"] == "AP8=" and "content" not in blob
    assert not _receipts(seeded, "document.history")

    for arguments, code in (({"path": "nope.md"}, "not_found"),
                            ({"path": "specs/plan.md", "limit": 0}, "invalid_arguments"),
                            ({"path": "specs/plan.md", "revision": "0" * 40}, "not_found")):
        with pytest.raises(AgentToolError) as error:
            _call(chat, identity, run_id, "document.history", arguments)
        assert error.value.code == code, arguments
    actions = chat.agent_tools.capabilities(identity)["actions"]
    assert "guideline.history" not in actions and "skill.history" not in actions


def test_guideline_and_skill_history_read_old_versions(seeded):
    project = seeded.get_project("webshop")
    for body in ("旧规则。", "新规则。"):
        save_guideline(seeded, seeded.get_project("webshop"),
                       f"---\nname: 评审规范\ndescription: 评审要求\n---\n\n{body}\n",
                       enabled=True, actor="human")
    for body in ("第一版。", "第二版。"):
        save_project_skill_markdown(
            seeded, seeded.get_project("webshop"), "release-notes",
            f"---\nname: 发布说明\ndescription: 写发布说明\n---\n\n{body}\n",
            enabled=True, actor="human")
    assert project.id == "webshop"
    chat = ChatEngine(seeded)
    identity, run_id = _identity(seeded, chat)

    guideline_versions = _call(chat, identity, run_id, "guideline.history",
                               {"name": "评审规范"})["versions"]
    assert len(guideline_versions) >= 2
    old_guideline = _call(chat, identity, run_id, "guideline.history",
                          {"name": "评审规范", "revision": guideline_versions[-1]["revision"]})
    assert "旧规则。" in old_guideline["markdown"]
    assert old_guideline["markdown"].startswith("---\n")

    skill_versions = _call(chat, identity, run_id, "skill.history",
                           {"id": "release-notes"})["versions"]
    assert len(skill_versions) >= 2
    old_rev = skill_versions[-1]["revision"]
    old_skill = _call(chat, identity, run_id, "skill.history",
                      {"id": "release-notes", "revision": old_rev})
    assert "第一版。" in old_skill["markdown"] and old_skill["files"] == ["SKILL.md"]
    with_file = _call(chat, identity, run_id, "skill.history",
                      {"id": "release-notes", "revision": old_rev, "file": "SKILL.md"})
    assert "第一版。" in with_file["content"]

    for action, arguments, code in (
            ("guideline.history", {"name": "不存在"}, "not_found"),
            ("skill.history", {"id": "release-notes", "file": "SKILL.md"}, "invalid_arguments"),
            ("skill.history", {"id": "release-notes", "revision": old_rev,
                               "file": "missing.md"}, "not_found")):
        with pytest.raises(AgentToolError) as error:
            _call(chat, identity, run_id, action, arguments)
        assert error.value.code == code, (action, arguments)
    assert not _receipts(seeded, "guideline.history") and not _receipts(seeded, "skill.history")
