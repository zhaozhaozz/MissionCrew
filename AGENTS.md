# MissionCrew 项目约定

## 代码结构

```
missioncrew/
├── cli.py            Typer CLI(角色/任务/运行管理,与 API 同一套校验口径)
├── agent_tool.py     Runtime 内可调用的 Agent Tool 客户端(权限校验在服务端)
├── core/             领域模型与持久化:models.py(dataclass 领域对象)、
│                     store.py(SQLite,领域对象存 JSON)、config.py、seed.py
├── collab/           业务引擎:chat.py(聊天协作/主控派发)、workspace.py
│                     (Agent 工作区装配)、documents/tasks/skills/guidelines 等
├── runtime/          Runtime 统一抽象层(业务层只使用 runtime_manager)
│   ├── base.py       RuntimeProvider 契约、host_isolated_environ()
│   ├── manager.py    RuntimeManager 控制面 + _BuiltinProvider;唯一对外入口
│   ├── claude.py     Claude 原生 provider(stream-json 双向协议)
│   ├── codex.py      Codex 原生 provider(app-server thread/turn)
│   ├── pi.py         pi 原生 provider(RPC;自定义 API 模型的执行后端)
│   ├── adapters.py   通用执行器 CliAdapter/AcpAdapter + 注册表汇总与分发
│   ├── acp.py        ACP stdio 协议客户端(JSON-RPC,长驻会话池)
│   ├── usage.py      账户限额探测实现(grok/kimi/claude)
│   └── clis/         按工具的声明模块(一个工具一个文件):CliSpec 描述
│                     检测、命令模板、能力、模型、effort、升级渠道与行为钩子
├── api/              FastAPI 路由(按资源分文件);context.py 持缓存与共享状态,
│                     schemas.py 请求模型,spa.py 静态页兜底(必须最后注册)
└── web/              无构建步骤的前端:index.html + js/(按页面分文件)+ css/
tests/                pytest;fake_acp_agent.py 是 ACP 协议假服务端
docs/runtimes.md      Runtime 层的详细设计文档(会话恢复/权限/effort/升级)
```

分层规则(有测试钉住):

- 业务层(api/collab/core)不得直接 import `runtime.adapters` 或原生执行器,只经 `runtime_manager`;见 `test_application_layers_do_not_import_raw_runtime_executors`。
- 工具相关知识进 `runtime/clis/<tool>.py`:静态事实写 `CliSpec` 字段,工具特有行为写可选钩子(`session_args`、`apply_permissions`、`prepare_env`、`parse_model_efforts`、`locate_binary`、`update_plan`、`account_usage_probe` 等);`adapters.py` 只做汇总与分发,新增工具 = 加声明模块 + 追加进 `clis.SPECS`,不动执行器。
- claude/codex/pi 走各自原生 provider 类,执行与 effort 档位声明都在 provider 里;`clis/` 里对应文件只负责检测、回退模板与升级渠道。
- 钩子体内如需 adapters 工具函数,用函数内延迟导入,避免 clis↔adapters 环形依赖。

## 交付流程

- 完成代码修改并通过与风险相称的验证后，默认自动提交本次任务范围内的代码，不等待额外确认。
- 提交前检查 `git status` 和暂存区，只纳入当前任务文件；保留用户已有或其他任务产生的未提交修改。
- 提交完成后默认重启 MissionCrew。停服前必须同时确认数据库没有 `queued`、`running` 或 `waiting_user` 状态的 `chat_runs`，确认 `/api/runtime/status` 的 `summary.running` 为 0 且各实例 `background_tasks` 均为 0；存在活动 Agent 或后台命令时等待其结束，不得直接中断，除非用户明确要求强制停止。
- 服务统一由 pm2 管理，入口是 `scripts/serve.sh {start|restart|stop|status|logs}`（配置在 `ecosystem.config.cjs`）；不要再用 `nohup`/`setsid` 手工拉起服务。服务按常规继承调用方环境，PATH 必须完整——Runtime 检测靠它探测本机装了哪些 Agent CLI。宿主终端注入的变量（`VSCODE_*`、`CLAUDE_*`、`CLAUDECODE`、`GIT_ASKPASS`、`SSH_AUTH_SOCK` 等）由派发层 `runtime/base.py` 的 `host_isolated_environ()` 统一剥离，不依赖启动方式；新增这类变量改那份清单，不要改启动脚本。
- 启动后用 `pm2 status missioncrew` 确认单实例在线，并检查 `0.0.0.0:8321` 监听与 `/api/overview` 健康接口；日志在 `.missioncrew/server.log`（pm2 接管 stdout/stderr）。
- 改动 `core/models.py` 等序列化字段后重启不可跳过：脚本、CLI、测试等旁路进程会用新代码把新字段写进 `db.sqlite3`，旧服务进程 `from_dict` 读到未知字段即抛 `TypeError`、接口 500。健康检查必须打 `/api/overview` 这类会读库的接口——首页是静态 HTML，返回 200 不代表服务正常。
- 如果测试失败、无法安全区分待提交文件、提交失败或服务无法健康启动，不要生成不完整提交；先说明具体证据并继续排查。
