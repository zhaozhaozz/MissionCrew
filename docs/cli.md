# 命令行工具 `mc`

MissionCrew 的日常操作在 Web 页面完成，频道、Task、文档、准则等资源通常直接交给主控在对话中创建和维护；`mc` 是同一套业务逻辑的终端形态，与 API 走同一套校验口径，适合脚本化、无头环境和排查问题。安装依赖后用 `uv run mc <子命令>` 调用，或激活 `.venv` 后直接执行 `mc`。

所有子命令读写同一个数据目录：默认为用户主目录下的 `~/.missioncrew/`，可用环境变量 `MISSIONCREW_HOME` 覆盖。数据目录不存在时会静默新建；若服务实例通过 `MISSIONCREW_HOME` 用了别的目录，执行 CLI 前要设置同一个值，否则操作的是一份空数据库。`mc --help` 与各子命令的 `--help` 列出完整参数，本文按用途归纳。

## 服务与审计

| 命令 | 说明 |
|---|---|
| `mc serve [--host H] [--port P] [--chat-workers N]` | 启动 Web 服务（REST API + 页面），默认只监听本机 `127.0.0.1:8321`；`--host 0.0.0.0` 或环境变量 `MISSIONCREW_HOST` 可开放局域网访问（平台无身份验证，只在可信网络中使用）。`--chat-workers` 设定聊天执行并发上限（默认 16，亦可用环境变量 `MISSIONCREW_CHAT_MAX_WORKERS`），同频道同角色仍按会话串行。常驻运行请交给 pm2，见 README「用 pm2 常驻运行」。 |
| `mc audit [TASK_ID] [--limit N]` | 查看审计日志，平台每个决策都可追溯。可按 Task 过滤，默认最近 50 条。 |
| `mc demo [--no-run]` | 向当前数据目录写入 mock 后端与示例项目 `webshop`，并演示一次 Task 经频道交给主控的流程。只用于开发调试：它会把演示数据写进真实数据目录，建议配合 `MISSIONCREW_HOME` 指向临时目录。 |

## Runtime（后端）

| 命令 | 说明 |
|---|---|
| `mc backend detect [--no-register]` | 扫描本机已安装的 Agent CLI 并注册，与 Web 的「重新检测」相同。已注册的工具只刷新路径、版本与自带模型清单，保留用户的启停与配额调整；同时保证全局角色模板存在，并在平台还没有任何项目时自动创建 `default` 项目。没检测到任何工具时以非零退出。 |
| `mc backend list` | 列出注册表中的 runtime 及其 adapter、档位、成本、配额与能力。 |
| `mc backend add --file <yaml>` | 从 YAML 导入/更新 runtime 记录（单个或列表），用于手工定制；示例见 `examples/backends.real.yaml`。 |

## 项目

| 命令 | 说明 |
|---|---|
| `mc project add --file <yaml>` | 从 YAML 导入/更新项目（含 Skill、受控资源）。准则只在新建项目时随 YAML 一并导入；更新已有项目时准则以项目准则文件库为准，请在 Web 准则页修改。新项目会复制当时的全局角色模板并初始化 `general` 频道和文档库；未指定 `orchestrator_role_id` 时用模板首项作为主控。示例见 `examples/project.template.yaml`。 |
| `mc project list` | 列出项目与准则数量。 |
| `mc project show <project_id>` | 以 JSON 输出项目完整定义。 |

## 角色

| 命令 | 说明 |
|---|---|
| `mc role list [-p 项目]` | 列出角色及其启停状态、runtime/模型/effort、偏好与能力。 |
| `mc role add --file <yaml> [-p 项目]` | 从 YAML 导入/更新角色（单个或列表），`-p` 会覆盖 YAML 中的 `project_id`。校验口径与 Web 基本一致：项目必须存在，runtime 必须已注册，effort 必须是该模型支持的档位，不能停用当前主控。模型校验比 Web 更严格：runtime 注册了自带模型清单时模型必须在清单内（清单不含 CLI 默认项时留空也会被拒绝），不接受 Web 里实时探测到的型号；另外 CLI 不检查 runtime 是否已停用。示例见 `examples/roles.yaml`。 |

## 频道与聊天

| 命令 | 说明 |
|---|---|
| `mc chat channels [-p 项目]` | 列出频道，含所属项目和工作目录。 |
| `mc chat channels --create <名称> -p <项目> [--workdir 目录]` | 创建频道，id 为 `<项目>:<名称>`。 |
| `mc chat send "<内容>" [-c 频道] [-p 项目] [--author 名字] [--no-wait]` | 向频道发消息，默认频道 `general`。内容中的 `@[角色]`（方括号）显式触发该角色执行，普通 `@角色` 只是正文；不 @ 任何角色则交给主控。默认等待本条消息触发的所有执行（含级联）结束，并打印期间产生的新消息。 |
| `mc chat log [频道] [-p 项目] [--limit N]` | 查看频道最近的聊天记录。 |

频道参数既可以是完整 id（如 `default:general`），也可以只写名称；名称在多个项目里重复时需要加 `-p` 限定。

## Task

| 命令 | 说明 |
|---|---|
| `mc task create -p 项目 --title 标题 [-s 简介] [-b 正文] [-l 标签]... [-c 频道id]... [--status S] [--process]` | 创建 Task，状态是自由文本（内置取值 `待处理`、`处理中`、`已阻塞`、`已完成`），落库为 `status: 文本` 标签；`--process` 表示创建后立即发送给绑定频道的主控。 |
| `mc task process <task_id> [-m 附言]` | 在每个绑定频道里通知主控处理该 Task。 |
| `mc task brief <task_id> -m <内容> [--status S]` | 追加状态简报，可同时更新 Task 状态。 |
| `mc task list` / `mc task show <task_id>` | 列出 Task；查看单个 Task 及其状态简报。 |
| `mc task delete <task_id> [-y]` | 把 Task 及其简报移入项目回收站，可在 Web 的项目回收站中恢复。 |

## Agent 侧命令

Agent 在执行回合内操作平台资源用的是另一支随包安装的命令 `missioncrew-tool`：它凭角色令牌调用服务端的 Agent Tool API，权限校验在服务端完成。契约与动作清单见 [Agent Tool API](agent-tool-api.md)。
