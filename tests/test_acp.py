"""ACP stdio 协议:客户端流程、权限自动决策、适配器路由。"""
import sys
from pathlib import Path

from missioncrew.runtime import adapters
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


def test_acp_list_models_from_config_options():
    from missioncrew.runtime import acp
    models = acp.list_models([sys.executable, FAKE], timeout=15)
    assert models == ["fake/base", "fake/pro"]


def test_acp_list_models_from_trae_models_block():
    """trae 形态:目录在 models.availableModels(无 configOptions)。"""
    from missioncrew.runtime import acp
    models = acp.list_models([sys.executable, FAKE, "trae"], timeout=15)
    assert models == ["GLM-5.2", "Kimi-K2.6"]
