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
- `/api/overview` 是前端 8s 轮询的精简快照(带 ETag):准则/Skill 只含元信息与内容指纹,任务不含 body;全文走准则单条端点、`skills/library` 与 `/api/tasks/{id}`。项目整对象回传时,带指纹的精简条目由 `save_project` 按主键回填现有全文,不得清空正文;给总览新增字段前先掂量轮询体积。
