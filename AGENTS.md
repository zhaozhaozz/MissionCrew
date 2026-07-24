# MissionCrew 项目约定

## 交付流程

- 完成代码修改并通过与风险相称的验证后，默认自动提交本次任务范围内的代码，不等待额外确认。
- 提交前检查 `git status` 和暂存区，只纳入当前任务文件；保留用户已有或其他任务产生的未提交修改。
- 提交完成后默认重启 MissionCrew。停服前必须同时确认数据库没有 `queued`、`running` 或 `waiting_user` 状态的 `chat_runs`，并确认 `/api/runtime/status` 的 `summary.running` 为 0；存在活动 Agent 时等待其结束，不得直接中断，除非用户明确要求强制停止。
- 重启时先用精确 PID 和监听端口确认旧服务，只对该 PID 发送 `TERM` 并确认退出；再使用 `setsid -f .venv/bin/python -m missioncrew.cli serve >> .missioncrew/server.log 2>&1 < /dev/null` 启动独立会话，避免服务绑定当前终端或工具进程。
- 启动后确认只有一个服务实例，检查 PID、PPID、SID、PGID、`0.0.0.0:8321` 监听状态，以及首页和 `/api/overview` 健康接口；独立服务应脱离原调用进程。
- 如果测试失败、无法安全区分待提交文件、提交失败或服务无法健康启动，不要生成不完整提交；先说明具体证据并继续排查。
