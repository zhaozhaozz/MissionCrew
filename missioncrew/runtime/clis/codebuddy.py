"""CodeBuddy CLI 的静态声明。"""
from .spec import CliSpec

SPEC = CliSpec(
    adapter="codebuddy",
    binary="codebuddy",
    capabilities=("coding",),
    tier="economy",
    cost_per_run=2.0,
    command=("codebuddy", "-p", "{prompt}", "--model", "{model}",
             "--permission-mode", "acceptEdits", "--add-dir", "{allowed_dirs}"),
    session_id="fixed",
    update={"npm": "@tencent-ai/codebuddy-code"},
)
