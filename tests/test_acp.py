"""ACP stdio 协议:客户端流程、权限自动决策、适配器路由。"""
import json
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

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


def test_acp_list_models_from_models_block():
    """只有 models.availableModels(无 configOptions)的形态,如旧版 traecli。"""
    from missioncrew.runtime import acp
    models = acp.list_models([sys.executable, FAKE, "models_block"], timeout=15)
    assert models == ["GLM-5.2", "Kimi-K2.6"]


def test_acp_catalog_prefers_config_options_over_trae_combo_models():
    """traecli 0.209:models 块是「模型/档位」组合 id,目录以 configOptions 为准;
    档位由 thought_level 选项逐模型读回,没有该选项的模型不入表。"""
    models, efforts = acp.list_model_catalog(
        [sys.executable, FAKE, "trae"], timeout=15)

    assert models == ["glm-5.3", "Doubao_1_6"]
    assert efforts == {"glm-5.3": ["low", "high", "xhigh"]}


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
    """既无解析回调、也不自报 thought_level 的工具档位表恒为空
    = 回退静态档位。"""
    from missioncrew.runtime import acp
    models, efforts = acp.list_model_catalog(
        [sys.executable, FAKE, "models_block"], timeout=15)

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
    backend = Backend(id="qoder", name="q", adapter="qoder")
    result = _adapter("qoder", "models_block").run(
        _effort_cfg(tmp_path, backend, "high", events))

    assert result.success, result.summary
    statuses = "".join(t for k, t in events if k == "status")
    assert "effort=high 未生效" in statuses


def test_trae_effort_switches_reasoning_effort_option(tmp_path):
    backend = Backend(id="trae", name="t", adapter="trae", model="glm-5.3")
    result = _adapter("trae", "trae").run(_effort_cfg(tmp_path, backend, "xhigh"))

    assert result.success, result.summary
    assert "模型=glm-5.3" in result.output and "思考=xhigh" in result.output


def test_trae_effort_on_model_without_reasoning_option_fails_the_turn(tmp_path):
    backend = Backend(id="trae", name="t", adapter="trae", model="Doubao_1_6")
    result = _adapter("trae", "trae").run(_effort_cfg(tmp_path, backend, "low"))

    assert not result.success
    assert "effort=low" in result.output
    assert "Unsupported reasoning effort" in result.output


def test_acp_passes_additional_dirs_only_when_session_capability_declared(tmp_path):
    """trae 声明 sessionCapabilities.additionalDirectories:工作目录之外的授权
    目录随 session/new 传入;未声明的工具(kimi 形态)协议里不带。"""
    workdir = tmp_path / "work"
    inner = workdir / "docs"
    shared = tmp_path / "shared"
    for path in (inner, shared):
        path.mkdir(parents=True)

    def run(adapter, *extra):
        cfg = _cfg(workdir, Backend(id=adapter, name=adapter, adapter=adapter))
        cfg.allowed_dirs = [str(workdir), str(inner), str(shared)]
        return _adapter(adapter, *extra).run(cfg)

    trae = run("trae", "trae")
    assert trae.success, trae.summary
    assert f";目录={shared.resolve()}" in trae.output   # 工作根内的目录不重复授权

    kimi = run("kimi")
    assert kimi.success, kimi.summary
    assert "目录=" not in kimi.output


def test_trae_changed_dirs_restart_live_session_and_reload_with_new_dirs(tmp_path):
    """额外目录只在建会话时生效:清单变化要换进程,再 session/load 带上新清单。"""
    workdir = tmp_path / "work"
    first_dir, second_dir = tmp_path / "a", tmp_path / "b"
    for path in (workdir, first_dir, second_dir):
        path.mkdir()
    saved = {}

    def run(extra_dir):
        cfg = _chat_cfg(workdir, saved)
        cfg.backend = Backend(id="trae", name="t", adapter="trae")
        cfg.session_id = saved.get("id", "")
        cfg.allowed_dirs = [str(workdir), str(extra_dir)]
        return _adapter("trae", "trae").run(cfg)

    try:
        first = run(first_dir)
        assert first.success, first.summary
        assert "new=1;load=0" in first.output
        assert f";目录={first_dir.resolve()}" in first.output

        second = run(second_dir)
        assert second.success, second.summary
        assert "new=0;load=1" in second.output
        assert f";目录={second_dir.resolve()}" in second.output
    finally:
        acp.close_sessions()


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


def test_acp_subagent_messages_stay_out_of_reply(tmp_path):
    """并行子代理的正文分片与主代理同一 sessionId、交错到达,只能凭
    parentToolCallId 分开;并入回复会把几段 Markdown 逐字搅在一起。"""
    client = acp._AcpClient(["cat"], str(tmp_path), {}, timeout=5)
    client._write = lambda obj: None
    events = []

    def chunk(text, parent=""):
        meta = {"parentToolCallId": parent} if parent else {}
        client._handle({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1", "_meta": meta, "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text}}}})
    try:
        client.begin_turn(5, lambda kind, text: events.append((kind, text)))
        chunk("## 1. 主报告")
        chunk("# 子代理 A", "call-a")
        chunk("# 子代理 B", "call-b")
        chunk("\n## 2. 结论")
        assert "".join(client.chunks) == "## 1. 主报告\n## 2. 结论"
        assert [t for k, t in events if k == "text"] == ["## 1. 主报告", "\n## 2. 结论"]
        assert [t for k, t in events if k == "thinking"] == ["# 子代理 A", "# 子代理 B"]
        # 后台子代理在 turn 之外的正文同样不进唤醒汇报
        client.end_turn()
        chunk("# 后台子代理", "call-c")
        with client._wake_lock:
            assert client._wake_chunks == []
            assert client._wake_events == [("thinking", "# 后台子代理")]
    finally:
        client.close()


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


def test_acp_persists_session_id_once_prompt_is_sent(tmp_path):
    """首轮会话在 turn 中途被人工停止:会话 id 在 session/prompt 发出时就已
    落库,下一轮以 session/load 续接,而不是等轮末才持久化导致新建会话。"""
    saved = {}
    outcome = {}
    events = []

    def _turn():
        outcome["result"] = _adapter("kimi", "slow").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: events.append((kind, text))))

    worker = threading.Thread(target=_turn)
    try:
        worker.start()
        deadline = time.time() + 5
        while "id" not in saved and time.time() < deadline:
            time.sleep(0.02)
        assert saved.get("id") == "s-test" and saved.get("context") == "v1"
        assert worker.is_alive() and "turn_mode" not in saved
        assert acp.stop_runtime_sessions("kimi", "channel::role") == 1
        worker.join(timeout=10)
        assert not outcome["result"].success
        assert saved["id"] == "s-test"

        second_events = []
        second = _adapter("kimi").run(
            _chat_cfg(tmp_path, saved,
                      lambda kind, text: second_events.append((kind, text))))
        assert second.success
        assert "轮次=1;new=0;load=1" in second.output
        assert ("input", adapters.LEAN_TURN_TEMPLATE.format(context_version="v1")
                + "当前任务") in second_events
        assert saved["turn_mode"] == "lean"
    finally:
        worker.join(timeout=10)
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


@pytest.mark.parametrize("shape, recovered", [("load_replay", True),
                                            ("load_replay_error", False)])
@pytest.mark.parametrize("idle", [False, True])
def test_acp_load_replay_does_not_enter_output_tools_or_wake(tmp_path, shape, recovered, idle):
    events = []
    client = acp._AcpClient([sys.executable, FAKE, shape], str(tmp_path), {}, 5,
                            emit=lambda kind, text: events.append((kind, text)))
    try:
        if idle:
            client.end_turn()
        sid, loaded = acp._new_or_load_session(
            client, str(tmp_path), "persisted", acp._initialize(client))
        assert loaded is recovered
        assert client.chunks == []
        assert client.pending_detached_tasks() == {}
        assert client._wake_events == [] and client._wake_chunks == []
        assert not any(kind in ("text", "thinking", "tool") for kind, _ in events)
        client.begin_turn(5, lambda kind, text: events.append((kind, text)))
        output = acp._prompt_turn(client, sid, "fresh prompt", "")
        assert "ACP 收到任务" in output
        assert ("input", "fresh prompt") in events
        assert any(kind == "text" and "ACP 收到任务" in text for kind, text in events)
        assert "历史" not in output
    finally:
        client.close()


def test_kimi_restored_run_emits_only_current_turn(tmp_path):
    saved = {"id": "persisted", "context": "v1"}
    events = []
    try:
        result = _adapter("kimi", "load_replay").run(
            _chat_cfg(tmp_path, saved, lambda kind, text: events.append((kind, text))))
        assert result.success
        assert "load=1" in result.output
        assert not any("历史" in text for _, text in events)
        input_index = next(i for i, (kind, _) in enumerate(events) if kind == "input")
        assert not any(kind in ("text", "thinking", "tool")
                       for kind, _ in events[:input_index])
    finally:
        acp.close_sessions()


def test_acp_load_response_ends_replay_before_next_notification(tmp_path, monkeypatch):
    client = acp._AcpClient(["cat"], str(tmp_path), {}, 5)

    def text_update(text):
        client._handle({"method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text}}}})

    def respond(msg):
        text_update("history")
        client._handle({"id": msg["id"], "result": {}})
        # 请求线程尚未从 _write 返回,但 load 应答后的通知已属于实时流。
        text_update("live")

    monkeypatch.setattr(client, "_write", respond)
    try:
        client.request("session/load", {"sessionId": "persisted"})
        assert client.chunks == ["live"]
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["write", "timeout", "expired"])
def test_acp_failed_load_releases_replay_filter(tmp_path, monkeypatch, failure):
    client = acp._AcpClient(["cat"], str(tmp_path), {}, 5)

    def write(msg):
        if failure == "write":
            raise OSError("broken pipe")

    monkeypatch.setattr(client, "_write", write)
    client.deadline = time.time() + (0.01 if failure == "timeout" else -1)
    try:
        with pytest.raises(acp.AcpError):
            client.request("session/load", {"sessionId": "persisted"})
        client._handle({"method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "live"}}}})
        assert client.chunks == ["live"] and client._pending == {}
    finally:
        client.close()


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


def test_acp_spontaneous_turn_command_sees_token_issued_at_begin(tmp_path, monkeypatch):
    """端到端(真实 stdio 协议):后台命令结束后 agent 自发的一轮先思考再起
    前台命令读令牌文件。平台在首个输出处同步 begin 签发令牌,命令执行时文件
    已就位;收尾 payload 带 run_id,事件已实时写入。"""
    monkeypatch.setattr(acp, "_WAKE_DEBOUNCE_SECONDS", 0.3)
    token_file = tmp_path / "token.txt"
    binding = _WakeBinding()
    real_begin = binding.begin

    def _begin(payload):
        token_file.write_text("run-88-token\n")   # ChatEngine 在此签发令牌
        return real_begin(payload)

    binding.begin = _begin
    saved = {}
    try:
        with binding:
            result = _adapter("kimi", "terminal-token").run(_chat_cfg(tmp_path, saved))
            assert result.success, result.summary
            assert _wait_for(lambda: binding.finished, timeout=8)
        payload = binding.finished[0]
        assert payload["run_id"] == 88
        assert payload["output"] == "自发汇报:令牌=run-88-token"
        assert len(binding.begun) == 1
        # 触发任务是那条已结束的后台命令,派发语义按它的发起消息继承
        assert binding.begun[0]["tasks"][0]["origin_trigger"] == 77
        kinds = [kind for kind, _ in binding.live]
        assert kinds[0] == "thinking" and "text" in kinds
        assert any("客户端终端已创建" in text for _, text in binding.live)
    finally:
        acp.close_sessions()


def _silent_wake_run(tmp_path, monkeypatch, shape):
    """跑一轮把后台命令交给客户端终端的常规 turn,等 kimi 式的静默自发轮收尾。"""
    monkeypatch.setattr(acp, "_WAKE_DEBOUNCE_SECONDS", 0.1)
    monkeypatch.setattr(acp, "_WAKE_THINK_SECONDS", 0.3)
    monkeypatch.setattr(acp, "_WAKE_REPORT_RETRY_SECONDS", 0.2)
    binding = _WakeBinding()
    try:
        with binding:
            result = _adapter("kimi", shape).run(_chat_cfg(tmp_path, {}))
            assert result.success, result.summary
            assert _wait_for(lambda: binding.finished, timeout=10)
    finally:
        acp.close_sessions()
    assert len(binding.begun) == 1 and len(binding.finished) == 1
    return binding


def test_acp_silent_spontaneous_turn_gets_report_prompt(tmp_path, monkeypatch):
    """kimi 的自发轮只发终端请求、不推送 session/update:静默后平台在同一会话
    补发汇报 prompt,回复作为这轮的输出,过程事件直接进已落成的运行。"""
    binding = _silent_wake_run(tmp_path, monkeypatch, "silent-wake")
    payload = binding.finished[0]
    assert payload["run_id"] == 88 and payload["success"] is True
    assert payload["output"] == "补发汇报:后台命令已完成(第 1 次请求)"
    assert payload["tasks"][0]["origin_trigger"] == 77
    assert ("thinking", "回顾自发的一轮") in binding.live
    statuses = [text for kind, text in binding.live if kind == "status"]
    assert sum("补发汇报 (1/3)" in text for text in statuses) == 1
    assert not any("稍后重试" in text for text in statuses)


def test_acp_report_prompt_retries_while_runtime_is_busy(tmp_path, monkeypatch):
    """Runtime 还在自发轮里时补发的 prompt 只得到空应答:隔一段静默再试。"""
    binding = _silent_wake_run(tmp_path, monkeypatch, "silent-wake-busy")
    payload = binding.finished[0]
    assert payload["output"] == "补发汇报:后台命令已完成(第 2 次请求)"
    statuses = [text for kind, text in binding.live if kind == "status"]
    assert any("补发汇报 (2/3)" in text for text in statuses)
    assert sum("稍后重试" in text for text in statuses) == 1


def test_acp_report_prompt_gives_up_after_limit(tmp_path, monkeypatch):
    """始终拿不到正文:试满次数后按无输出收尾,由平台记失败,运行不悬挂。"""
    binding = _silent_wake_run(tmp_path, monkeypatch, "silent-wake-mute")
    payload = binding.finished[0]
    assert payload["run_id"] == 88 and payload["success"] is True
    assert payload["output"] == ""
    statuses = [text for kind, text in binding.live if kind == "status"]
    assert sum("补发汇报" in text and "/3)" in text for text in statuses) == 3


def test_acp_wake_with_reply_text_needs_no_report_prompt(tmp_path, monkeypatch):
    """自发轮自己推送了正文(Grok 形态)就按原路收尾,不补发 prompt。"""
    monkeypatch.setattr(acp, "_WAKE_DEBOUNCE_SECONDS", 0.1)
    client = _wake_client(tmp_path)
    client.session_id = "s1"
    sent = []
    client._write = lambda obj: sent.append(obj)
    try:
        with _WakeBinding() as wake:
            _update(client, _say("流水线已通过。"))
            assert _wait_for(lambda: wake.finished)
            assert wake.finished[0]["output"] == "流水线已通过。"
            assert not any(obj.get("method") == "session/prompt" for obj in sent)
    finally:
        client.close()


def test_wake_buffer_accepts_tool_call_list_content(tmp_path):
    """turn 之外的 tool_call 更新按 ACP 约定带 content 列表;唤醒缓冲不能把它当 dict
    (线上曾因此让读循环崩溃,之后该会话所有响应无人消费、运行永远 running)。"""
    client = acp._AcpClient(["cat"], str(tmp_path), {}, timeout=5)
    client._write = lambda obj: None
    try:
        client.end_turn()
        client._handle({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1", "update": {
                "sessionUpdate": "tool_call", "toolCallId": "call-1",
                "title": "Check PR pipeline", "status": "in_progress",
                "content": [{"type": "content",
                             "content": {"type": "text", "text": "gitcode pr checks"}}],
            }}})
        client._handle({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1", "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "流水线仍在排队。"},
            }}})
        with client._wake_lock:
            assert client._wake_events == [
                ("tool", "Check PR pipeline in_progress\n"),
                ("text", "流水线仍在排队。")]
            assert client._wake_chunks == ["流水线仍在排队。"]
        # turn 内同样形态的更新走工具生命周期路径,也不能抛
        client.begin_turn(5, None)
        client._handle({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1", "update": {
                "sessionUpdate": "tool_call_update", "toolCallId": "call-1",
                "status": "completed", "content": [{"type": "content",
                    "content": {"type": "text", "text": "ok"}}],
            }}})
    finally:
        client.close()


def _wake_client(tmp_path):
    """turn 已结束的长驻客户端:请求只记录不发送,会话可定位频道×角色。"""
    client = acp._AcpClient(["cat"], str(tmp_path), {}, timeout=5)
    client._write = lambda obj: None
    client.wake_meta = {"session_key": "general::lead", "backend_id": "grok",
                        "project_id": "webshop", "role_id": "lead",
                        "trigger_message_id": 11}
    client.end_turn()
    return client


def _update(client, update, meta=None):
    client._handle({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "s1", "_meta": meta or {}, "update": update}})


def _thought(text):
    return {"sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": text}}


def _say(text):
    return {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text}}


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.02)
    return predicate()


class _WakeBinding:
    """模拟 ChatEngine 的两段唤醒回调:begin 落成运行并收实时事件,handler 收尾。"""

    def __init__(self, run_id=88, trigger_id=501):
        self.run_id, self.trigger_id = run_id, trigger_id
        self.begun: list[dict] = []
        self.live: list[tuple[str, str]] = []
        self.finished: list[dict] = []

    def begin(self, payload):
        self.begun.append(payload)
        if not self.run_id:
            return None
        return {"run_id": self.run_id, "trigger_id": self.trigger_id,
                "emit": lambda kind, text: self.live.append((kind, text))}

    def __enter__(self):
        acp.set_wake_handler(self.finished.append, begin=self.begin)
        return self

    def __exit__(self, *exc):
        acp.set_wake_handler(None)


def test_acp_wake_turn_begins_run_before_its_first_command(tmp_path, monkeypatch):
    """Runtime 自发的一轮(后台命令汇报、自带定时器到点):主代理首个输出即
    落成运行并签发令牌,随后起的命令归属该运行,事件实时写入,收尾带 run_id。"""
    monkeypatch.setattr(acp, "_WAKE_DEBOUNCE_SECONDS", 0.1)
    client = _wake_client(tmp_path)
    try:
        with _WakeBinding() as wake:
            _update(client, _thought("定时提醒到点,先核对时间"))
            # 起点:同步 begin,此时还没有任何命令执行
            assert len(wake.begun) == 1
            assert wake.begun[0]["session_key"] == "general::lead"
            assert wake.begun[0]["tasks"] == []
            assert wake.live == [("thinking", "定时提醒到点,先核对时间")]
            _update(client, {"sessionUpdate": "tool_call", "toolCallId": "c1",
                             "title": "run_terminal_command", "status": "in_progress"})
            sent = []
            client._write = lambda obj: sent.append(obj)
            client._handle_terminal_request(
                {"id": 7}, "terminal/create", {"command": "echo agent-tool"})
            terminal_id = sent[-1]["result"]["terminalId"]
            terminal = client._terminals[terminal_id]
            assert terminal.wake_run_id == 88
            assert terminal.origin_trigger == 501   # 锚定该轮的平台触发消息
            assert terminal.exited.wait(5)
            # 本轮自己起的命令在本轮内结束:是过程事件,不是待汇报的后台任务
            assert _wait_for(lambda: any("客户端终端已退出" in text
                                         for _, text in wake.live))
            assert client._wake_tasks == []
            _update(client, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                             "status": "completed"})
            _update(client, _say("已重新派发 dev2。"))
            assert _wait_for(lambda: wake.finished)
            payload = wake.finished[0]
            assert payload["run_id"] == 88 and payload["success"] is True
            assert payload["output"] == "已重新派发 dev2。"
            assert payload["events"] == []   # 已实时写入,不再回放
            assert ("text", "已重新派发 dev2。") in wake.live
            assert len(wake.begun) == 1
            assert client._wake_run_id == 0
    finally:
        client.close()


def test_acp_wake_turn_starting_with_a_command_begins_first(tmp_path):
    """自发 turn 的第一个动作就是起命令(之前没有任何 update):先落成运行再执行。"""
    client = _wake_client(tmp_path)
    try:
        with _WakeBinding() as wake:
            client._handle_terminal_request(
                {"id": 1}, "terminal/create", {"command": "true"})
            assert len(wake.begun) == 1
            assert client._wake_run_id == 88
            assert any("客户端终端已创建" in text for _, text in wake.live)
    finally:
        client.close()


def test_acp_wake_residue_does_not_begin_a_run(tmp_path, monkeypatch):
    """turn 外的子代理残留与旧工具调用的状态更新不是自发 turn 的起点。"""
    monkeypatch.setattr(acp, "_WAKE_DEBOUNCE_SECONDS", 0.1)
    client = _wake_client(tmp_path)
    try:
        with _WakeBinding() as wake:
            _update(client, _say("# 后台子代理"), {"parentToolCallId": "call-c"})
            _update(client, {"sessionUpdate": "tool_call_update", "toolCallId": "old",
                             "status": "completed"})
            time.sleep(0.4)
            assert wake.begun == [] and wake.finished == []
    finally:
        client.close()


def test_acp_begun_wake_waits_for_running_tool(tmp_path, monkeypatch):
    """已落成运行的自发 turn 里工具还在跑(长测试):静默不收尾,否则令牌被撤销,
    工具跑完后的平台写操作失败;出现正文且工具结束后才按静默收尾。"""
    monkeypatch.setattr(acp, "_WAKE_DEBOUNCE_SECONDS", 0.1)
    monkeypatch.setattr(acp, "_WAKE_THINK_SECONDS", 0.6)
    client = _wake_client(tmp_path)
    try:
        with _WakeBinding() as wake:
            _update(client, _thought("跑测试"))
            _update(client, {"sessionUpdate": "tool_call", "toolCallId": "t1",
                             "title": "npm test", "status": "in_progress"})
            time.sleep(0.8)
            assert wake.finished == []
            _update(client, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                             "status": "completed"})
            # 工具刚结束、模型还在酝酿下一步:按 _WAKE_THINK_SECONDS 等
            time.sleep(0.3)
            assert wake.finished == []
            _update(client, _say("测试通过,已提交。"))
            assert _wait_for(lambda: wake.finished, timeout=2)
            assert wake.finished[0]["output"] == "测试通过,已提交。"
    finally:
        client.close()


def test_acp_wake_without_begin_binding_keeps_buffering(tmp_path, monkeypatch):
    """begin 未能落成运行(返回 None)时退回旧路径:事件缓冲,结束后整体交付。"""
    monkeypatch.setattr(acp, "_WAKE_DEBOUNCE_SECONDS", 0.1)
    client = _wake_client(tmp_path)
    try:
        with _WakeBinding(run_id=0) as wake:
            _update(client, _thought("汇报"))
            _update(client, _say("缓冲汇报"))
            assert len(wake.begun) == 1    # 同一轮只请求一次
            assert _wait_for(lambda: wake.finished)
            payload = wake.finished[0]
            assert payload["run_id"] == 0
            assert payload["events"] == [("thinking", "汇报"), ("text", "缓冲汇报")]
    finally:
        client.close()


def test_acp_begun_wake_closed_by_new_turn_or_close(tmp_path):
    """已落成的唤醒运行不悬挂:常规 turn 开始时先收尾;进程关闭时按失败收尾。"""
    client = _wake_client(tmp_path)
    try:
        with _WakeBinding() as wake:
            _update(client, _thought("还在想"))
            client.begin_turn(5, None)
            assert _wait_for(lambda: wake.finished)
            assert wake.finished[0]["run_id"] == 88
            client.end_turn()
            _update(client, _thought("又一轮"))
            assert len(wake.begun) == 2
            client.close()
            assert _wait_for(lambda: len(wake.finished) == 2)
            closed = wake.finished[1]
            assert closed["run_id"] == 88 and closed["success"] is False
            assert "已停止" in closed["error"]
    finally:
        client.close()


def test_read_loop_survives_malformed_update(tmp_path):
    """读循环里单条消息处理抛异常只记录,不能结束线程:后续响应仍要交付给等待方。"""
    client = acp._AcpClient(["cat"], str(tmp_path), {}, timeout=5)
    try:
        # cat 把写入 stdin 的行原样回显到 stdout,读循环据此消费
        client._write({"jsonrpc": "2.0", "method": "session/update",
                       "params": {"update": "not-an-object"}})
        waiter = queue.Queue()
        client._pending[4242] = waiter
        client._write({"jsonrpc": "2.0", "id": 4242, "result": {"ok": True}})
        msg = waiter.get(timeout=5)
        assert msg["result"] == {"ok": True}
    finally:
        client.close()


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


def test_idle_cleanup_spares_spontaneous_turns(tmp_path, monkeypatch):
    """后台命令结束后 Runtime 自发的汇报轮不经 run_prompt,不刷新 last_used:
    已落成运行的自发 turn 不被回收;收尾后空闲时长从最近一次自发活动算起。"""
    monkeypatch.setattr(acp, "_WAKE_DEBOUNCE_SECONDS", 0.1)
    client = _wake_client(tmp_path)
    key = "general::lead"
    live = acp._LiveSession(client, "kimi", "s1", ("sig",),
                            last_used=time.time() - 7200)
    with acp._LIVE_SESSIONS_GUARD:
        acp._LIVE_SESSIONS[key] = live

    def _pooled():
        with acp._LIVE_SESSIONS_GUARD:
            return key in acp._LIVE_SESSIONS
    try:
        with _WakeBinding() as wake:
            # 最后一个后台终端已退出,自发 turn 起了一条命令后还在想下一步
            client._handle_agent_request({"id": 1, "method": "terminal/create",
                                          "params": {"command": "true"}})
            assert client.wake_run_bound()
            assert _wait_for(lambda: client.live_terminal_count() == 0)
            acp._cleanup_idle_sessions()
            assert _pooled()

            _update(client, _say("测试通过,PR 已创建。"))
            assert _wait_for(lambda: wake.finished)
            assert not client.wake_run_bound()
            # 刚汇报完:不能按两小时前的 last_used 立刻回收
            acp._cleanup_idle_sessions()
            assert _pooled()
            assert acp.active_instances("kimi")[0].last_activity > time.time() - 60

            client._spontaneous_at = time.time() - 7200
            acp._cleanup_idle_sessions()
            assert not _pooled()
    finally:
        with acp._LIVE_SESSIONS_GUARD:
            acp._LIVE_SESSIONS.pop(key, None)
        client.close()


def _usage_events(events):
    return [json.loads(text) for kind, text in events if kind == "usage"]


def test_trae_turn_emits_unified_usage_event(tmp_path):
    """标准 usage(本轮)+ usage_update(上下文)+ 私有 _meta 会话累计归一成 usage/v1。"""
    events = []
    backend = Backend(id="trae", name="t", adapter="trae", model="glm-5.3")
    result = _adapter("trae", "trae").run(_effort_cfg(tmp_path, backend, "", events))

    assert result.success, result.summary
    [usage] = _usage_events(events)
    assert usage["schema"] == "usage/v1"
    assert usage["turn"] == {"input": 27082, "cache_read": 13504, "cache_write": 0,
                             "output": 35, "reasoning": 0, "total": 27117}
    assert usage["total"] == {"input": 40666, "cache_read": 27008,
                              "output": 54, "total": 40720}
    # 上下文占用取 usage_update.used,而不是一轮两次请求的合计 27117
    assert (usage["context_used"], usage["context_window"]) == (13564, 190000)
    assert usage["raw"]["usage_update"] == {"used": 13564, "size": 190000}
    assert "stopReason" not in usage["raw"]["responses"][0]


def test_acp_without_usage_reports_no_usage_event(tmp_path):
    """kimi 形态的响应不带用量:不发空的 usage 事件。"""
    events = []
    backend = Backend(id="kimi", name="k", adapter="kimi")
    result = _adapter("kimi").run(_effort_cfg(tmp_path, backend, "", events))

    assert result.success, result.summary
    assert _usage_events(events) == []


def test_turn_usage_sums_continuation_prompts_and_prefers_standard_cost():
    first = {"stopReason": "end_turn",
             "usage": {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110}}
    second = {"stopReason": "end_turn",
              "usage": {"inputTokens": 200, "outputTokens": 20, "totalTokens": 220}}
    context = {"sessionUpdate": "usage_update", "used": 230, "size": 1000,
               "cost": {"amount": 0.5, "currency": "USD"}}
    vendor = lambda response: {"cost_usd": 0.01, "duration_ms": 40}

    usage = acp._turn_usage([first, second], context, vendor)

    assert usage["turn"] == {"input": 300, "output": 30, "total": 330}
    assert (usage["context_used"], usage["context_window"]) == (230, 1000)
    assert usage["cost_usd"] == 0.5          # 标准会话累计费用优先
    assert usage["duration_ms"] == 80        # 续接 prompt 的耗时逐项相加


def test_turn_usage_ignores_non_usd_cost_and_failing_vendor_hook():
    def broken(response):
        raise KeyError("boom")

    usage = acp._turn_usage(
        [{"usage": {"inputTokens": 5, "outputTokens": 1, "totalTokens": 6}}],
        {"used": 6, "size": 100, "cost": {"amount": 3, "currency": "EUR"}}, broken)

    assert usage["turn"]["total"] == 6 and "cost_usd" not in usage
    assert acp._turn_usage([{"stopReason": "end_turn"}], {}, None) == {}


def test_grok_prompt_usage_reads_xai_meta():
    """grok 不填标准 usage,本轮用量、费用与耗时在 _meta(真实响应截取)。"""
    from missioncrew.runtime.clis import grok
    response = {"stopReason": "end_turn", "_meta": {
        "totalTokens": 22886, "modelId": "grok-4.7",
        "usage": {"inputTokens": 22774, "outputTokens": 103, "totalTokens": 22877,
                  "cachedReadTokens": 2176, "cacheCreationTokens": 0,
                  "reasoningTokens": 102, "modelCalls": 1, "apiDurationMs": 3258,
                  "costUsdTicks": 145866800}}}

    usage = acp._turn_usage([response], {}, grok.parse_prompt_usage)

    assert usage["turn"] == {"input": 22774, "cache_read": 2176, "cache_write": 0,
                             "output": 103, "reasoning": 102, "total": 22877}
    assert usage["context_used"] == 22886 and "context_window" not in usage
    assert usage["cost_usd"] == pytest.approx(0.01458668)
    assert usage["duration_ms"] == 3258
    assert grok.parse_prompt_usage({"stopReason": "end_turn"}) == {}


def test_usage_update_outside_turn_is_recorded_not_woken(tmp_path):
    """回合外的 usage_update 只更新上下文状态,不进自唤醒缓冲。"""
    client = acp._AcpClient(["cat"], str(tmp_path), {}, timeout=5)
    try:
        client.end_turn()
        client._handle({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s", "update": {"sessionUpdate": "usage_update",
                                         "used": 10, "size": 100}}})
        assert client.context_usage["used"] == 10
        assert client._wake_events == [] and client._wake_chunks == []
        client.begin_turn(5, None)
        assert client.context_usage == {}
    finally:
        client.close()
