"""GitHub 身份:GITHUB 环境的 TOKEN 变成 gh / git 的登录状态,以及 token 核对。"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from missioncrew.api import create_app
from missioncrew.collab import github as github_mod
from missioncrew.collab.automations import AutomationService, save_automation
from missioncrew.collab.chat import ChatEngine
from missioncrew.collab.credentials import (credential_context_dir, delete_credential,
                                            save_environment, write_credential_context)
from missioncrew.collab.github import (GitHubError, GitHubResponse, github_cli_dir,
                                       github_env, github_status, github_token,
                                       inspect_token, token_kind)

CLASSIC = "ghp_" + "a1B2c3D4e5" * 4
FINE = "github_pat_" + "Z9y8X7w6V5" * 6


def save_github_token(store, project_id: str, value: str):
    """把 token 存成 GitHub 身份:环境 GITHUB 的 secret 变量 TOKEN。"""
    return save_environment(store, project_id=project_id, name="GITHUB",
                            variables=[{"name": "TOKEN", "value": value}])



def _user_transport(scopes="repo, notifications", status=200, calls=None):
    """假的 GitHub:只认 GET /user,按 token 类型决定是否带 scope 头。"""
    def transport(method, url, headers, timeout):
        if calls is not None:
            calls.append((method, url))
        assert url == "https://api.github.com/user"
        token = headers["Authorization"].removeprefix("Bearer ")
        if status != 200:
            return GitHubResponse(status, {}, json.dumps({"message": "Bad credentials"}).encode())
        response_headers = {"github-authentication-token-expiration": "2027-01-01 00:00:00 UTC"}
        if not token.startswith("github_pat_"):
            response_headers["x-oauth-scopes"] = scopes
        return GitHubResponse(200, response_headers, json.dumps({
            "login": "crew-bot", "name": "Crew Bot",
            "html_url": "https://github.com/crew-bot"}).encode())
    return transport


def _run(store, chat: ChatEngine, role_id: str = "dev"):
    channel = store.get_channel("general")
    root_id = store.add_message(channel.id, "human", "human", f"@{role_id} 执行", [role_id])
    run_id = store.add_chat_run(channel.id, role_id, root_id, root_id, 0)
    store.update_chat_run(run_id, "running", backend_id="std-1")
    return chat._assemble(channel, store.get_role("webshop", role_id),
                          store.get_backend("std-1"), root_id, run_id=run_id)


def _wait_run(store, automation_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = store.list_automation_runs(automation_id, 5)
        if runs and all(run["status"] != "running" for run in runs):
            return runs[0]
        time.sleep(0.05)
    raise AssertionError("automation run did not finish in time")


# ---- token 识别与核对 ----

def test_token_kind_recognizes_pat_flavours():
    assert token_kind(CLASSIC) == "classic"
    assert token_kind(FINE) == "fine_grained"
    assert token_kind("gho_" + "x" * 36) == "oauth"
    assert token_kind("ghs_" + "x" * 36) == "app_installation"
    assert token_kind("0123456789abcdef" * 2 + "01234567") == "classic"   # 旧式 40 位十六进制
    assert token_kind("something-else") == "unknown"


def test_inspect_classic_token_reports_scopes_and_notification_access():
    result = inspect_token(CLASSIC, _user_transport("repo, read:org"))
    assert result["ok"] and result["login"] == "crew-bot" and result["kind"] == "classic"
    assert result["scopes"] == ["repo", "read:org"]
    assert result["notifications"] is True              # repo 权限覆盖通知接口
    assert result["expires_at"] == "2027-01-01 00:00:00 UTC"
    assert CLASSIC not in json.dumps(result)

    limited = inspect_token(CLASSIC, _user_transport("public_repo"))
    assert limited["ok"] and limited["notifications"] is False


def test_inspect_fine_grained_token_has_no_scopes_and_no_notifications():
    result = inspect_token(FINE, _user_transport())
    assert result["ok"] and result["kind"] == "fine_grained"
    assert result["scopes"] is None and result["notifications"] is False


def test_inspect_reports_rejected_token_and_network_failure():
    rejected = inspect_token(CLASSIC, _user_transport(status=401))
    assert not rejected["ok"] and "401" in rejected["error"]

    def offline(method, url, headers, timeout):
        raise GitHubError("无法连接 GitHub: timed out")
    failed = inspect_token(CLASSIC, offline)
    assert not failed["ok"] and "无法连接 GitHub" in failed["error"]


def test_status_follows_project_override_and_caches_by_token(seeded):
    assert github_status(seeded, "webshop") == {
        "configured": False, "scope": "", "identity": None}
    save_github_token(seeded, "", CLASSIC)
    assert github_token(seeded, "webshop") == (CLASSIC, "global")
    calls: list = []
    transport = _user_transport(calls=calls)
    first = github_status(seeded, "webshop", transport=transport)
    assert first["scope"] == "global" and first["identity"]["login"] == "crew-bot"
    github_status(seeded, "webshop", transport=transport)
    assert len(calls) == 1                               # 第二次读缓存
    github_status(seeded, "webshop", refresh=True, transport=transport)
    assert len(calls) == 2

    save_github_token(seeded, "webshop", FINE)
    overridden = github_status(seeded, "webshop", transport=transport)
    assert overridden["scope"] == "project" and overridden["identity"]["kind"] == "fine_grained"
    assert github_token(seeded, "") == (CLASSIC, "global")   # 全局视角不受项目覆盖影响


# ---- gh 配置目录与环境变量 ----

def test_cli_config_tracks_the_effective_token(seeded):
    directory = github_cli_dir("webshop")
    write_credential_context(seeded, "webshop")
    assert not directory.exists()
    assert github_env("webshop", credential_context_dir("webshop")) == {}

    save_github_token(seeded, "", CLASSIC)
    hosts = directory / "hosts.yml"
    assert f'oauth_token: "{CLASSIC}"' in hosts.read_text()
    assert directory.stat().st_mode & 0o777 == 0o700 and hosts.stat().st_mode & 0o777 == 0o600

    save_github_token(seeded, "webshop", FINE)
    assert FINE in hosts.read_text() and CLASSIC not in hosts.read_text()
    delete_credential(seeded, project_id="webshop", name="GITHUB")
    assert CLASSIC in hosts.read_text()                  # 退回全局那份
    delete_credential(seeded, project_id="", name="GITHUB")
    assert not directory.exists()                        # 没有 token 就不留登录状态


def test_env_adds_git_helper_after_existing_git_config_and_blanks_host_tokens(seeded):
    save_github_token(seeded, "", CLASSIC)
    credentials = credential_context_dir("webshop")
    env = github_env("webshop", credentials, base={
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "x",
        "GH_TOKEN": "host-token"})
    assert env["GH_CONFIG_DIR"] == str(github_cli_dir("webshop"))
    assert env["GIT_CONFIG_COUNT"] == "5" and "GIT_CONFIG_KEY_0" not in env   # 接在已有配置之后
    assert env["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_1"] == ""               # 先清掉宿主的 helper
    assert str(credentials / "GITHUB" / "TOKEN") in env["GIT_CONFIG_VALUE_2"]
    assert env["GIT_CONFIG_KEY_3"] == "credential.https://gist.github.com.helper"
    assert env["GH_TOKEN"] == "" and "GITHUB_TOKEN" not in env
    assert CLASSIC not in json.dumps(env)                # 环境变量里只有文件路径


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_git_reads_the_token_through_the_helper_at_call_time(seeded, tmp_path):
    save_github_token(seeded, "", CLASSIC)
    credentials = credential_context_dir("webshop")
    base = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path),
            "GIT_TERMINAL_PROMPT": "0"}
    env = {**base, **github_env("webshop", credentials, base=base)}

    def fill(host):
        return subprocess.run(
            ["git", "credential", "fill"], input=f"protocol=https\nhost={host}\n\n",
            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=20)

    filled = fill("github.com")
    assert filled.returncode == 0, filled.stderr
    assert f"password={CLASSIC}" in filled.stdout and "username=x-access-token" in filled.stdout
    # 替换 token 后不用重建环境:helper 每次调用都重新读文件
    save_github_token(seeded, "", FINE)
    assert f"password={FINE}" in fill("github.com").stdout
    assert f"password={FINE}" in fill("gist.github.com").stdout
    assert fill("example.com").returncode != 0           # 其他主机不受影响,也拿不到 token


@pytest.mark.skipif(shutil.which("gh") is None, reason="需要 gh")
def test_gh_treats_the_generated_config_as_logged_in(seeded, tmp_path):
    save_github_token(seeded, "", CLASSIC)
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path),
           **github_env("webshop", credential_context_dir("webshop"), base={})}
    shown = subprocess.run(["gh", "auth", "token"], env=env, cwd=tmp_path,
                           capture_output=True, text=True, timeout=20)
    assert shown.returncode == 0 and shown.stdout.strip() == CLASSIC, shown.stderr
    before = (github_cli_dir("webshop") / "hosts.yml").read_text()
    assert sorted(item.name for item in github_cli_dir("webshop").iterdir()) == ["hosts.yml"]
    assert (github_cli_dir("webshop") / "hosts.yml").read_text() == before   # gh 只读不改写


# ---- 派发与自动化 ----

def test_agent_run_gets_the_identity_without_the_token_in_prompt(seeded):
    chat = ChatEngine(seeded, max_workers=2)
    plain = _run(seeded, chat)
    assert "GH_CONFIG_DIR" not in plain.env and "GitHub identity" not in plain.common_prompt

    save_github_token(seeded, "", CLASSIC)
    config = _run(seeded, chat)
    assert config.env["GH_CONFIG_DIR"] == str(github_cli_dir("webshop"))
    assert str(github_cli_dir("webshop")) in config.allowed_dirs
    helper = config.env[f"GIT_CONFIG_VALUE_{int(config.env['GIT_CONFIG_COUNT']) - 1}"]
    assert str(Path(config.env["MISSIONCREW_CREDENTIALS_DIR"]) / "GITHUB" / "TOKEN") in helper
    assert "`GITHUB/TOKEN` is also the project's GitHub identity" in config.common_prompt
    everything = (config.prompt + config.common_prompt + config.turn_prompt
                  + json.dumps(config.env))
    assert CLASSIC not in everything
    manual = Path(config.env["MISSIONCREW_MANUAL"]).read_text()
    assert "The `GITHUB` environment's `TOKEN` is the project's GitHub identity" in manual


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_automation_script_runs_git_as_the_project_identity(seeded):
    save_github_token(seeded, "webshop", CLASSIC)
    chat = ChatEngine(seeded, max_workers=2)
    service = AutomationService(seeded, chat.agent_tools)
    automation, _ = save_automation(seeded, "webshop", id="gh-auth", script=(
        'test -f "$GH_CONFIG_DIR/hosts.yml" && echo config-ok\n'
        'printf "protocol=https\\nhost=github.com\\n\\n" | git credential fill | grep password\n'))
    service.trigger(automation)
    run = _wait_run(seeded, automation.id)
    assert run["status"] == "succeeded", run
    # helper 交出了 token,落库的输出照常脱敏
    assert run["stdout"].splitlines() == ["config-ok", "password=[credential:GITHUB.TOKEN]"]


# ---- HTTP API 与页面 ----

def test_identity_api_never_returns_the_token(seeded, monkeypatch):
    monkeypatch.setattr(github_mod, "http_transport", _user_transport())
    with TestClient(create_app()) as client:
        assert client.get("/api/github").json() == {
            "configured": False, "scope": "", "identity": None}
        assert client.get("/api/projects/missing/github").status_code == 404
        client.post("/api/credentials", json={
            "name": "GITHUB", "variables": [{"name": "TOKEN", "value": CLASSIC}]})
        global_status = client.get("/api/github").json()
        project_status = client.get("/api/projects/webshop/github?refresh=true").json()
        assert global_status["identity"]["login"] == "crew-bot"
        assert project_status["scope"] == "global"
        assert project_status["identity"]["notifications"] is True
        assert CLASSIC not in json.dumps([global_status, project_status])

        html = client.get("/").text
        js = client.get("/assets/js/settings-credentials.js").text
    assert 'id="proj-github-identity"' in html and 'id="global-github-identity"' in html
    assert "function renderGithubIdentity" in js


def test_deleting_project_removes_its_cli_config(seeded):
    with TestClient(create_app()) as client:
        client.post("/api/projects", json={"id": "tmp", "name": "Tmp"})
        save_github_token(seeded, "tmp", CLASSIC)
        assert (github_cli_dir("tmp") / "hosts.yml").exists()
        assert client.delete("/api/projects/tmp").status_code == 200
    assert not github_cli_dir("tmp").exists()
