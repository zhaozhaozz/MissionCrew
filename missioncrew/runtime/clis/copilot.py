"""GitHub Copilot CLI 的静态声明。"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="copilot",
    binary="copilot",
    capabilities=("coding",),
    tier="economy",
    cost_per_run=2.0,
    command=("copilot", "-p", "{prompt}", "--model", "{model}",
             "--allow-all-tools", "--add-dir={allowed_dirs}"),
    session_id="fixed",
    # copilot 常由 VS Code 扩展托管;仅当二进制确实由 npm 管理时才允许
    # `npm install -g` 更新,update_plan 会先做归属判断
    update={"npm": "@github/copilot"},
)
