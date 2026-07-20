from missioncrew import cli


def test_serve_listens_on_all_ipv4_interfaces_by_default(monkeypatch):
    called = {}

    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: called.update(kwargs))

    cli.serve()

    assert called["host"] == "0.0.0.0"
    assert called["port"] == 8321
