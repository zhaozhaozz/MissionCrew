# MissionCrew 项目约定

## 交付流程

- 完成代码修改并通过与风险相称的验证后，默认自动提交本次任务范围内的代码，不等待额外确认。
- 提交前检查 `git status` 和暂存区，只纳入当前任务文件；保留用户已有或其他任务产生的未提交修改。
- 提交完成后默认重启 MissionCrew。停服前必须同时确认数据库没有 `queued`、`running` 或 `waiting_user` 状态的 `chat_runs`，确认 `/api/runtime/status` 的 `summary.running` 为 0 且各实例 `background_tasks` 均为 0；存在活动 Agent 或后台命令时等待其结束，不得直接中断，除非用户明确要求强制停止。
- 服务统一由 pm2 管理，入口是 `scripts/serve.sh {start|restart|stop|status|logs}`（配置在 `ecosystem.config.cjs`）。start/restart 内部用 `env -i` 构造最小环境，防止调用方终端的环境变量（`VSCODE_*`、`CLAUDE_*`、`SSH_AUTH_SOCK` 等）被服务及其 spawn 的 Agent CLI 继承；不要再用 `nohup`/`setsid` 手工拉起服务。
- 启动后用 `pm2 status missioncrew` 确认单实例在线，并检查 `0.0.0.0:8321` 监听与 `/api/overview` 健康接口；日志在 `.missioncrew/server.log`（pm2 接管 stdout/stderr）。
- 如果测试失败、无法安全区分待提交文件、提交失败或服务无法健康启动，不要生成不完整提交；先说明具体证据并继续排查。
