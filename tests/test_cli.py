import json

from typer.testing import CliRunner

from missioncrew import cli


def test_chat_send_human_bracket_mention_dispatches(seeded):
    """人类 CLI 的 @[角色] 仍是显式派发语法;普通 @角色 只是正文。"""
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["chat", "send", "@[dev] 检查购物车,@reviewer 只是引用。"])
    assert result.exit_code == 0, result.output
    runs = [row["role_id"] for row in
            seeded._query("SELECT role_id FROM chat_runs")]
    assert runs == ["dev"]           # reviewer 未被普通 @ 触发
    stored = seeded.list_messages("general")[0]
    assert stored["content"].startswith("@dev ")   # 方括号归一化为可见提及
    assert [span["role_id"] for span in
            json.loads(stored["mention_spans"])] == ["dev"]


def test_serve_listens_on_all_ipv4_interfaces_by_default(monkeypatch):
    called = {}

    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: called.update(kwargs))

    cli.serve()

    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8321
