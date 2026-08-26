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


def _adapter(name: str, *extra: str) -> adapters.AcpAdapter:
    return adapters.AcpAdapter(name, [sys.executable, FAKE, *extra])


def test_acp_end_to_end_with_permission(tmp_path):
    """完整协议轮:收集文本块 + 自动选择 allow_once 权限项。"""
    backend = Backend(id="qoder", name="q", adapter="qoder")
    result = _adapter("qoder").run(_cfg(tmp_path, backend))
    assert result.success, result.summary
    assert "ACP 收到任务" in result.output
    assert "权限选择=yes-once" in result.output   # 选了 allow_once,而不是 reject


def test_acp_set_model_flows_through(tmp_path):
    backend = Backend(id="kimi", name="k", adapter="kimi", model="k2")
    result = _adapter("kimi").run(_cfg(tmp_path, backend))
    assert result.success
    assert "模型=k2" in result.output


def test_acp_execution_has_no_default_deadline(tmp_path):
    backend = Backend(id="grok", name="g", adapter="grok_build")
    config = ExecutionConfig(
        task_id="chat", stage_name="chat", backend=backend,
        prompt="keep working", workdir=str(tmp_path))

    result = _adapter("grok_build", "slow").run(config)

    assert config.timeout is None
    assert result.success and "ACP 收到任务" in result.output


def test_acp_keeps_run_active_until_detached_task_finishes(tmp_path):
    """Runtime 把后台任务藏在 rawInput/rawOutput 时，阶段性响应不能结束 run。"""
    saved = {}
    events = []
    continuation_started = threading.Event()
    result = {}

    def emit(kind, text):
        events.append((kind, text))
        if kind == "status" and "继续等待后台任务" in text:
            continuation_started.set()

    def run():
        result["value"] = _adapter("kimi", "detached").run(
            _chat_cfg(tmp_path, saved, emit))

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert continuation_started.wait(timeout=10)
        instances = acp.active_instances("kimi")
        assert len(instances) == 1
        assert instances[0].state == "running"
    finally:
        worker.join(timeout=10)
        acp.close_sessions()

    execution = result["value"]
    assert execution.success, execution.summary
    assert "后台任务已启动" in execution.output
    assert "后台任务结果=completed" in execution.output
    assert any(kind == "status" and "fake-background-1" in text
               for kind, text in events)
    # 兼容续接是 Runtime 内部生命周期，不伪装成第二条用户输入事件。
    assert len([event for event in events if event[0] == "input"]) == 1


def test_acp_process_start_failure_is_reported(tmp_path):
    backend = Backend(id="trae", name="t", adapter="trae")
    result = adapters.AcpAdapter(
        "trae", ["/nonexistent/acp-tool"],
    ).run(_cfg(tmp_path, backend))
    assert not result.success
    assert "启动失败" in result.summary


def test_acp_tools_use_acp_adapter():
    for name in ("grok_build", "kimi", "kiro", "qoder", "trae"):
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


def test_acp_catalog_reads_per_model_reasoning_efforts():
    """grok 形态:同一次 session/new 里既有模型目录,也有每个模型的档位。

    按模型档位是厂商私有扩展,解析回调来自工具的 clis 声明,协议层只透传。
    """
    from missioncrew.runtime import acp
    from missioncrew.runtime.clis import grok
    models, efforts = acp.list_model_catalog(
        [sys.executable, FAKE, "efforts"], timeout=15,
        parse_efforts=grok.parse_model_efforts)

    assert models == ["fake-4.6", "fake-4.5", "fake-mini"]
    # 协议原样顺序(高到低),规范排序由 adapters 层负责
    assert efforts == {"fake-4.6": ["xhigh", "high", "medium", "low"],
                       "fake-4.5": ["high", "medium", "low"]}
    assert "fake-mini" not in efforts   # 明确不支持的模型不入表


def test_acp_catalog_empty_efforts_without_parser():
    """既无解析回调、也不自报 thought_level 的工具(trae 等)档位表恒为空
    = 回退静态档位。"""
    from missioncrew.runtime import acp
    models, efforts = acp.list_model_catalog(
        [sys.executable, FAKE, "trae"], timeout=15)

    assert models == ["GLM-5.2", "Kimi-K2.6"] and efforts == {}


def test_acp_catalog_reads_thought_level_per_model_from_config_options():
    """kimi 形态:档位是 ACP 标准会话配置项(category=thought_level),取值随
    模型变化,session/new 只给当前模型的;探测在同一会话里逐模型切换读回。"""
    from missioncrew.runtime import acp
    models, efforts = acp.list_model_catalog([sys.executable, FAKE], timeout=15)

    assert models == ["fake/base", "fake/pro"]
    # 协议原样顺序;规范排序由 adapters 层负责
    assert efforts == {"fake/base": ["low", "high", "max"],
                       "fake/pro": ["on", "high"]}


def test_kimi_model_catalog_sorts_thought_levels(monkeypatch):
    """经 adapters 走 kimi 的通用 ACP 探测:不需要解析钩子,档位按
    EFFORT_ORDER 规范化(K2.7 的 on 排在 high 之前)。"""
    monkeypatch.setitem(
        adapters.ACP_SERVE_COMMANDS, "kimi", [sys.executable, FAKE])
    backend = Backend(id="kimi", name="k", adapter="kimi")
    models, efforts = adapters.list_runtime_model_catalog(backend, timeout=15)

    assert models == ["fake/base", "fake/pro"]
    assert efforts == {"fake/base": ["low", "high", "max"],
                       "fake/pro": ["on", "high"]}
    # 静态兜底 = K3 的档位;按模型清单优先于静态兜底
    assert runtime_manager.effort_options(backend) == ["low", "high", "max"]
    assert runtime_manager.effort_options(
        backend, "fake/pro", efforts) == ["on", "high"]


def _effort_cfg(tmp_path, backend, effort, events=None):
    return ExecutionConfig(
        task_id="t", stage_name="chat", backend=backend,
        prompt="# 聊天协作请求\n修一下登录问题", workdir=str(tmp_path),
        timeout=30, effort=effort,
        emit=(lambda kind, text: events.append((kind, text)))
        if events is not None else None)


def test_acp_effort_switches_thought_level_in_session(tmp_path):
    """kimi 的 effort 不在 serve 命令里:每轮先 set_model 再
    set_config_option(thinking),同一会话内切换档位。"""
    backend = Backend(id="kimi", name="k", adapter="kimi", model="fake/base")
    result = _adapter("kimi").run(_effort_cfg(tmp_path, backend, "max"))

    assert result.success, result.summary
    assert "模型=fake/base" in result.output and "思考=max" in result.output


def test_acp_effort_rejected_by_runtime_fails_the_turn(tmp_path):
    """越界档位(K2.7 形态的模型没有 max)由 Runtime 报错,本轮失败并把
    原因回流,不静默回落。"""
    backend = Backend(id="kimi", name="k", adapter="kimi", model="fake/pro")
    result = _adapter("kimi").run(_effort_cfg(tmp_path, backend, "max"))

    assert not result.success
    assert "effort=max" in result.output
    assert "Unknown thinking value: max" in result.output


def test_acp_effort_in_serve_command_is_not_sent_over_protocol(tmp_path):
    """grok/copilot 形态:effort 已随 {effort} 占位符进 serve 命令,协议层
    不再发 set_config_option,也不告警。"""
    events = []
    backend = Backend(id="grok", name="g", adapter="grok_build")
    result = _adapter("grok_build", "efforts", "--effort", "{effort}").run(
        _effort_cfg(tmp_path, backend, "high", events))

    assert result.success, result.summary
    assert "思考=" not in result.output
    statuses = "".join(t for k, t in events if k == "status")
    assert "未生效" not in statuses
    assert "--effort high" in "".join(t for k, t in events if k == "command")


def test_acp_effort_without_thought_level_option_warns(tmp_path):
    """既不在命令里、Runtime 也不自报 thought_level 的组合:本轮照常执行,
    但明确上报 effort 未生效,不静默吞掉配置。"""
    events = []
    backend = Backend(id="trae", name="t", adapter="trae")
    result = _adapter("trae", "trae").run(
        _effort_cfg(tmp_path, backend, "high", events))

    assert result.success, result.summary
    statuses = "".join(t for k, t in events if k == "status")
    assert "effort=high 未生效" in statuses


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


def _chat_cfg(tmp_path, saved, emit=None):
    backend = Backend(id="kimi", name="k", adapter="kimi")
    return ExecutionConfig(
        task_id="chat", stage_name="chat", backend=backend,
        prompt="公共\n恢复历史\n当前任务", workdir=str(tmp_path), timeout=30,
        project_id="project-a", role_id="lead", trigger_message_id=77,
        session_key="channel::role", session_id=saved.get("id", ""),
        common_prompt="公共上下文", turn_prompt="当前任务",
        recovery_prompt="最近对话\n当前任务", context_version="v1",
        save_session=lambda session_id, context, **stats: saved.update(
            id=session_id, context=context, **stats), emit=emit,
    )


def test_acp_reuses_one_live_session_for_multiple_turns(tmp_path):
    saved = {}
    first_events = []
    try:
        first = _adapter("kimi").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: first_events.append((kind, text))))
        assert first.success
        assert "轮次=1;new=1;load=0" in first.output
        assert saved["id"] == "s-test"
        assert ("input", "公共上下文\n最近对话\n当前任务") in first_events
        active = acp.active_instances("kimi")
        assert len(active) == 1 and active[0].mode == "persistent"
        assert active[0].state == "idle" and active[0].session_key == "channel::role"
        assert (active[0].project_id, active[0].role_id) == ("project-a", "lead")
        second_events = []
        second = _adapter("kimi").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: second_events.append((kind, text))))
        assert second.success
        assert "轮次=2;new=1;load=0" in second.output
        lean_input = (adapters.LEAN_TURN_TEMPLATE.format(context_version="v1")
                      + "当前任务")
        assert ("input", lean_input) in second_events
        assert saved["turn_mode"] == "lean" and saved["turn_bytes"] > 0
        assert all("最近对话" not in text and "公共上下文\n" not in text
                   for kind, text in second_events if kind == "input")
    finally:
        acp.close_sessions()


def test_acp_signature_ignores_legacy_per_run_environment(tmp_path):
    first = {"MISSIONCREW_AGENT_RUN_ID": "41",
             "MISSIONCREW_AGENT_TOKEN_FILE": "/stable/token"}
    second = {"MISSIONCREW_AGENT_RUN_ID": "42",
              "MISSIONCREW_AGENT_TOKEN_FILE": "/stable/token"}
    changed_path = {**second, "MISSIONCREW_AGENT_TOKEN_FILE": "/other/token"}

    assert acp._client_signature(["agent"], str(tmp_path), first) == \
        acp._client_signature(["agent"], str(tmp_path), second)
    assert acp._client_signature(["agent"], str(tmp_path), second) != \
        acp._client_signature(["agent"], str(tmp_path), changed_path)


def test_acp_drops_provider_marked_replay_notifications():
    client = object.__new__(acp._AcpClient)
    client.chunks = []
    events = []
    client.emit = lambda kind, text: events.append((kind, text))

    client._handle({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "persisted",
            "_meta": {"isReplay": True, "eventId": "old-event"},
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "historical"},
            },
        },
    })

    assert client.chunks == [] and events == []


def test_runtime_manager_stops_acp_live_session(tmp_path, monkeypatch):
    saved = {}
    backend = Backend(id="kimi-stop", name="k", adapter="kimi")
    config = _chat_cfg(tmp_path, saved)
    config.backend = backend
    monkeypatch.setitem(
        adapters.ACP_SERVE_COMMANDS, "kimi", [sys.executable, FAKE])
    try:
        assert runtime_manager.start(config).success
        assert runtime_manager.stop(backend, "channel::role") == 1
    finally:
        acp.close_sessions()


def test_acp_loads_persisted_session_after_process_restart(tmp_path):
    saved = {}
    try:
        first = _adapter("kimi").run(_chat_cfg(tmp_path, saved))
        assert first.success and saved["id"] == "s-test"
        acp.close_sessions()  # 模拟 MissionCrew 服务进程重启后内存会话消失
        events = []
        second = _adapter("kimi").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: events.append((kind, text))))
        assert second.success
        assert "轮次=1;new=0;load=1" in second.output
        # session/load 恢复了包含公共上下文的完整会话,版本未变仍走增量回合
        assert ("input", adapters.LEAN_TURN_TEMPLATE.format(context_version="v1")
                + "当前任务") in events
    finally:
        acp.close_sessions()


def test_grok_load_disables_historical_replay(tmp_path):
    saved = {"id": "persisted-session", "context": "v1"}
    config = _chat_cfg(tmp_path, saved)
    config.backend = Backend(id="grok", name="g", adapter="grok_build")
    events = []
    config.emit = lambda kind, text: events.append((kind, text))
    try:
        result = _adapter("grok_build", "replay").run(config)
        assert result.success
        assert "load=1;noReplay=1" in result.output
        assert all("不应进入当前回合的历史" not in text
                   for _kind, text in events)
    finally:
        acp.close_sessions()


def test_acp_without_load_capability_starts_recovery_session(tmp_path):
    saved = {"id": "persisted-session", "context": "v1"}
    events = []
    try:
        result = _adapter("kimi", "noload").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: events.append((kind, text))))
        assert result.success
        assert "轮次=1;new=1;load=0" in result.output
        assert ("input", "公共上下文\n最近对话\n当前任务") in events
        assert any(kind == "status" and "未声明 session/load" in text
                   for kind, text in events)
    finally:
        acp.close_sessions()


def test_client_terminal_methods_roundtrip(tmp_path):
    """terminal/* 全流程:创建、等待退出、读输出、释放、未知 id 报错。"""
    client = acp._AcpClient(["cat"], str(tmp_path), {}, timeout=5)
    responses = []
    client._write = lambda obj: responses.append(obj)   # 截获发往 agent 的应答
    try:
        client._handle_terminal_request(
            {"id": 1}, "terminal/create", {"command": "echo TERMINAL_OK"})
        terminal_id = responses[-1]["result"]["terminalId"]
        assert client.live_terminal_count() in (0, 1)   # 命令可能已瞬时退出

        client._handle_terminal_request(
            {"id": 2}, "terminal/wait_for_exit", {"terminalId": terminal_id})
        deadline = time.time() + 5
        while len(responses) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert responses[-1]["id"] == 2
        assert responses[-1]["result"]["exitCode"] == 0

        client._handle_terminal_request(
            {"id": 3}, "terminal/output", {"terminalId": terminal_id})
        assert "TERMINAL_OK" in responses[-1]["result"]["output"]
        assert responses[-1]["result"]["exitStatus"]["exitCode"] == 0

        client._handle_terminal_request(
            {"id": 4}, "terminal/release", {"terminalId": terminal_id})
        assert client._terminals == {}
        client._handle_terminal_request(
            {"id": 5}, "terminal/output", {"terminalId": terminal_id})
        assert "error" in responses[-1]
    finally:
        client.close()


def test_acp_client_terminal_background_wake(tmp_path, monkeypatch):
    """后台命令进客户端终端,turn 立即结束;终端退出后 agent 的自发汇报
    经唤醒管线整体交付,携带触发任务与会话定位。"""
    monkeypatch.setattr(acp, "_WAKE_DEBOUNCE_SECONDS", 0.3)
    woken = []
    acp.set_wake_handler(woken.append)
    saved = {}
    events = []
    try:
        result = _adapter("kimi", "terminal").run(
            _chat_cfg(tmp_path, saved, lambda kind, text: events.append((kind, text))))
        assert result.success, result.summary
        assert "后台命令已交给客户端终端" in result.output
        # 自发汇报不混入本轮 run 的输出
        assert "自发汇报" not in result.output

        deadline = time.time() + 8
        while not woken and time.time() < deadline:
            time.sleep(0.05)
        assert woken, "wake handler 未被调用"
        payload = woken[0]
        assert payload["runtime"] == "acp"
        assert payload["session_key"] == "channel::role"
        assert payload["role_id"] == "lead"
        assert "FAKE_TERMINAL_DONE" in payload["output"]
        assert payload["tasks"] and payload["tasks"][0]["status"] == "completed"
        # 唤醒任务带回启动该任务的触发消息,派发语义据此继承
        assert payload["tasks"][0]["origin_trigger"] == 77
        assert any(kind == "text" for kind, _ in payload["events"])
    finally:
        acp.set_wake_handler(None)
        acp.close_sessions()


def test_idle_cleanup_spares_clients_with_live_terminals(tmp_path):
    """带存活客户端终端的长驻会话不被空闲回收;终端退出后正常回收。"""
    client = acp._AcpClient(["cat"], str(tmp_path), {}, timeout=5)
    client._write = lambda obj: None
    key = "cleanup-test::role"
    try:
        client._handle_terminal_request(
            {"id": 1}, "terminal/create", {"command": "sleep 30"})
        assert client.live_terminal_count() == 1
        live = acp._LiveSession(client, "kimi", "s1", ("sig",),
                                last_used=time.time() - 7200)
        with acp._LIVE_SESSIONS_GUARD:
            acp._LIVE_SESSIONS[key] = live
        acp._cleanup_idle_sessions()
        with acp._LIVE_SESSIONS_GUARD:
            assert key in acp._LIVE_SESSIONS
        assert acp.active_instances("kimi")[0].background_tasks == 1

        for terminal in list(client._terminals.values()):
            terminal.kill()
        deadline = time.time() + 5
        while client.live_terminal_count() and time.time() < deadline:
            time.sleep(0.05)
        acp._cleanup_idle_sessions()
        with acp._LIVE_SESSIONS_GUARD:
            assert key not in acp._LIVE_SESSIONS
    finally:
        with acp._LIVE_SESSIONS_GUARD:
            acp._LIVE_SESSIONS.pop(key, None)
        client.close()
