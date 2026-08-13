// MissionCrew pi 守卫扩展:强制模型为每次 bash 调用显式声明 timeout。
// 由 pi.py 经 `--extension` 显式加载(`--no-extensions` 只禁自动发现,
// 不影响本文件)。只做提示与门禁,不改写模型给出的任何工具参数:
// 未声明 timeout 的调用被拒绝,拒绝理由作为工具错误回到模型,由模型
// 自行补上超时重试;超时到点后命令由 pi 终止并返回 timeout 错误。

const MAX_TIMEOUT_SECONDS =
  Number(process.env.MISSIONCREW_BASH_TIMEOUT_MAX || "") || 7200;

const TIMEOUT_RULE = `

## bash 超时要求(平台强制)
每次调用 bash 工具都必须显式设置 timeout 参数(秒),按预期耗时选择:
- 快速命令(ls/grep/git status 等): 60
- 一般脚本、单元测试: 300
- 构建、E2E、依赖安装等长任务: 900-1800(上限 ${MAX_TIMEOUT_SECONDS})
超时后命令会被终止并返回 timeout 错误,可加大超时重试;
未设置 timeout 的 bash 调用会被平台直接拒绝。`;

export default function (pi: any) {
  pi.on("before_agent_start", (event: any) => ({
    systemPrompt: event.systemPrompt + TIMEOUT_RULE,
  }));

  pi.on("tool_call", (event: any) => {
    if (event.toolName !== "bash") return;
    const timeout = event.input ? event.input.timeout : undefined;
    if (typeof timeout !== "number" || !Number.isFinite(timeout) || timeout <= 0) {
      return {
        block: true,
        reason:
          "bash 调用必须显式设置 timeout(秒): 快速命令 60,一般脚本/单测 300," +
          "构建或 E2E 900-1800。请补上 timeout 后重新调用。",
      };
    }
    if (timeout > MAX_TIMEOUT_SECONDS) {
      return {
        block: true,
        reason:
          `timeout=${timeout} 超过平台上限 ${MAX_TIMEOUT_SECONDS} 秒;` +
          "请拆分任务或用更小的超时分步执行。",
      };
    }
  });
}
