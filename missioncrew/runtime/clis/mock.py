"""Mock runtime 的静态声明:仅供测试/演示走通配置链路,不参与本机检测。"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="mock",
    efforts=("low", "medium", "high"),
)
