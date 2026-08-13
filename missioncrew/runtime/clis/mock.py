"""Mock runtime 的静态声明:仅供测试/演示走通配置链路,不参与本机检测。"""
from .spec import CliSpec


def discover_models(backend, timeout: int = 25):
    """mock 无本体可问:模型发现返回工具自带清单。"""
    return [name for name in backend.models if name], {}


SPEC = CliSpec(
    adapter="mock",
    efforts=("low", "medium", "high"),
    discover_models=discover_models,
)
