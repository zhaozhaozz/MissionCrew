"""pi 的静态声明。

执行走原生 provider(runtime/pi.py 的 PiRuntimeProvider,effort 档位也在那里
声明);pi 是平台 vendored 安装(MC_HOME/pi/vendor),检测、模型清单与升级
都不走通用路径,由下方钩子声明。
"""
from .spec import CliSpec

_NPM_PACKAGE = "@mariozechner/pi-coding-agent"


def locate_binary() -> str:
    """只用平台 vendored 安装,不检测系统级 pi。"""
    from ...core.config import pi_vendor_bin
    vendored = pi_vendor_bin()
    return str(vendored) if vendored.is_file() else ""


def configured_models() -> list[str]:
    """pi 的执行单元与平台自有 models.json 同源(provider/model)。"""
    from ..pi import read_pi_models
    return read_pi_models()


def update_plan() -> tuple:
    """vendored 安装:更新只写平台自有 vendor 目录,绝不 -g 污染全局。"""
    from ...core.config import pi_vendor_prefix
    return ("npm", ["npm", "install", "--prefix", str(pi_vendor_prefix()),
                    "--no-fund", "--no-audit", f"{_NPM_PACKAGE}@latest"])


def private_dirs() -> list[str]:
    """pi 的平台自有主目录(agent 配置/会话/vendored 安装均重定向到此)。"""
    from ...core.config import pi_home
    return [str(pi_home())]


SPEC = CliSpec(
    adapter="pi",
    binary="pi",
    capabilities=("coding", "reasoning"),
    tier="standard",
    cost_per_run=4.0,
    # npm 包名用于 registry 版本比对;实际更新命令见 update_plan 钩子
    update={"npm": _NPM_PACKAGE},
    locate_binary=locate_binary,
    configured_models=configured_models,
    update_plan=update_plan,
    private_dirs=private_dirs,
)
