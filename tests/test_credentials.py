"""凭据(环境):全局 + 项目继承、secret 只写不读、工作区文件下发、各写入口脱敏、
credential.request,以及旧版单值凭据到环境的迁移。"""
from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab.automations import AutomationService, save_automation
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.credentials import (credential_context_dir, delete_credential,
                                            effective_credentials, save_environment,
                                            write_credential_context)
from missioncrew.collab.github import github_token
from missioncrew.core.models import Project
from missioncrew.core.store import Store

GITHUB = "ghp_" + "a1B2c3D4e5" * 4          # 44 字符,像真的 GitHub token
PROJECT_GITHUB = "ghp_" + "Z9y8X7w6V5" * 4
NPM = "npm_" + "0123456789abcdef" * 2


def _secret(store, project_id: str, environment: str, value: str, *,
            variable: str = "TOKEN", description=None):
    """存一个只有单个 secret 变量的环境,如 GITHUB/TOKEN、NPM/TOKEN。"""
    return save_environment(store, project_id=project_id, name=environment,
                            description=description,
                            variables=[{"name": variable, "value": value}])[0]


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

def test_secret_is_stored_apart_from_metadata_and_project_overrides_global(seeded):
    _secret(seeded, "", "GITHUB", f"  {GITHUB}\n", description="org PAT")
    _secret(seeded, "", "NPM", NPM)
    _secret(seeded, "webshop", "GITHUB", PROJECT_GITHUB, description="webshop only")

    rows = seeded._query("SELECT data, value FROM credentials ORDER BY id")
    assert all(GITHUB not in row["data"] and PROJECT_GITHUB not in row["data"] for row in rows)
    assert seeded.credential_secrets("", "GITHUB") == {"TOKEN": GITHUB}     # 首尾空白已去掉
    meta = seeded.get_credential("", "GITHUB")
    assert meta.has_value and meta.variable("TOKEN").fingerprint == "…" + GITHUB[-4:]
    assert seeded.get_credential("webshop", "GITHUB").id == "webshop:GITHUB"

    effective = {item.name: item for item in effective_credentials(seeded, "webshop")}
    assert effective["GITHUB"].project_id == "webshop"
    assert effective["NPM"].project_id == ""

    for name, variables in (("github", [{"name": "TOKEN", "value": "x" * 12}]),
                            ("NEW_ONE", [{"name": "TOKEN"}]),               # 新建必须给值
                            ("EMPTY", [{"name": "TOKEN", "value": "   "}])):
        with pytest.raises(ValueError):
            save_environment(seeded, project_id="", name=name, variables=variables)
    with pytest.raises(LookupError):
        _secret(seeded, "missing", "X", "y" * 12)


def test_credential_directory_follows_inheritance_and_changes(seeded):
    seeded.put_project(Project(id="other", name="Other"))
    _secret(seeded, "", "GITHUB", GITHUB)
    _secret(seeded, "webshop", "GITHUB", PROJECT_GITHUB)
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    _secret(seeded, "webshop", "DEPLOY", pem, variable="KEY")

    webshop = write_credential_context(seeded, "webshop")
    other = credential_context_dir("other")
    assert webshop.stat().st_mode & 0o777 == 0o700
    assert (webshop / "GITHUB").stat().st_mode & 0o777 == 0o700
    assert (webshop / "GITHUB" / "TOKEN").read_text() == PROJECT_GITHUB
    assert (webshop / "GITHUB" / "TOKEN").stat().st_mode & 0o777 == 0o600
    assert (webshop / "DEPLOY" / "KEY").read_text() == pem + "\n"       # 多行值保留结尾换行
    # 全局环境保存时已写入所有项目的目录
    assert (other / "GITHUB" / "TOKEN").read_text() == GITHUB
    index = json.loads((webshop / "index.json").read_text())
    assert [item["name"] for item in index["credentials"]] == ["DEPLOY", "GITHUB"]
    assert index["credentials"][1] == {
        "name": "GITHUB", "description": "", "scope": "project",
        "variables": [{"name": "TOKEN", "secret": True, "available": True}]}
    assert GITHUB not in (webshop / "index.json").read_text()

    # 删掉项目覆盖后回落到全局那个;删掉全局后所有项目都没有了
    delete_credential(seeded, project_id="webshop", name="GITHUB")
    assert (webshop / "GITHUB" / "TOKEN").read_text() == GITHUB
    delete_credential(seeded, project_id="", name="GITHUB")
    assert not (webshop / "GITHUB").exists() and not (other / "GITHUB").exists()
    with pytest.raises(LookupError):
        delete_credential(seeded, project_id="", name="GITHUB")


# ---- 脱敏 ----

def test_messages_are_redacted_and_mention_offsets_stay_valid(seeded):
    _secret(seeded, "", "GITHUB", GITHUB)
    content = f"token {GITHUB} 给 @dev 看"
    start = content.index("@dev")
    msg_id = seeded.add_message("general", "human", "human", content, ["dev"],
                                mention_spans=[{"role_id": "dev", "start": start,
                                                "end": start + 4}])
    message = seeded.get_message(msg_id)
    assert message["content"] == "token [credential:GITHUB.TOKEN] 给 @dev 看"
    [span] = json.loads(message["mention_spans"])
    assert message["content"][span["start"]:span["end"]] == "@dev"
    # URL 编码形式同样替换;短于 8 个字符的值不替换,免得误伤正文
    _secret(seeded, "", "DOOR", "1234", variable="PIN")
    _secret(seeded, "", "WEIRD", "a+b/c=d" * 2, variable="KEY")
    msg_id = seeded.add_message("general", "agent", "agent",
                                "https://x/?k=a%2Bb%2Fc%3Dda%2Bb%2Fc%3Dd pin 1234", [])
    assert seeded.get_message(msg_id)["content"] == "https://x/?k=[credential:WEIRD.KEY] pin 1234"


def test_run_events_redact_values_split_across_stream_deltas(seeded):
    _secret(seeded, "", "GITHUB", GITHUB)
    run_id = seeded.add_chat_run("general", "lead", 1, 1, 0)
    text = f"the token is {GITHUB} done"
    for index in range(0, len(text), 5):        # 模拟流式输出的小片段
        seeded.append_run_event(run_id, "text", text[index:index + 5])
    seeded.append_run_event(run_id, "tool_result", f"stdout: {GITHUB}")
    seeded.append_run_event(run_id, "usage", json.dumps({"note": GITHUB}))
    events = seeded._query("SELECT kind, content FROM run_events WHERE run_id=?", (run_id,))
    joined = "".join(row["content"] for row in events)
    assert GITHUB not in joined
    assert events[0]["content"] == "the token is [credential:GITHUB.TOKEN] done"
    assert json.loads(events[2]["content"]) == {"note": "[credential:GITHUB.TOKEN]"}


# ---- Agent 工作区与提示词 ----

def test_workspace_exposes_credential_files_but_prompt_only_names(seeded):
    _secret(seeded, "", "GITHUB", GITHUB, description="org PAT")
    _secret(seeded, "webshop", "NPM", NPM)
    chat = ChatEngine(seeded, max_workers=2)
    config, _call = _run(seeded, chat, "dev")

    directory = Path(config.env["MISSIONCREW_CREDENTIALS_DIR"])
    assert directory.name == "credentials" and directory.is_symlink()
    assert directory.resolve() == credential_context_dir("webshop")
    assert str(credential_context_dir("webshop")) in config.allowed_dirs
    assert (directory / "GITHUB" / "TOKEN").read_text() == GITHUB
    assert "one file each): `GITHUB` (TOKEN); `NPM` (TOKEN)." in config.common_prompt
    everything = config.prompt + config.common_prompt + config.turn_prompt
    assert GITHUB not in everything and NPM not in everything
    manual = Path(config.env["MISSIONCREW_MANUAL"]).read_text()
    assert "## 12. Credentials (credential.*)" in manual

    # 人替换值后,既有工作区立即读到新值,不必重新装配
    _secret(seeded, "", "GITHUB", PROJECT_GITHUB)
    assert (directory / "GITHUB" / "TOKEN").read_text() == PROJECT_GITHUB


def test_credential_request_registers_pending_variables_and_posts_link(seeded):
    _secret(seeded, "", "GITHUB", GITHUB)
    chat = ChatEngine(seeded, max_workers=2)
    config, call = _run(seeded, chat, "dev")

    result = call("credential.request", {"name": "SENTRY",
                                         "description": "上报错误,project:read 权限",
                                         "variables": [{"name": "TOKEN"}]})
    assert result["credential"] == {
        "name": "SENTRY", "description": "上报错误,project:read 权限", "status": "pending",
        "variables": [{"name": "TOKEN", "secret": True, "status": "pending"}]}
    pending = seeded.get_credential("webshop", "SENTRY")
    assert not pending.has_value and pending.requested_by == "role:dev"
    receipt = seeded.list_messages("general")[-1]
    # 回执里的表单链接是资源 URL(聊天里能点开),同一个地址也回给角色,供它转述时引用
    form_url = "/resources/webshop/settings/credentials"
    assert "`credential.request`" in receipt["content"]
    assert f"[项目设置 → 凭据]({form_url})" in receipt["content"]
    assert result["resource_url"] == form_url
    # 待填写的不下发文件;人填写后出现在目录里,请求方信息清空
    directory = Path(config.env["MISSIONCREW_CREDENTIALS_DIR"])
    assert not (directory / "SENTRY").exists()
    _secret(seeded, "webshop", "SENTRY", "s" * 40)
    assert (directory / "SENTRY" / "TOKEN").read_text() == "s" * 40
    assert seeded.get_credential("webshop", "SENTRY").requested_by == ""

    for arguments, code in (
            ({"name": "GITHUB", "description": "x", "variables": [{"name": "TOKEN"}]},
             "already_exists"),
            ({"name": "bad-name", "description": "x", "variables": [{"name": "TOKEN"}]},
             "invalid_arguments"),
            ({"name": "OTHER", "variables": [{"name": "TOKEN"}]}, "invalid_arguments"),
            # 凭据一律是环境:不带 variables 的旧式单值请求被拒绝并说明怎么写
            ({"name": "OTHER", "description": "x"}, "invalid_arguments"),
            # GitHub 身份必须是 secret
            ({"name": "GITHUB", "description": "x",
              "variables": [{"name": "TOKEN", "secret": False, "value": "x"}]},
             "invalid_arguments")):
        with pytest.raises(Exception) as exc:
            call("credential.request", arguments)
        assert getattr(exc.value, "code", "") == code, (arguments, exc.value)


def test_agent_tool_arguments_and_published_files_are_redacted(seeded):
    _secret(seeded, "", "GITHUB", GITHUB)
    chat = ChatEngine(seeded, max_workers=2)
    _config, call = _run(seeded, chat, "lead")

    created = call("task.create", {"title": "接入 CI", "body": f"用 {GITHUB} 调 API",
                                   "labels": [f"token: {GITHUB}"]})
    task = seeded.get_task(created["task"]["id"])
    assert GITHUB not in json.dumps(task.to_dict(), ensure_ascii=False)
    assert "[credential:GITHUB.TOKEN]" in task.body

    call("document.publish", {
        "path": "notes/ci.md",
        "content_base64": base64.b64encode(f"token={GITHUB}\n".encode()).decode()})
    from missioncrew.collab.documents import library_for
    published = (library_for("webshop").root / "notes" / "ci.md").read_text()
    assert published == "token=[credential:GITHUB.TOKEN]\n"


# ---- 自动化脚本 ----

def test_automation_reads_credentials_and_its_output_is_redacted(seeded):
    _secret(seeded, "", "NPM", NPM)
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    automation, _ = save_automation(seeded, "webshop", id="leaky", script=(
        'test -s "$MISSIONCREW_CREDENTIALS_DIR/NPM/TOKEN" && echo ok\n'
        'cat "$MISSIONCREW_CREDENTIALS_DIR/NPM/TOKEN" >&2\n'))
    service.trigger(automation)
    [run] = _wait_runs(seeded, automation.id, 1)
    assert run["stdout"].strip() == "ok"
    assert run["stderr"] == "[credential:NPM.TOKEN]"     # 脚本误打印的值落库前已替换
    with pytest.raises(Exception) as exc:     # 脚本身份不能请求凭据
        chat.agent_tools.execute(
            chat.agent_tools.authenticate(
                chat.agent_tools.issue_automation_token(automation, 600)),
            "credential.request", {"name": "X", "description": "x",
                                   "variables": [{"name": "TOKEN"}]}, None, "req-1")
    assert getattr(exc.value, "code", "") == "permission_denied", exc.value


# ---- HTTP API ----

def test_api_never_returns_secrets_and_lists_inherited_environments(seeded):
    token = [{"name": "TOKEN", "value": GITHUB}]
    with TestClient(create_app()) as client:
        created = client.post("/api/credentials", json={
            "name": "GITHUB", "description": "org PAT", "variables": token})
        assert created.status_code == 200, created.text
        assert created.json()["created"] is True and created.json()["scope"] == "global"
        assert client.post("/api/credentials", json={
            "name": "bad", "variables": token}).status_code == 400
        assert client.post("/api/credentials", json={"name": "NEW_ONE"}).status_code == 422
        assert client.post("/api/credentials", json={
            "name": "NEW_ONE", "variables": [{"name": "TOKEN"}]}).status_code == 400
        assert client.post("/api/projects/missing/credentials", json={
            "name": "X", "variables": token}).status_code == 404
        saved = client.post("/api/projects/webshop/credentials", json={
            "name": "GITHUB", "variables": [{"name": "TOKEN", "value": PROJECT_GITHUB}]})
        assert saved.status_code == 200 and saved.json()["id"] == "webshop:GITHUB"
        # 只改用途不动值
        assert client.post("/api/credentials", json={
            "name": "GITHUB", "description": "renamed",
            "variables": [{"name": "TOKEN"}]}).status_code == 200
        assert seeded.credential_secrets("", "GITHUB") == {"TOKEN": GITHUB}
        # 表单以为自己在新建时带 create:名字已被占用就拒绝,不整份换掉已有环境
        taken = client.post("/api/credentials", json={
            "name": "GITHUB", "create": True,
            "variables": [{"name": "OTHER", "value": "o" * 12}]})
        assert taken.status_code == 409 and "已存在" in taken.json()["detail"]
        assert seeded.credential_secrets("", "GITHUB") == {"TOKEN": GITHUB}

        listed = client.get("/api/projects/webshop/credentials").json()
        assert [item["name"] for item in listed["credentials"]] == ["GITHUB"]
        [inherited] = listed["inherited"]
        assert inherited["overridden"] is True and inherited["description"] == "renamed"
        everything = json.dumps([
            listed, client.get("/api/credentials").json(),
            client.get("/api/overview").json(),
            client.get("/api/projects/webshop/overview").json()], ensure_ascii=False)
        assert GITHUB not in everything and PROJECT_GITHUB not in everything

        assert client.delete("/api/projects/webshop/credentials/GITHUB").json() == {
            "deleted": True}
        assert client.delete("/api/projects/webshop/credentials/GITHUB").status_code == 404
        assert client.delete("/api/credentials/GITHUB").json() == {"deleted": True}


def test_deleting_project_drops_its_credentials_and_directory(seeded):
    with TestClient(create_app()) as client:
        client.post("/api/projects", json={"id": "tmp", "name": "Tmp"})
        _secret(seeded, "tmp", "X", "x" * 20)
        assert (credential_context_dir("tmp") / "X" / "TOKEN").exists()
        assert client.delete("/api/projects/tmp").status_code == 200
    assert seeded.list_credentials("tmp") == []
    assert not credential_context_dir("tmp").exists()


# ---- 多个变量的环境:每个变量各自标记是否 secret ----

BOT_PASSWORD = "p@ss-" + "Qw3rTy9z" * 3
BOT_VARIABLES = [{"name": "URL", "secret": False, "value": "http://127.0.0.1:8791"},
                 {"name": "USERNAME", "secret": False, "value": "admin-operator"},
                 {"name": "PASSWORD", "secret": True, "value": BOT_PASSWORD}]


def test_environment_keeps_secret_values_apart_and_writes_a_directory(seeded):
    environment, created = save_environment(
        seeded, project_id="webshop", name="LOCAL_BOT", description="本机 Bot 控制台",
        variables=BOT_VARIABLES)
    assert created and environment.has_value
    # 元数据里只有普通变量的值;secret 只留指纹,原文只在 value 列
    data = json.dumps(seeded.get_credential("webshop", "LOCAL_BOT").to_dict())
    assert "admin-operator" in data and BOT_PASSWORD not in data
    password = environment.variable("PASSWORD")
    assert password.secret and password.value == "" and password.fingerprint == "…Ty9z"
    assert seeded.credential_secrets("webshop", "LOCAL_BOT") == {"PASSWORD": BOT_PASSWORD}

    directory = credential_context_dir("webshop") / "LOCAL_BOT"
    assert directory.is_dir() and directory.stat().st_mode & 0o777 == 0o700
    assert {entry.name: entry.read_text() for entry in directory.iterdir()} == {
        "URL": "http://127.0.0.1:8791", "USERNAME": "admin-operator",
        "PASSWORD": BOT_PASSWORD}
    assert (directory / "PASSWORD").stat().st_mode & 0o777 == 0o600
    index_text = (credential_context_dir("webshop") / "index.json").read_text()
    assert BOT_PASSWORD not in index_text
    [entry] = json.loads(index_text)["credentials"]
    assert entry["variables"] == [
        {"name": "URL", "secret": False, "available": True, "value": "http://127.0.0.1:8791"},
        {"name": "USERNAME", "secret": False, "available": True, "value": "admin-operator"},
        {"name": "PASSWORD", "secret": True, "available": True}]

    # 只有 secret 变量脱敏,占位符带环境名和变量名;普通变量的值原样保留
    text = seeded.redact_credentials(f"curl -u admin-operator:{BOT_PASSWORD} http://127.0.0.1:8791")
    assert text == "curl -u admin-operator:[credential:LOCAL_BOT.PASSWORD] http://127.0.0.1:8791"


def test_environment_update_replaces_the_variable_list_and_guards_secret_values(seeded):
    save_environment(seeded, project_id="webshop", name="LOCAL_BOT", variables=BOT_VARIABLES)
    directory = credential_context_dir("webshop") / "LOCAL_BOT"

    # 不带 value = 保留原值;列表里没有的变量被删掉,文件一起消失
    updated, created = save_environment(seeded, project_id="webshop", name="LOCAL_BOT", variables=[
        {"name": "USERNAME", "secret": False, "value": "root-operator"},
        {"name": "PASSWORD", "secret": True}])
    assert not created and [item.name for item in updated.variables] == ["USERNAME", "PASSWORD"]
    assert sorted(entry.name for entry in directory.iterdir()) == ["PASSWORD", "USERNAME"]
    assert (directory / "USERNAME").read_text() == "root-operator"
    assert (directory / "PASSWORD").read_text() == BOT_PASSWORD

    # 普通变量改成 secret 可以沿用原值,之后它不再出现在元数据里并开始脱敏
    save_environment(seeded, project_id="webshop", name="LOCAL_BOT", variables=[
        {"name": "USERNAME", "secret": True}, {"name": "PASSWORD", "secret": True}])
    assert "root-operator" not in json.dumps(
        seeded.get_credential("webshop", "LOCAL_BOT").to_dict())
    assert seeded.redact_credentials("login root-operator") == \
        "login [credential:LOCAL_BOT.USERNAME]"

    # secret 改回普通变量必须重新填值,否则等于把保密值显示出来
    with pytest.raises(ValueError, match="重新填写"):
        save_environment(seeded, project_id="webshop", name="LOCAL_BOT", variables=[
            {"name": "USERNAME", "secret": True}, {"name": "PASSWORD", "secret": False}])
    for variables, message in (([], "至少"),
                               ([{"name": "bad-name", "value": "x"}], "变量名"),
                               ([{"name": "A", "value": "1"}, {"name": "A", "value": "2"}], "重复"),
                               ([{"name": "NEW_ONE", "secret": True}], "请填写"),
                               ([{"name": "BIG", "secret": False, "value": "x" * 5000}], "4 KB")):
        with pytest.raises(ValueError, match=message):
            save_environment(seeded, project_id="webshop", name="LOCAL_BOT", variables=variables)

    # GITHUB 环境的 TOKEN 是 GitHub 身份,不能存成明文变量;同一环境里的其他变量不受限
    with pytest.raises(ValueError, match="GitHub 身份"):
        save_environment(seeded, project_id="", name="GITHUB", variables=[
            {"name": "TOKEN", "secret": False, "value": GITHUB}])
    save_environment(seeded, project_id="", name="GITHUB", variables=[
        {"name": "TOKEN", "value": GITHUB},
        {"name": "API_URL", "secret": False, "value": "https://api.github.com"}])
    assert github_token(seeded, "webshop") == (GITHUB, "global")


def test_project_environment_replaces_the_global_one_as_a_whole(seeded):
    save_environment(seeded, project_id="", name="STAGING", variables=BOT_VARIABLES)
    root = credential_context_dir("webshop")
    assert sorted(entry.name for entry in (root / "STAGING").iterdir()) == [
        "PASSWORD", "URL", "USERNAME"]
    # 项目里同名的环境整份盖住全局那个:不是逐变量合并,全局有而项目没有的变量也看不到
    _secret(seeded, "webshop", "STAGING", "p" * 20, variable="PASSWORD")
    assert [entry.name for entry in (root / "STAGING").iterdir()] == ["PASSWORD"]
    assert (root / "STAGING" / "PASSWORD").read_text() == "p" * 20
    delete_credential(seeded, project_id="webshop", name="STAGING")
    assert (root / "STAGING" / "URL").read_text() == "http://127.0.0.1:8791"

    # GitHub 身份跟着同一条规则走:项目自己的 GITHUB 环境里没有 TOKEN 时,不回落到全局的
    _secret(seeded, "", "GITHUB", GITHUB)
    assert github_token(seeded, "webshop") == (GITHUB, "global")
    _secret(seeded, "webshop", "GITHUB", "https://ghe.example.com", variable="API_URL")
    assert github_token(seeded, "webshop") == ("", "")
    assert not (root / "GITHUB" / "TOKEN").exists()
    save_environment(seeded, project_id="webshop", name="GITHUB", variables=[
        {"name": "API_URL", "secret": False, "value": "https://ghe.example.com"},
        {"name": "TOKEN", "value": PROJECT_GITHUB}])
    assert github_token(seeded, "webshop") == (PROJECT_GITHUB, "project")
    assert github_token(seeded, "") == (GITHUB, "global")


def test_credential_request_with_variables_registers_an_environment(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    config, call = _run(seeded, chat, "dev")
    directory = Path(config.env["MISSIONCREW_CREDENTIALS_DIR"]) / "LOCAL_BOT"

    result = call("credential.request", {
        "name": "LOCAL_BOT", "description": "登录本机 Bot 控制台",
        "variables": [{"name": "USERNAME", "secret": False, "value": "admin-operator"},
                      {"name": "PASSWORD"}]})
    assert result["credential"] == {
        "name": "LOCAL_BOT", "description": "登录本机 Bot 控制台",
        "status": "pending", "variables": [
            {"name": "USERNAME", "secret": False, "status": "available"},
            {"name": "PASSWORD", "secret": True, "status": "pending"}]}
    assert result["resource_url"] == "/resources/webshop/settings/credentials"
    receipt = seeded.list_messages("general")[-1]["content"]
    assert "请求环境 `LOCAL_BOT`" in receipt and "待填写变量 `PASSWORD`" in receipt
    pending = seeded.get_credential("webshop", "LOCAL_BOT")
    assert not pending.has_value and pending.requested_by == "role:dev"
    # Agent 自己带来的普通变量马上可用;secret 等人填,索引里标着还缺
    assert (directory / "USERNAME").read_text() == "admin-operator"
    assert not (directory / "PASSWORD").exists()
    [entry] = json.loads((directory.parent / "index.json").read_text())["credentials"]
    assert entry["variables"][1] == {"name": "PASSWORD", "secret": True, "available": False}
    assert "one file each): `LOCAL_BOT` (USERNAME)." in chat._credentials_line("webshop")

    # 人把缺的补上后环境不再待填写;Agent 再要同样的变量得到 already_exists
    save_environment(seeded, project_id="webshop", name="LOCAL_BOT", variables=[
        {"name": "USERNAME", "secret": False, "value": "admin-operator"},
        {"name": "PASSWORD", "secret": True, "value": BOT_PASSWORD}])
    filled = seeded.get_credential("webshop", "LOCAL_BOT")
    assert filled.has_value and filled.requested_by == ""
    assert (directory / "PASSWORD").read_text() == BOT_PASSWORD
    with pytest.raises(Exception) as exc:
        call("credential.request", {"name": "LOCAL_BOT", "description": "x",
                                    "variables": [{"name": "PASSWORD"}]})
    assert getattr(exc.value, "code", "") == "already_exists", exc.value

    # 给已有环境补一个变量:原有变量和它们的值不动,环境回到待填写
    call("credential.request", {"name": "LOCAL_BOT", "description": "再要一个 API key",
                                "variables": [{"name": "USERNAME", "secret": True},
                                              {"name": "API_KEY"}]})
    extended = seeded.get_credential("webshop", "LOCAL_BOT")
    assert [(item.name, item.secret, item.has_value) for item in extended.variables] == [
        ("USERNAME", False, True), ("PASSWORD", True, True), ("API_KEY", True, False)]
    assert not extended.has_value and extended.description == "登录本机 Bot 控制台"
    assert (directory / "PASSWORD").read_text() == BOT_PASSWORD

    # secret 的值不能经参数传;继承的全局环境缺变量要求换做法
    save_environment(seeded, project_id="", name="SHARED", variables=BOT_VARIABLES)
    for arguments in ({"name": "OTHER", "variables": [{"name": "TOKEN", "value": "s" * 20}]},
                      {"name": "SHARED", "variables": [{"name": "API_KEY"}]},
                      {"name": "OTHER", "variables": []}):
        with pytest.raises(Exception) as exc:
            call("credential.request", {"description": "x", **arguments})
        assert getattr(exc.value, "code", "") == "invalid_arguments", (arguments, exc.value)
    with pytest.raises(Exception) as exc:      # 全局环境里已有的变量:直接用
        call("credential.request", {"name": "SHARED", "description": "x",
                                    "variables": [{"name": "PASSWORD"}]})
    assert getattr(exc.value, "code", "") == "already_exists", exc.value
    assert seeded.get_credential("webshop", "SHARED") is None


def test_api_saves_environments_and_only_returns_plain_variable_values(seeded):
    with TestClient(create_app()) as client:
        created = client.post("/api/projects/webshop/credentials", json={
            "name": "LOCAL_BOT", "description": "本机 Bot 控制台", "variables": BOT_VARIABLES})
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["created"] is True and body["has_value"] and "kind" not in body
        listing = client.get("/api/projects/webshop/credentials")
        assert BOT_PASSWORD not in listing.text and "admin-operator" in listing.text
        [item] = listing.json()["credentials"]
        assert item["variables"][2] == {"name": "PASSWORD", "secret": True, "has_value": True,
                                        "fingerprint": "…Ty9z", "value": ""}
        # 保留 secret 原值、只改普通变量
        kept = client.post("/api/projects/webshop/credentials", json={
            "name": "LOCAL_BOT", "variables": [
                {"name": "URL", "secret": False, "value": "http://127.0.0.1:9000"},
                {"name": "PASSWORD", "secret": True}]})
        assert kept.status_code == 200, kept.text
        assert seeded.credential_secrets("webshop", "LOCAL_BOT") == {"PASSWORD": BOT_PASSWORD}
        for payload in ({"name": "LOCAL_BOT", "variables": []},
                        {"name": "LOCAL_BOT", "variables": [{"name": "TOKEN"}]}):
            assert client.post("/api/projects/webshop/credentials",
                               json=payload).status_code == 400, payload
        assert client.delete("/api/projects/webshop/credentials/LOCAL_BOT").status_code == 200
        assert not (credential_context_dir("webshop") / "LOCAL_BOT").exists()



# ---- 旧版单值凭据迁成环境 ----

def _legacy_row(store, project_id, name, value, **extra):
    """按旧版格式直接写一行单值凭据(值是原文,元数据里没有 variables)。"""
    data = {"name": name, "project_id": project_id, "description": f"{name} 的用途",
            "has_value": bool(value), "fingerprint": f"…{value[-4:]}" if value else "",
            "requested_by": "", "created_at": 1.0, "updated_at": 2.0, **extra}
    store._execute("INSERT INTO credentials(id, data, value) VALUES(?, ?, ?)",
                   (f"{project_id}:{name}" if project_id else name, json.dumps(data), value))


def test_legacy_single_value_credentials_become_environments(seeded):
    _legacy_row(seeded, "", "GITHUB_TOKEN", GITHUB)
    _legacy_row(seeded, "webshop", "GITHUB_TOKEN", PROJECT_GITHUB, kind="secret", variables=[])
    _legacy_row(seeded, "webshop", "NPM_TOKEN", NPM)
    _legacy_row(seeded, "webshop", "SENTRY_TOKEN", "", requested_by="role:dev")
    # 上一版已经是环境的行:只去掉不再使用的字段
    seeded._execute("INSERT INTO credentials(id, data, value) VALUES(?, ?, ?)", (
        "webshop:LOCAL_BOT", json.dumps({
            "name": "LOCAL_BOT", "project_id": "webshop", "description": "bot", "has_value": True,
            "fingerprint": "", "requested_by": "", "created_at": 1.0, "updated_at": 2.0,
            "kind": "environment", "variables": [
                {"name": "USERNAME", "secret": False, "has_value": True, "fingerprint": "",
                 "value": "admin"},
                {"name": "PASSWORD", "secret": True, "has_value": True, "fingerprint": "…Ty9z",
                 "value": ""}]}), json.dumps({"PASSWORD": BOT_PASSWORD})))

    migrated = Store(seeded.path)       # 打开库时迁移
    assert sorted(item.id for item in migrated.list_credentials()) == [
        "GITHUB", "webshop:GITHUB", "webshop:LOCAL_BOT", "webshop:NPM_TOKEN",
        "webshop:SENTRY_TOKEN"]
    # GitHub 身份:GITHUB_TOKEN -> GITHUB / TOKEN,项目覆盖全局的关系不变
    assert github_token(migrated, "webshop") == (PROJECT_GITHUB, "project")
    assert github_token(migrated, "") == (GITHUB, "global")
    identity = migrated.get_credential("", "GITHUB")
    assert identity.description == "GITHUB_TOKEN 的用途" and identity.created_at == 1.0
    [token] = identity.variables
    assert (token.name, token.secret, token.has_value, token.fingerprint) == (
        "TOKEN", True, True, "…" + GITHUB[-4:])
    # 其他凭据:同名环境,值在变量 VALUE 里;待填写的仍然待填写
    assert migrated.credential_secrets("webshop", "NPM_TOKEN") == {"VALUE": NPM}
    pending = migrated.get_credential("webshop", "SENTRY_TOKEN")
    assert not pending.has_value and pending.requested_by == "role:dev"
    assert [(item.name, item.has_value) for item in pending.variables] == [("VALUE", False)]
    assert migrated.credential_secrets("webshop", "LOCAL_BOT") == {"PASSWORD": BOT_PASSWORD}
    assert migrated.get_credential("webshop", "LOCAL_BOT").variable("USERNAME").value == "admin"
    raw = "".join(row["data"] for row in migrated._query("SELECT data FROM credentials"))
    assert '"kind"' not in raw and GITHUB not in raw and NPM not in raw
    assert migrated.redact_credentials(f"{NPM} {GITHUB}") == (
        "[credential:NPM_TOKEN.VALUE] [credential:GITHUB.TOKEN]")

    # 目录按新形态重写:旧版留下的同名文件被目录取代
    root = credential_context_dir("webshop")
    root.mkdir(parents=True, exist_ok=True)
    (root / "NPM_TOKEN").write_text(NPM)
    (root / "GITHUB_TOKEN").write_text(PROJECT_GITHUB)
    write_credential_context(migrated, "webshop")
    assert (root / "NPM_TOKEN" / "VALUE").read_text() == NPM
    assert (root / "GITHUB" / "TOKEN").read_text() == PROJECT_GITHUB
    assert not (root / "GITHUB_TOKEN").exists() and not (root / "SENTRY_TOKEN").exists()

    # 再打开一次不再改动
    before = [tuple(row) for row in migrated._query("SELECT id, data, value FROM credentials")]
    assert Store(seeded.path)._migrate_credentials_to_environments() == 0
    assert [tuple(row) for row in Store(seeded.path)._query(
        "SELECT id, data, value FROM credentials")] == before


def test_legacy_github_token_merges_into_an_existing_github_environment(seeded):
    # 上一版里人已经建了 GITHUB 环境(没有 TOKEN):旧凭据并进去,环境原有变量保留
    save_environment(seeded, project_id="", name="GITHUB", description="自建 GHE", variables=[
        {"name": "API_URL", "secret": False, "value": "https://ghe.example.com"}])
    # 项目里的 GITHUB 环境自己已有 TOKEN:保留它,旧凭据按普通规则迁,值不丢
    _secret(seeded, "webshop", "GITHUB", PROJECT_GITHUB)
    _legacy_row(seeded, "", "GITHUB_TOKEN", GITHUB)
    _legacy_row(seeded, "webshop", "GITHUB_TOKEN", NPM)

    migrated = Store(seeded.path)
    merged = migrated.get_credential("", "GITHUB")
    assert [item.name for item in merged.variables] == ["API_URL", "TOKEN"]
    assert merged.description == "自建 GHE" and merged.has_value
    assert github_token(migrated, "") == (GITHUB, "global")
    assert github_token(migrated, "webshop") == (PROJECT_GITHUB, "project")
    assert migrated.credential_secrets("webshop", "GITHUB_TOKEN") == {"VALUE": NPM}


WEB_JS = Path(__file__).resolve().parents[1] / "missioncrew" / "web" / "js"

# 在 node 里装载 markdown.js 与 router.js:渲染一条聊天 Markdown 链接看它挂了哪个点击
# 处理函数,再执行点击,观察是按页面跳转、按路由解析成哪个页签,还是被当成文档去打开
_SETTINGS_LINK_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const calls = [];
const context = {
  location: { origin: "http://localhost", pathname: "/webshop/chat", hash: "" },
  window: { addEventListener() {} },
  localStorage: { getItem: () => null },
  history: { pushState: (_, __, url) => { calls.push(["push", url]);
                                          context.location.pathname = url; } },
  toast: message => calls.push(["toast", message]),
  esc: value => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                             .replace(/"/g, "&quot;"),
  overview: { projects: [{ id: "webshop" }] },
  currentProject: "webshop",
  currentTab: "chat",
};
vm.createContext(context);
for (const name of ["markdown.js", "router.js"])
  vm.runInContext(fs.readFileSync(process.argv[1] + "/" + name, "utf8"), context);
context.applyRoute = async () => {
  const route = context.parsePath();
  calls.push(["route", route.project, route.tab, route.section]);
};
context.openMarkdownDocumentLink = (_, path) => { calls.push(["document", path]); return false; };
const results = JSON.parse(fs.readFileSync(0, "utf8")).map(href => {
  calls.length = 0;
  const html = context.markdownInline(`[link](${href})`);
  const handler = (html.match(/onclick="return (\w+)\(/) || [])[1] || "";
  const target = (html.match(/data-[a-z]+-link="([^"]*)"/) || [])[1] || "";
  const event = { preventDefault() {} };
  if (handler) context[handler](event, target);
  return { handler, calls: calls.slice() };
});
process.stdout.write(JSON.stringify(results));
"""


def test_settings_links_in_chat_open_the_settings_page_not_a_document():
    """聊天里指向项目设置的链接:资源 URL 直达凭据分区;页面地址(旧回执与地址栏复制的
    `/<项目>/proj`)也按页面打开,而不是被当成文档库路径报"找不到项目文档"。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the browser router")
    hrefs = ["/resources/webshop/settings/credentials", "/resources/webshop/settings",
             "/webshop/proj", "/webshop/settings", "/notes/proj", "/webshop/proj/readme.md"]
    result = subprocess.run(
        [node, "-e", _SETTINGS_LINK_SCRIPT, str(WEB_JS)],
        input=json.dumps(hrefs), text=True, capture_output=True, check=True)
    section, page, legacy, global_settings, unknown_project, document = json.loads(result.stdout)

    assert section == {"handler": "openMissionCrewResourceLink", "calls": [
        ["push", "/resources/webshop/settings/credentials"],
        ["route", "webshop", "proj", "credentials"]]}
    assert page["calls"][-1] == ["route", "webshop", "proj", None]
    assert legacy == {"handler": "openMissionCrewPageLink", "calls": [
        ["push", "/webshop/proj"], ["route", "webshop", "proj", None]]}
    assert global_settings["calls"][-1] == ["route", "webshop", "settings", None]
    # 第一段不是项目,或后面还有路径:仍是文档库路径
    assert unknown_project == {"handler": "openMissionCrewPageLink",
                               "calls": [["document", "/notes/proj"]]}
    assert document == {"handler": "openMarkdownDocumentLink",
                        "calls": [["document", "/webshop/proj/readme.md"]]}
