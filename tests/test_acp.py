"""ACP stdio 协议:客户端流程、权限自动决策、适配器路由。"""
import sys
import threading
import time
from pathlib import Path

from missioncrew.runtime import adapters
from missioncrew.runtime import acp
from missioncrew.runtime import runtime_manager
from missioncrew.runtime.acp import _pick_permission_option
from missioncrew.core.models import Backend, ExecutionConfig

FAKE = str(Path(__file__).parent / "fake_acp_agent.py")


def _cfg(tmp_path, backend):
    return ExecutionConfig(task_id="t", stage_name="chat", backend=backend,
                           prompt="# 聊天协作请求\n修一下登录问题",
                           workdir=str(tmp_path), timeout=30)


def test_acp_end_to_end_with_permission(tmp_path):
    """完整协议轮:收集文本块 + 自动选择 allow_once 权限项。"""
    backend = Backend(id="qoder", name="q", adapter="qoder",
                      command=[sys.executable, FAKE])
    result = adapters.get_adapter("qoder").run(_cfg(tmp_path, backend))
    assert result.success, result.summary
    assert "ACP 收到任务" in result.output
    assert "权限选择=yes-once" in result.output   # 选了 allow_once,而不是 reject


def test_acp_set_model_flows_through(tmp_path):
    backend = Backend(id="kimi", name="k", adapter="kimi", model="k2",
                      command=[sys.executable, FAKE])
    result = adapters.get_adapter("kimi").run(_cfg(tmp_path, backend))
    assert result.success
    assert "模型=k2" in result.output


def test_acp_process_start_failure_is_reported(tmp_path):
    backend = Backend(id="trae", name="t", adapter="trae",
                      command=["/nonexistent/acp-tool"])
    result = adapters.get_adapter("trae").run(_cfg(tmp_path, backend))
    assert not result.success
    assert "启动失败" in result.summary


def test_acp_tools_use_acp_adapter():
    for name in ("kimi", "kiro", "qoder", "trae"):
        assert isinstance(adapters.get_adapter(name), adapters.AcpAdapter)
    assert isinstance(adapters.get_adapter("claude_code"), adapters.CliAdapter)


def test_permission_option_preference():
    opts = [{"optionId": "always", "kind": "allow_always"},
            {"optionId": "once", "kind": "allow_once"}]
    assert _pick_permission_option(opts) == "once"          # 单次优先于长期
    assert _pick_permission_option(
        [{"optionId": "no", "kind": "reject_once"}]) == "no"  # 无允许项选单次拒绝
    assert _pick_permission_option(
        [{"optionId": "never", "kind": "reject_always"}]) is None  # 永久拒绝不可选
    assert _pick_permission_option(opts + [
        {"optionId": "no", "kind": "reject_once"}], "deny") == "no"


def test_acp_list_models_from_config_options():
    from missioncrew.runtime import acp
    models = acp.list_models([sys.executable, FAKE], timeout=15)
    assert models == ["fake/base", "fake/pro"]


def test_acp_list_models_from_trae_models_block():
    """trae 形态:目录在 models.availableModels(无 configOptions)。"""
    from missioncrew.runtime import acp
    models = acp.list_models([sys.executable, FAKE, "trae"], timeout=15)
    assert models == ["GLM-5.2", "Kimi-K2.6"]


def test_acp_one_shot_execution_appears_in_runtime_status(tmp_path):
    result = {}

    def run():
        result["value"] = acp.run_prompt(
            [sys.executable, FAKE, "slow"], "work", str(tmp_path), {},
            timeout=10, runtime_id="kimi", task_id="task",
            stage_name="research")

    worker = threading.Thread(target=run)
    worker.start()
    instances = []
    for _ in range(100):
        instances = acp.active_instances("kimi")
        if instances:
            break
        time.sleep(0.01)
    try:
        assert len(instances) == 1
        instance = instances[0]
        assert instance.mode == "one_shot"
        assert instance.transport == "acp-stdio"
        assert instance.state == "running" and instance.pid
        assert (instance.task_id, instance.stage_name) == ("task", "research")
    finally:
        worker.join(timeout=10)
        acp.close_sessions()
    assert result["value"][0] is True


def _chat_cfg(tmp_path, saved, emit=None, shape="config"):
    backend = Backend(id="kimi", name="k", adapter="kimi",
                      command=[sys.executable, FAKE, shape])
    return ExecutionConfig(
        task_id="chat", stage_name="chat", backend=backend,
        prompt="公共\n恢复历史\n当前任务", workdir=str(tmp_path), timeout=30,
        session_key="channel::role", session_id=saved.get("id", ""),
        common_prompt="公共上下文", turn_prompt="当前任务",
        recovery_prompt="最近对话\n当前任务", context_version="v1",
        save_session=lambda session_id, context: saved.update(
            id=session_id, context=context), emit=emit,
    )


def test_acp_reuses_one_live_session_for_multiple_turns(tmp_path):
    saved = {}
    first_events = []
    try:
        first = adapters.AcpAdapter("kimi").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: first_events.append((kind, text))))
        assert first.success
        assert "轮次=1;new=1;load=0" in first.output
        assert saved["id"] == "s-test"
        assert ("input", "公共上下文\n最近对话\n当前任务") in first_events
        active = acp.active_instances("kimi")
        assert len(active) == 1 and active[0].mode == "persistent"
        assert active[0].state == "idle" and active[0].session_key == "channel::role"
        second_events = []
        second = adapters.AcpAdapter("kimi").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: second_events.append((kind, text))))
        assert second.success
        assert "轮次=2;new=1;load=0" in second.output
        assert ("input", "公共上下文\n当前任务") in second_events
        assert all("最近对话" not in text for kind, text in second_events
                   if kind == "input")
    finally:
        acp.close_sessions()


def test_runtime_manager_stops_acp_live_session(tmp_path):
    saved = {}
    backend = Backend(id="kimi-stop", name="k", adapter="kimi",
                      command=[sys.executable, FAKE])
    config = _chat_cfg(tmp_path, saved)
    config.backend = backend
    try:
        assert runtime_manager.start(config).success
        assert runtime_manager.stop(backend, "channel::role") == 1
    finally:
        acp.close_sessions()


def test_acp_loads_persisted_session_after_process_restart(tmp_path):
    saved = {}
    try:
        first = adapters.AcpAdapter("kimi").run(_chat_cfg(tmp_path, saved))
        assert first.success and saved["id"] == "s-test"
        acp.close_sessions()  # 模拟 MissionCrew 服务进程重启后内存会话消失
        events = []
        second = adapters.AcpAdapter("kimi").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: events.append((kind, text))))
        assert second.success
        assert "轮次=1;new=0;load=1" in second.output
        assert ("input", "公共上下文\n当前任务") in events
    finally:
        acp.close_sessions()


def test_acp_without_load_capability_starts_recovery_session(tmp_path):
    saved = {"id": "persisted-session", "context": "v1"}
    events = []
    try:
        result = adapters.AcpAdapter("kimi").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: events.append((kind, text)),
                      shape="noload"))
        assert result.success
        assert "轮次=1;new=1;load=0" in result.output
        assert ("input", "公共上下文\n最近对话\n当前任务") in events
        assert any(kind == "status" and "未声明 session/load" in text
                   for kind, text in events)
    finally:
        acp.close_sessions()
