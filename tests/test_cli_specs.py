"""clis/ 按工具声明的一致性:注册表由声明汇总,声明本身必须自洽。"""

from missioncrew.runtime import adapters
from missioncrew.runtime.clis import BY_ADAPTER, SPECS


def test_specs_are_unique_and_self_consistent():
    adapters_seen = [s.adapter for s in SPECS]
    assert len(adapters_seen) == len(set(adapters_seen))       # adapter 不重名
    binaries = [s.binary for s in SPECS if s.binary]
    assert len(binaries) == len(set(binaries))                 # 检测可执行名不重名
    for s in SPECS:
        # print 模板与 ACP serve 命令互斥;两者都无 = 原生 provider 或 mock
        assert not (s.command and s.acp_serve), s.adapter
        # 会话恢复方式只对 print 模式有意义
        assert s.session_id in ("", "fixed", "captured"), s.adapter
        if s.session_id:
            assert s.command, s.adapter
        # 命令模板第一个 token 应当就是检测的可执行名,防止声明拷贝走样
        template = s.command or s.acp_serve
        if template and s.binary:
            assert template[0] == s.binary, s.adapter


def test_registry_tables_are_derived_from_specs():
    """adapters 的注册表与声明一一对应,改声明即改注册表。"""
    assert set(adapters.DEFAULT_COMMANDS) == {
        s.adapter for s in SPECS if s.command}
    assert set(adapters.ACP_SERVE_COMMANDS) == {
        s.adapter for s in SPECS if s.acp_serve}
    assert [b for b, *_ in adapters.KNOWN_CLIS] == [
        s.binary for s in SPECS if s.binary]
    assert set(adapters.EFFORT_SUPPORT) == {s.adapter for s in SPECS if s.efforts}
    assert set(adapters.UPDATE_SPECS) == {s.adapter for s in SPECS if s.update}
    # 每个声明的 adapter 都能拿到执行器(mock/ACP/CLI 三选一)
    for s in SPECS:
        assert adapters.get_adapter(s.adapter) is not None
    # 工具级特例走声明,不再散在执行器里
    assert BY_ADAPTER["grok_build"].load_session_meta == {"noReplay": True}
    assert adapters._load_session_meta("grok_build") == {"noReplay": True}
    assert adapters.supports_account_usage("kimi")
    assert not adapters.supports_account_usage("codex")


def test_model_discovery_dispatches_to_spec_hooks():
    """模型发现回调随工具声明;ACP 工具无回调,走通用 session/new 探测。"""
    from missioncrew.core.models import Backend

    assert {a for a, s in BY_ADAPTER.items() if s.discover_models} == {
        "claude_code", "codex", "opencode", "mock"}
    for spec in BY_ADAPTER.values():
        if spec.acp_serve:
            assert spec.discover_models is None, spec.adapter

    # claude:静态目录;mock:工具自带清单——都经统一入口分发到声明回调
    models, efforts = adapters.list_runtime_model_catalog(
        Backend(id="c", name="c", adapter="claude_code"))
    assert models == list(adapters.CLAUDE_MODEL_CATALOG) and efforts == {}
    models, efforts = adapters.list_runtime_model_catalog(
        Backend(id="m", name="m", adapter="mock", models=["", "small"]))
    assert models == ["small"] and efforts == {}
