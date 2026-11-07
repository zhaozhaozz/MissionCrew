"""凭据:全局 + 项目继承、值只写不读、工作区文件下发、各写入口脱敏、credential.request。"""
from __future__ import annotations

import base64
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.automations import AutomationService, save_automation
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.credentials import (credential_context_dir, delete_credential,
                                            effective_credentials, save_credential,
                                            write_credential_context)
from missioncrew.core.models import Project
from missioncrew.core.store import Store

GITHUB = "ghp_" + "a1B2c3D4e5" * 4          # 44 字符,像真的 GitHub token
PROJECT_GITHUB = "ghp_" + "Z9y8X7w6V5" * 4
NPM = "npm_" + "0123456789abcdef" * 2


def _run(store, chat: ChatEngine, role_id: str = "lead"):
    """在 general 起一个活动 Run 并装配,返回 (配置, 按该身份调用动作的函数)。"""
    channel = store.get_channel("general")
    root_id = store.add_message(channel.id, "human", "human", f"@{role_id} 执行", [role_id])
    run_id = store.add_chat_run(channel.id, role_id, root_id, root_id, 0)
    store.update_chat_run(run_id, "running", backend_id="std-1")
    config = chat._assemble(channel, store.get_role("webshop", role_id),
                            store.get_backend("std-1"), root_id, run_id=run_id)
    identity = chat.agent_tools.authenticate(
        Path(config.env["MISSIONCREW_AGENT_TOKEN_FILE"]).read_text().strip())

    def call(action, arguments):
        return chat.agent_tools.execute(
            identity, action, arguments, run_id, f"{action.replace('.', '-')}-{time.time_ns()}")
    return config, call


def _wait_runs(store, automation_id, count, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = store.list_automation_runs(automation_id, 50)
        if len(runs) >= count and all(run["status"] != "running" for run in runs):
            return list(reversed(runs))
        time.sleep(0.05)
    raise AssertionError("automation runs did not finish in time")


# ---- 存储与继承 ----

def test_value_is_stored_apart_from_metadata_and_project_overrides_global(seeded):
    save_credential(seeded, project_id="", name="GITHUB_TOKEN",
                    description="org PAT", value=f"  {GITHUB}\n")
    save_credential(seeded, project_id="", name="NPM_TOKEN", value=NPM)
    save_credential(seeded, project_id="webshop", name="GITHUB_TOKEN",
                    description="webshop only", value=PROJECT_GITHUB)

    rows = seeded._query("SELECT data, value FROM credentials ORDER BY id")
    assert all(GITHUB not in row["data"] and PROJECT_GITHUB not in row["data"] for row in rows)
    assert seeded.credential_value("", "GITHUB_TOKEN") == GITHUB      # 首尾空白已去掉
    meta = seeded.get_credential("", "GITHUB_TOKEN")
    assert meta.has_value and meta.fingerprint == "…" + GITHUB[-4:]
    assert seeded.get_credential("webshop", "GITHUB_TOKEN").id == "webshop:GITHUB_TOKEN"

    effective = {item.name: item for item in effective_credentials(seeded, "webshop")}
    assert effective["GITHUB_TOKEN"].project_id == "webshop"
    assert effective["NPM_TOKEN"].project_id == ""

    for kwargs in ({"name": "github_token", "value": "x" * 12},
                   {"name": "NEW_TOKEN"},                    # 新建必须给值
                   {"name": "EMPTY", "value": "   "}):
        with pytest.raises(ValueError):
            save_credential(seeded, project_id="", **kwargs)
    with pytest.raises(LookupError):
        save_credential(seeded, project_id="missing", name="X_TOKEN", value="y" * 12)


def test_credential_directory_follows_inheritance_and_changes(seeded):
    seeded.put_project(Project(id="other", name="Other"))
    save_credential(seeded, project_id="", name="GITHUB_TOKEN", value=GITHUB)
    save_credential(seeded, project_id="webshop", name="GITHUB_TOKEN", value=PROJECT_GITHUB)
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    save_credential(seeded, project_id="webshop", name="DEPLOY_KEY", value=pem)

    webshop = write_credential_context(seeded, "webshop")
    other = credential_context_dir("other")
    assert webshop.stat().st_mode & 0o777 == 0o700
    assert (webshop / "GITHUB_TOKEN").read_text() == PROJECT_GITHUB
    assert (webshop / "GITHUB_TOKEN").stat().st_mode & 0o777 == 0o600
    assert (webshop / "DEPLOY_KEY").read_text() == pem + "\n"       # 多行值保留结尾换行
    # 全局凭据保存时已写入所有项目的目录
    assert (other / "GITHUB_TOKEN").read_text() == GITHUB
    index = json.loads((webshop / "index.json").read_text())
    assert [item["name"] for item in index["credentials"]] == ["DEPLOY_KEY", "GITHUB_TOKEN"]
    assert index["credentials"][1]["scope"] == "project"
    assert GITHUB not in (webshop / "index.json").read_text()

    # 删掉项目覆盖后回落到全局那份;删掉全局后所有项目都没有了
    delete_credential(seeded, project_id="webshop", name="GITHUB_TOKEN")
    assert (webshop / "GITHUB_TOKEN").read_text() == GITHUB
    delete_credential(seeded, project_id="", name="GITHUB_TOKEN")
    assert not (webshop / "GITHUB_TOKEN").exists() and not (other / "GITHUB_TOKEN").exists()
    with pytest.raises(LookupError):
        delete_credential(seeded, project_id="", name="GITHUB_TOKEN")


# ---- 脱敏 ----

def test_messages_are_redacted_and_mention_offsets_stay_valid(seeded):
    save_credential(seeded, project_id="", name="GITHUB_TOKEN", value=GITHUB)
    content = f"token {GITHUB} 给 @dev 看"
    start = content.index("@dev")
    msg_id = seeded.add_message("general", "human", "human", content, ["dev"],
                                mention_spans=[{"role_id": "dev", "start": start,
                                                "end": start + 4}])
    message = seeded.get_message(msg_id)
    assert message["content"] == "token [credential:GITHUB_TOKEN] 给 @dev 看"
    [span] = json.loads(message["mention_spans"])
    assert message["content"][span["start"]:span["end"]] == "@dev"
    # URL 编码形式同样替换;短于 8 个字符的值不替换,免得误伤正文
    save_credential(seeded, project_id="", name="PIN", value="1234")
    url_value = "a+b/c=d" * 2
    save_credential(seeded, project_id="", name="WEIRD", value=url_value)
    msg_id = seeded.add_message("general", "agent", "agent",
                                "https://x/?k=a%2Bb%2Fc%3Dda%2Bb%2Fc%3Dd pin 1234", [])
    assert seeded.get_message(msg_id)["content"] == "https://x/?k=[credential:WEIRD] pin 1234"


def test_run_events_redact_values_split_across_stream_deltas(seeded):
    save_credential(seeded, project_id="", name="GITHUB_TOKEN", value=GITHUB)
    run_id = seeded.add_chat_run("general", "lead", 1, 1, 0)
    text = f"the token is {GITHUB} done"
    for index in range(0, len(text), 5):        # 模拟流式输出的小片段
        seeded.append_run_event(run_id, "text", text[index:index + 5])
    seeded.append_run_event(run_id, "tool_result", f"stdout: {GITHUB}")
    seeded.append_run_event(run_id, "usage", json.dumps({"note": GITHUB}))
    events = seeded._query("SELECT kind, content FROM run_events WHERE run_id=?", (run_id,))
    joined = "".join(row["content"] for row in events)
    assert GITHUB not in joined
    assert events[0]["content"] == "the token is [credential:GITHUB_TOKEN] done"
    assert json.loads(events[2]["content"]) == {"note": "[credential:GITHUB_TOKEN]"}


# ---- Agent 工作区与提示词 ----

def test_workspace_exposes_credential_files_but_prompt_only_names(seeded):
    save_credential(seeded, project_id="", name="GITHUB_TOKEN",
                    description="org PAT", value=GITHUB)
    save_credential(seeded, project_id="webshop", name="NPM_TOKEN", value=NPM)
    chat = ChatEngine(seeded, max_workers=2)
    config, _call = _run(seeded, chat, "dev")

    directory = Path(config.env["MISSIONCREW_CREDENTIALS_DIR"])
    assert directory.name == "credentials" and directory.is_symlink()
    assert directory.resolve() == credential_context_dir("webshop")
    assert str(credential_context_dir("webshop")) in config.allowed_dirs
    assert (directory / "GITHUB_TOKEN").read_text() == GITHUB
    assert "Credentials humans saved for this project: `GITHUB_TOKEN`, `NPM_TOKEN`" in (
        config.common_prompt)
    everything = config.prompt + config.common_prompt + config.turn_prompt
    assert GITHUB not in everything and NPM not in everything
    manual = Path(config.env["MISSIONCREW_MANUAL"]).read_text()
    assert "## 12. Credentials (credential.*)" in manual

    # 人替换值后,既有工作区立即读到新值,不必重新装配
    save_credential(seeded, project_id="", name="GITHUB_TOKEN", value=PROJECT_GITHUB)
    assert (directory / "GITHUB_TOKEN").read_text() == PROJECT_GITHUB


def test_credential_request_registers_pending_entry_and_posts_link(seeded):
    save_credential(seeded, project_id="", name="GITHUB_TOKEN", value=GITHUB)
    chat = ChatEngine(seeded, max_workers=2)
    config, call = _run(seeded, chat, "dev")

    result = call("credential.request", {"name": "SENTRY_TOKEN",
                                         "description": "上报错误,project:read 权限"})
    assert result["credential"] == {"name": "SENTRY_TOKEN",
                                    "description": "上报错误,project:read 权限",
                                    "status": "pending"}
    pending = seeded.get_credential("webshop", "SENTRY_TOKEN")
    assert not pending.has_value and pending.requested_by == "role:dev"
    receipt = seeded.list_messages("general")[-1]
    assert "`credential.request`" in receipt["content"] and "](/webshop/proj)" in receipt["content"]
    # 待填写的不下发文件;人填写后出现在目录里,请求方信息清空
    directory = Path(config.env["MISSIONCREW_CREDENTIALS_DIR"])
    assert not (directory / "SENTRY_TOKEN").exists()
    save_credential(seeded, project_id="webshop", name="SENTRY_TOKEN", value="s" * 40)
    assert (directory / "SENTRY_TOKEN").read_text() == "s" * 40
    assert seeded.get_credential("webshop", "SENTRY_TOKEN").requested_by == ""

    for arguments, code in (({"name": "GITHUB_TOKEN", "description": "x"}, "already_exists"),
                            ({"name": "bad-name", "description": "x"}, "invalid_arguments"),
                            ({"name": "OTHER_TOKEN"}, "invalid_arguments")):
        with pytest.raises(Exception) as exc:
            call("credential.request", arguments)
        assert getattr(exc.value, "code", "") == code, exc.value


def test_agent_tool_arguments_and_published_files_are_redacted(seeded):
    save_credential(seeded, project_id="", name="GITHUB_TOKEN", value=GITHUB)
    chat = ChatEngine(seeded, max_workers=2)
    _config, call = _run(seeded, chat, "lead")

    created = call("task.create", {"title": "接入 CI", "body": f"用 {GITHUB} 调 API",
                                   "labels": [f"token: {GITHUB}"]})
    task = seeded.get_task(created["task"]["id"])
    assert GITHUB not in json.dumps(task.to_dict(), ensure_ascii=False)
    assert "[credential:GITHUB_TOKEN]" in task.body

    call("document.publish", {
        "path": "notes/ci.md",
        "content_base64": base64.b64encode(f"token={GITHUB}\n".encode()).decode()})
    from missioncrew.collab.documents import library_for
    published = (library_for("webshop").root / "notes" / "ci.md").read_text()
    assert published == "token=[credential:GITHUB_TOKEN]\n"


# ---- 自动化脚本 ----

def test_automation_reads_credentials_and_its_output_is_redacted(seeded):
    save_credential(seeded, project_id="", name="GITHUB_TOKEN", value=GITHUB)
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    automation, _ = save_automation(seeded, "webshop", id="leaky", script=(
        'test -s "$MISSIONCREW_CREDENTIALS_DIR/GITHUB_TOKEN" && echo ok\n'
        'cat "$MISSIONCREW_CREDENTIALS_DIR/GITHUB_TOKEN" >&2\n'))
    service.trigger(automation)
    [run] = _wait_runs(seeded, automation.id, 1)
    assert run["stdout"].strip() == "ok"
    assert run["stderr"] == "[credential:GITHUB_TOKEN]"     # 脚本误打印的值落库前已替换
    with pytest.raises(Exception) as exc:     # 脚本身份不能请求凭据
        chat.agent_tools.execute(
            chat.agent_tools.authenticate(
                chat.agent_tools.issue_automation_token(automation, 600)),
            "credential.request", {"name": "X_TOKEN", "description": "x"}, None, "req-1")
    assert getattr(exc.value, "code", "") == "permission_denied", exc.value


# ---- HTTP API ----

def test_api_never_returns_values_and_lists_inherited_credentials(seeded):
    with TestClient(create_app()) as client:
        created = client.post("/api/credentials", json={
            "name": "GITHUB_TOKEN", "description": "org PAT", "value": GITHUB})
        assert created.status_code == 200, created.text
        assert created.json()["created"] is True and created.json()["scope"] == "global"
        assert client.post("/api/credentials", json={"name": "bad"}).status_code == 400
        assert client.post("/api/credentials", json={"name": "NEW_TOKEN"}).status_code == 400
        assert client.post("/api/projects/missing/credentials", json={
            "name": "X_TOKEN", "value": "y" * 12}).status_code == 404
        saved = client.post("/api/projects/webshop/credentials", json={
            "name": "GITHUB_TOKEN", "value": PROJECT_GITHUB})
        assert saved.status_code == 200 and saved.json()["id"] == "webshop:GITHUB_TOKEN"
        # 只改用途不动值
        assert client.post("/api/credentials", json={
            "name": "GITHUB_TOKEN", "description": "renamed"}).status_code == 200
        assert seeded.credential_value("", "GITHUB_TOKEN") == GITHUB

        listed = client.get("/api/projects/webshop/credentials").json()
        assert [item["name"] for item in listed["credentials"]] == ["GITHUB_TOKEN"]
        [inherited] = listed["inherited"]
        assert inherited["overridden"] is True and inherited["description"] == "renamed"
        everything = json.dumps([
            listed, client.get("/api/credentials").json(),
            client.get("/api/overview").json(),
            client.get("/api/projects/webshop/overview").json()], ensure_ascii=False)
        assert GITHUB not in everything and PROJECT_GITHUB not in everything

        assert client.delete("/api/projects/webshop/credentials/GITHUB_TOKEN").json() == {
            "deleted": True}
        assert client.delete("/api/projects/webshop/credentials/GITHUB_TOKEN").status_code == 404
        assert client.delete("/api/credentials/GITHUB_TOKEN").json() == {"deleted": True}


def test_deleting_project_drops_its_credentials_and_directory(seeded):
    with TestClient(create_app()) as client:
        client.post("/api/projects", json={"id": "tmp", "name": "Tmp"})
        save_credential(seeded, project_id="tmp", name="X_TOKEN", value="x" * 20)
        assert (credential_context_dir("tmp") / "X_TOKEN").exists()
        assert client.delete("/api/projects/tmp").status_code == 200
    assert seeded.list_credentials("tmp") == []
    assert not credential_context_dir("tmp").exists()


def test_settings_pages_manage_credentials_and_composer_checks_secrets(seeded):
    with TestClient(create_app()) as client:
        html = client.get("/").text
        js = client.get("/assets/js/settings-credentials.js").text
        sidebar = client.get("/assets/js/sidebar.js").text
        configs = client.get("/assets/js/project-configs.js").text
    assert 'id="global-credential-table"' in html and 'id="proj-credential-table"' in html
    assert html.index('src="/assets/js/settings-credentials.js"') < html.index(
        'src="/assets/js/main.js"')
    assert "confirmSecretInMessage(content, box)" in sidebar
    assert "confirmSecretInMessage(payload.content, box)" in configs
    # 值只写不读:前端没有读取值的请求
    assert "/value" not in js and "function detectSecret" in js


# ---- 旧受控资源的清理 ----

def test_legacy_resource_table_and_project_field_are_dropped(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE resources (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.commit()
    conn.close()
    store = Store(path)
    tables = {row["name"] for row in store._query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "resources" not in tables and "credentials" in tables
    project = Project.from_dict({"id": "p", "name": "p", "resources": ["test-db"]})
    assert "resources" not in project.to_dict()
