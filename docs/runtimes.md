# Runtime(后端)接入说明

Runtime 指本机安装的 Agent CLI(代码中的 `Backend`)。它是**全局资源**:注册表存在 SQLite,所有项目共享；全局角色模板和项目角色都固定绑定某个 runtime 与模型，执行时直接使用,不做运行时路由。新项目复制当时的全局角色模板，首项作为默认主控，之后模板与项目角色独立维护。

本文说明支持哪些工具、用什么技术接入、如何检测/升级、模型清单从哪来,以及如何接入新工具。所有事实以 `missioncrew/runtime/` 下的 provider 与统一管理层为准。

## 支持的工具矩阵

检测表 `KNOWN_CLIS` 定义了支持的 12 个本地 CLI(另有 `mock` 适配器用于测试/演示):

| 二进制 | adapter | 接入方式 | 升级方式 |
|---|---|---|---|
| `claude` | `claude_code` | 原生双向 stream-json | npm(`@anthropic-ai/claude-code`)或 `claude update` |
| `codex` | `codex` | 原生 app-server | npm(`@openai/codex`) |
| `grok` | `grok_build` | ACP stdio | `grok update` |
| `opencode` | `opencode` | 打印模式 CLI | npm(`opencode-ai`)或 `opencode upgrade` |
| `copilot` | `copilot` | 打印模式 CLI | npm(`@github/copilot`) |
| `cursor-agent` | `cursor` | 打印模式 CLI | `cursor-agent update` |
| `codebuddy` | `codebuddy` | 打印模式 CLI | npm(`@tencent-ai/codebuddy-code`) |
| `pi` | `pi` | 打印模式 CLI | 不支持自动更新 |
| `kimi` | `kimi` | ACP stdio | `kimi upgrade`(不做最新版比对) |
| `kiro-cli` | `kiro` | ACP stdio | 不支持自动更新 |
| `qodercli` | `qoder` | ACP stdio | 不支持自动更新 |
| `traecli` | `trae` | ACP stdio | `traecli update`(不做最新版比对) |

每个工具在检测表中还带默认能力位、档位与单次成本估算，注册时自动填充；这些属性挂在工具级，用于展示与使用量估算，**不按模型细分**——同一工具下换模型不改变档位与配额扣减。项目角色固定绑定 Runtime/模型，Task 不再进行阶段路由。

## 统一 Runtime 抽象边界

业务模块只依赖 `missioncrew.runtime.runtime_manager` 和统一领域对象，不允许导入 `runtime.adapters`、调用 `get_adapter()`，也不允许根据 `ACP_SERVE_COMMANDS` 等原始执行器常量分支。CLI、ACP、Mock 的命令、协议和进程细节由 `RuntimeProvider` 实现封装在 Runtime 包内部。

统一控制面提供以下操作：

- `start(ExecutionConfig) -> RunResult`：启动一次 Runtime 执行，过程事件与最终结果使用统一格式。
- `stop(Backend, session_key="") -> int`：停止指定 Runtime 的活动 CLI 进程、ACP 一次性执行或长驻会话。
- `interrupt(Backend, session_key="") -> int`：原生 provider 中断当前 turn，但保留 session/thread 供下一轮继续；不支持的 Runtime 返回 0。
- `supports_session()` / `capabilities()`：查询实际会话复用和生命周期能力，聊天层不再猜测后端类型。
- 原生 provider 还声明 `structured_events`、`user_interaction`、`permission_control` 和 `interrupt`，前端只按能力呈现操作，不判断 Claude/Codex 名称。
- `list_models()`、`effort_options()`：统一模型和推理力度管理。
- `detect_report()`、`detect_backends()`、`update()`、`refresh_installation()`：统一发现、注册、升级和版本探测。
- `register(adapter, provider)`：为新 Runtime 或插件注册实现，不修改聊天、任务、API 或 CLI 调用链。

`start()` 的 provider 若在内部用 session lock 串行化同一会话，必须在取得该锁后、启动新 turn 或子进程前调用 `ExecutionConfig.cancellation_requested()`。频道停止可能发生在调用进入 provider 之后、真正取得会话锁之前；只在 `RuntimeManager.start()` 入口检查会让排队轮次在当前 turn 被 interrupt 后继续启动。内置 Claude、Codex、ACP、打印模式和 Mock provider 都遵守这条契约。

`ExecutionConfig.runtime_policy` 是后端无关的执行策略，包含 `readable_paths`、`writable_paths`、`skill_paths` 和 `RuntimePermissions`。权限目前统一表达为审批模式 `auto|prompt|deny`、文件系统模式 `read-only|workspace-write|full-access`、网络模式 `inherit|allow|deny`。Runtime manager 会生成 `MISSIONCREW_READABLE_DIRS`、`MISSIONCREW_WRITABLE_DIRS`、`MISSIONCREW_SKILL_DIRS`、`MISSIONCREW_RUNTIME_PERMISSIONS`，provider 再把可支持的策略翻译为命令参数或 ACP 权限响应。`allowed_dirs` 仅作为旧构造入口的兼容字段。

项目 Skill 的摘要和适用性判断仍属于项目上下文；Runtime 层负责把已选 Skill 目录作为统一策略注入所有后端。聊天角色对 MissionCrew 的写操作同样不进入原始执行器：统一注入 Agent Tool URL、角色令牌文件和当前 `run_id`，Claude、Codex、ACP 与打印模式都调用相同 HTTP/CLI 契约。删除动作进入项目统一回收站，主控通过 `recycle.list`、`recycle.restore` 和 `recycle.purge` 管理回收项，Runtime 不直接读写其内部存储。这样项目语义不会进入原始执行器，后端差异也不会反向泄漏到主程序。具体动作、权限与错误结构见 [MissionCrew Agent Tool API](agent-tool-api.md)。

## 实时运行状态

系统每 10 秒轮询一次 `GET /api/runtime/status`，并缓存同一份全局状态。左下角「运行状态」显示正在执行的实例数；聊天输入区上方的角色按钮按 `project_id + role_id` 标记对应角色是否正在运行；进入全局运行状态页或点击“立即刷新”时会额外即时请求。页面不读取各后端内部结构，而是由 `RuntimeManager.status()` 聚合每个 `RuntimeProvider.instances()` 返回的统一 `RuntimeInstance` 快照，因此新增 provider 只需实现实例枚举，不需要修改页面分支。

快照区分两种生命周期：

- `persistent`：服务进程内长驻并可复用的 Claude stream-json、Codex app-server 或 ACP stdio session。运行中显示 `running`，轮次结束但进程仍在时显示 `idle`，进程异常退出但实例记录尚在时显示 `disconnected`。
- `one_shot`：打印模式和无 session key 的 ACP 调用。子进程存在时显示 `running`，退出后立即从状态页移除。

每个实例统一提供 backend、adapter、transport、PID、session key、原生 session/thread id、项目、角色、模型、工作目录、启动时间和最近活动时间。打印模式通过活动进程注册表上报；ACP 同时上报长驻池与一次性 client；Claude/Codex 原生 provider 直接上报其会话对象。页面的后端概览始终列出全部已注册 Runtime，即使当前没有进程，也会明确显示未运行或已停用。

页面下方的「使用历史」来自独立的 `GET /api/runtime/history`，记录的是每次 `RuntimeManager.start()` 调用，而不是进程实例生命周期。`RuntimeProvider.execution_info()` 声明该次调用的 `persistent` / `one_shot` 形态与 transport；统一管理器在调用 provider 前写入 `running`，返回后更新为 `succeeded` 或 `failed`。记录包含 Runtime、项目、角色、session key、模型、effort、工作目录、起止时间和耗时，保存在平台 SQLite 中，因此服务重启后仍保留。

`ExecutionConfig`、`RuntimeInstance` 与 `runtime_usage` 同时携带 `project_id` 和 `role_id`。因此全局后端概览会聚合当前实例所属的项目和角色，实例表与历史表也直接显示这两个字段；非项目调用两者可以为空。

## 接入技术

### 逐 Runtime 会话复用矩阵

聊天会话在一个上下文周期内以 `channel::role` 为键，但每个 Runtime 的原生接口不同。聊天区的频道停止按钮会原子地把该频道全部 `queued`、`running` 和 `waiting_user` 运行改为 `stopped`，先取消待处理交互，再按能力调用原生 `interrupt`；没有 interrupt 能力的打印模式或 ACP Runtime 改用 `stop` 终止当前执行。原生 interrupt 不删除 `chat_sessions`，因此下一轮仍可复用原生 session/thread。运行线程在启动前、输出事件、失败发布、主控动作和最终回复边界都会检查终态，停止后的迟到结果不能重新打开运行或触发后续 Agent。已经落盘的部分修改保留，不做隐式回滚。

用户点击「清除上下文」后，平台先确认频道没有运行中的 Agent，再停止该频道各角色的持久实例、删除 `chat_sessions`，写入可见的 `context_boundary` 分隔消息，并把后续 key 切换为 `channel::role::context-<marker-id>`；因此固定 session id 或稳定目录型 CLI 也不会重新连接清除前的上下文。最近对话只选择分隔消息之后的记录，旧消息和完整历史文件仍保留供人类查看或 Agent 按需读取。下表中的“后续轮次”都只发送最新 MissionCrew 公共上下文和当前触发消息，不再重复回放最近对话；只有新建会话、原会话无法恢复或没有可靠原生接口时，才发送包含最近对话 JSON 的恢复输入。

| Runtime / adapter | 首轮如何创建 | 后续轮次如何复用 | 会话 ID 来源与进程生命周期 |
|---|---|---|---|
| Claude / `claude_code` | 启动双向 stream-json 进程，从 `system/init` 保存原生 session id | 服务存活时把下一条 user message 写入同一进程；进程或服务重启后以 `--resume <id>` 恢复 | Runtime 返回 ID；一个 `channel::role` 对应一个长驻进程。模型、effort、目录或环境变化时重启进程，但恢复同一 session |
| Codex / `codex` | 启动 `codex app-server`，调用 `initialize → thread/start` | 同一进程、同一 thread 调用 `turn/start`；进程或服务重启后先 `thread/resume` | app-server 返回 thread id；一个 `channel::role` 对应一个长驻进程。恢复失败会明确结束本轮，不会静默创建新 thread |
| Grok / `grok_build` | `session/new` 创建会话，并在服务进程内保留 `grok agent stdio` | 同一服务进程直接续轮；重启后通过 `session/load` 恢复 | ACP session id；按 channel × role 长驻复用 |
| OpenCode / `opencode` | 使用 `--format json` 启动，并从 JSON 事件捕获 session id | 新进程使用 `--session <id>`，继续保持 JSON 输出 | Runtime 返回 ID；每轮一个 CLI 进程。未捕获 ID 时下一轮回退恢复输入 |
| GitHub Copilot / `copilot` | MissionCrew 生成 UUID，通过 `--session-id <id>` 启动 | 新进程继续传同一个 `--session-id <id>` | 固定 ID；每轮一个 CLI 进程 |
| Cursor / `cursor` | 使用 `--output-format json` 启动，并从 JSON 结果捕获 session/chat id | 新进程使用 `--resume <id>` | Runtime 返回 ID；每轮一个 CLI 进程。未捕获 ID 时下一轮回退恢复输入 |
| CodeBuddy / `codebuddy` | MissionCrew 生成 UUID，通过 `--session-id <id>` 启动 | 新进程使用 `--resume <id>` | 固定 ID；每轮一个 CLI 进程 |
| Pi / `pi` | 为 `channel::role` 计算稳定目录，首轮传 `--session-dir <dir>` | 在同一目录启动新进程并传 `--continue` | SQLite 保存 `pi-dir:<dir>`；每轮一个 CLI 进程，会话文件位于 `runtime-sessions/pi/` |
| Kimi / `kimi` | 启动 ACP serve 进程并调用 `session/new` | 服务存活时直接在同一进程、同一 `sessionId` 调用 `session/prompt`；MissionCrew 重启后仅在 Runtime 声明 `loadSession` 时调用 `session/load` | ACP 返回 ID；一个 `channel::role` 对应一个长驻进程，空闲 30 分钟回收 |
| Kiro / `kiro` | 同 Kimi：ACP `session/new` | 同 Kimi：长驻复用；重启后能力门控 `session/load` | ACP 返回 ID；一个 `channel::role` 对应一个长驻进程，空闲 30 分钟回收 |
| Qoder / `qoder` | 同 Kimi：ACP `session/new` | 同 Kimi：长驻复用；重启后能力门控 `session/load` | ACP 返回 ID；一个 `channel::role` 对应一个长驻进程，空闲 30 分钟回收 |
| Trae / `trae` | 同 Kimi：ACP `session/new` | 同 Kimi：长驻复用；重启后能力门控 `session/load` | ACP 返回 ID；一个 `channel::role` 对应一个长驻进程，空闲 30 分钟回收 |
| Mock / `mock` | 使用 `mock:<channel::role>` 作为确定性会话 ID | 在同一会话锁内复用该 ID | 无外部进程，仅用于测试和演示 |

会话元数据保存在 SQLite `chat_sessions`：除原生 ID 外，还记录 backend id、adapter、解析后的 workdir 和最后成功接收的公共上下文版本。只有 backend、adapter、workdir 均兼容时才恢复；角色、频道或 backend 删除时同步清理。项目设置变化在下一轮把完整新版公共上下文注入原会话；Claude 的模型、effort、目录或进程环境变化会重启 OS 进程，但仍以原生 id 恢复同一会话。若 Claude/Codex 明确报告 session/thread 不存在或无效，本轮会失败并删除旧记录，绝不在同一轮静默新建；用户下一次明确重试时才使用恢复输入建立新会话。

Runtime 启动命令由 provider 固定维护，不允许通过 Backend 数据覆盖。若新增或调整打印 Runtime，必须在 `DEFAULT_COMMANDS` 和会话策略集合中同时登记，并补首轮、续接和缺失 ID 的测试；ACP serve 命令则统一维护在 `ACP_SERVE_COMMANDS`。

### Claude 双向 stream-json

默认 Claude provider 启动官方 `claude`，同时使用 `--input-format stream-json`、`--output-format stream-json`、`--include-partial-messages` 和 `--permission-prompt-tool stdio`。stdin 在进程整个生命周期保持打开，每轮发送结构化 user message；stdout 的 system、assistant、user、stream event 和 result 被转换为状态、思考、工具、工具结果、文本与最终结果事件。

Claude 原生后台 Agent 不会被禁用。provider 直接消费 stream-json 的 `task_started`、`task_progress`、`task_updated` 与 `task_notification`，以 `task_type=local_agent|remote_agent` 和 `task_id` 跟踪同一轮里的后台 subagent；其中 `task_updated` 只更新任务记录，`task_notification` 才是父 Agent 可消费结果的终止边界。顶层 `result` 若仍有 Agent 运行，只作为阶段性结果写入过程流；MissionCrew 保持该 chat run、stdout reader、权限回调和 session 锁继续有效，直到后台 Agent 全部终止且 Claude 随后发出新的顶层 `result`，才发布最终频道回复。`Agent|Task` 工具返回的异步启动文本仅用于兼容缺少 `task_started` 的旧 CLI，不能作为主要完成判断。后台 Agent 的启动、进度、等待和完成状态作为结构化 `backend_agent` 事件显示在聊天运行卡中。

`can_use_tool` control request 不由 Claude 终端自行决定，而是交给 MissionCrew。`AskUserQuestion`、`ask_user_question` 和 `request_user_input` 会生成聊天交互卡，用户回答后 provider 把 answers 写回原 control request，同一个 turn 继续执行。文件写入和联网工具会先经过统一策略硬检查，再进入 `auto|prompt|deny` 审批。非 `full-access` 模式同时通过临时 `--settings` 启用 Claude 的 OS 级 Bash sandbox，强制关闭 unsandboxed escape hatch；sandbox 不可用时执行失败，不降级成无隔离命令。用户停止执行时通过 control request 发送 interrupt，必要时再清理进程组。

### Codex app-server

默认 Codex provider 不再执行 `codex exec`，而是为每个 `channel::role` 启动官方 `codex app-server`，使用省略 `jsonrpc` 字段的 JSONL 双向协议。连接先完成 `initialize/initialized`，再调用 `thread/start|thread/resume` 和 `turn/start`；agent message delta、reasoning delta、command、file change、plan、usage 和 turn completion 通知分别映射到聊天过程事件。

每轮结构化传入 `cwd`、`model`、`effort`、`runtimeWorkspaceRoots`、`approvalPolicy` 和 `sandboxPolicy`。`workspaceWrite` 的 `writableRoots` 来自统一 `RuntimePolicy.writable_paths`，网络权限来自 `RuntimePermissions.network`。模型目录直接调用 app-server `model/list`，失败时才退回旧的 CLI 发现路径。

### 聊天交互与 MissionCrew 权限接管

原生 user-input/permission request 会以独立 JSON 事件持久化，聊天运行状态切换为 `waiting_user`。用户回答通过运行 id 与随机 request id 回传；原始回答只存在于内存中的待处理请求，不写入 run event，避免 secret input 或凭据进入日志。回答完成后原生请求收到 response，运行恢复为 `running`。

统一审批模式的含义：

- `auto`：MissionCrew YOLO。后端仍保持 request-approval 模式，平台逐次自动批准并保存不含敏感回答的审计事件；不使用 Claude `bypassPermissions` 或 Codex `danger-full-access + never` 绕开平台。
- `prompt`：显示权限卡，支持批准一次、拒绝或取消；后端提供可持久化权限建议时额外显示“本会话批准”，Claude 会把原生 `permission_suggestions` 原样回传为 session 范围的 `updatedPermissions`。
- `deny`：平台拒绝请求；Codex 同时使用 `approvalPolicy=never` 与配置的 sandbox，使不可批准的越权操作直接失败。

自动批准和 sandbox 是两层：批准请求不等于忽略文件系统边界。Codex 对额外目录或网络的 `item/permissions/requestApproval` 会回传请求的精确 permission profile；Claude 使用 `updatedInput` 继续获准的工具调用。所有交互都设有与本轮相同的截止时间，超时按取消处理。

聊天中的每条 Runtime 过程事件都渲染为可折叠项，包括输入、思考、命令、工具结果、日志、用量和交互请求。每个运行卡采用单项展开：首次载入时只展开最新事件；收到新的事件 id 时自动收起上一项并展开新项；用户手动展开任一旧项时会收起同卡片中的其他项。相同事件的流式内容追加不会被误判为新项。

### 打印模式 CLI(`CliAdapter`)

未实现原生双向 provider 的工具仍使用打印模式：一次执行 = 一个子进程，按内置命令模板渲染参数，在频道工作目录内启动，收集 stdout/stderr，以退出码判定成败。聊天执行按“频道 × 角色”持久化原生会话 id，每轮用对应 CLI 的 create/resume 参数继续；不同频道或不同角色不会共用会话。同一会话的执行串行化，避免并行轮次交叉。模板统一在 `DEFAULT_COMMANDS` 中定义，不接受 Backend 数据覆盖。

模板占位符(`render_command`):

- `{prompt}` — 装配好的完整提示词(角色定位、项目上下文、JSON 格式的最近对话与触发消息、按需读取的频道历史文件路径);
- `{model}` — 角色固定的模型;为空时该 token 连同紧邻的 `--model`/`-m` 标志一起移除,即显式使用 CLI 默认模型;
- `{effort}` — 角色固定的推理力度(见下方 Effort 一节);为空时连同紧邻的 `--effort`/`-c` 标志一起移除;
- `{documents_dir}` — 项目文档库路径的兼容占位符；新模板应使用 `{allowed_dirs}`；
- `{allowed_dirs}` — 当前项目全部本地资源目录与文档库；会展开为重复的 `--add-dir <path>`；
- `{workdir}` — 本次主工作目录，用于需要显式工作根参数的 CLI。

打印模板统一带各 CLI 的非交互参数，保证无头执行不阻塞在终端确认提示上。Copilot、CodeBuddy 会逐个传入额外目录；OpenCode 通过 `OPENCODE_CONFIG_CONTENT.permission.external_directory` 注入精确规则并显式传入主工作目录；Cursor print 模式带 `--force`。Claude/Codex 始终使用各自的原生双向 provider。诊断输出尾部落盘到独立 harness 工作区 `.missioncrew/runtime/last-output-<adapter>.log` 便于回查，不在业务代码仓生成日志；频道消息保存 Runtime 返回的完整最终回复，超长内容只在 Web 端视觉折叠。

### ACP stdio(`AcpAdapter`,Grok / kimi / kiro / qoder / trae)

这类 CLI 不接受"命令行传 prompt"的调用方式,而是作为 JSON-RPC 2.0 服务挂在 stdio 上(换行分隔)。固定 serve 命令在 `ACP_SERVE_COMMANDS` 中定义；Grok 以 `grok agent stdio` 启动，并用 `--cwd` 固定项目工作目录；Kimi、Qoder、Trae 会在启动 ACP 服务前逐个传入项目额外目录，Kiro 使用其 trust-all-tools 模式并由 ACP 权限请求应答完成外部访问。协议流程(`acp.py`):

```text
initialize → session/new|session/load → [session/set_model] → session/prompt
```

- 回复文本来自 `session/update` 通知中的 `agent_message_chunk`,拼接为最终输出;
- Agent 反向发来的 `session/request_permission` 必须应答,否则 Agent 阻塞到内部超时、任务假死。平台无头运行,自动从 Agent 提供的选项里挑安全项:单次允许 > 会话允许 > 单次拒绝;都没有时返回协议错误(不能回 cancelled,那会取消整轮);
- 其余未知的 agent→client 请求返回空结果,避免阻塞;
- 整轮共享一个截止时间,进程 EOF 时让所有等待方立刻失败,不悬挂。

聊天场景中，同一“频道 × 角色”的 ACP serve 进程和 `sessionId` 会在 MissionCrew 服务进程内长驻复用；空闲 30 分钟后回收。MissionCrew 重启或进程退出后，平台读取 SQLite 中的会话 id，并且仅当 `initialize.agentCapabilities.loadSession=true` 时调用 `session/load`。Runtime 不支持或无法恢复时，平台明确降级为 `session/new`，并把格式化最近对话随新会话首轮输入补回。

### Agent Tool、公共上下文与压缩

聊天 Prompt 分为两部分：MissionCrew 公共上下文（harness 简介、角色、项目准则 description 与 Skills、独立 `.missioncrew` 工作区、目录权限、Agent Tool 和协作规则）和本轮任务输入。公共上下文明确 MissionCrew 是 Agent harness 而不是业务代码仓，并说明 harness 文件不会进入业务源码。公共区块带内容哈希版本及压缩提示，每轮都重新注入，要求 Runtime 只压缩普通对话、工具过程和任务细节，完整保留最新公共区块。项目或角色设置变更会改变版本；准则正文虽然不直接进入 Prompt，但其内容版本会参与公共上下文哈希，因此已有会话下一轮仍会收到更新标记和完整新上下文。后收到的版本整体替换旧版本。

Agent Tool 公共区块列出当前角色的动作 scope，并注入 `MISSIONCREW_AGENT_TOOL_URL`、`MISSIONCREW_AGENT_TOKEN_FILE` 和 `MISSIONCREW_AGENT_TOOL_PYTHON`。每轮任务输入另给出最新 `run_id`；持久 Runtime 必须显式传这个值，不能使用进程启动时遗留的 `MISSIONCREW_AGENT_RUN_ID`。工具的结构化错误可以在当前 Agent 回合内处理，而最终回复文本块只能在回合结束后解析，因此历史文本块只保留兼容读取。

最近对话 JSON 只进入新建/恢复降级的首轮，正常 resume 不重复回放；完整频道历史、文档、准则、Skills 和 Task 分别位于 `MISSIONCREW_WORKSPACE` 下，并提供对应环境变量。聊天角色新建、修改或删除文档、Task 时使用 `document.publish`、`task.create`、`task.update`、`task.brief` 或 `task.delete`；文档执行后扫描只作为迁移兼容，Task 快照不会反向同步。

### Mock(`MockAdapter`)

确定性模拟后端，零成本走通 Channel 协作：触发消息里写「请 @某角色」时，模拟主控会像真实 Runtime 一样执行显式命令 `message.publish`（`mentions` 参数）派发协作，回复正文里的 `@角色ID` 只是普通文字。平台只接受这条显式派发通道，非主控调用会被权限拒绝；人类单选角色时直接执行且结果只留在频道，多选角色时只启动主控并保留原始名单供其协调，主控派发的执行结果自动返回主控。测试与演示种子数据使用它。

## 检测与注册

- **检测**(`detect_report`):对检测表逐个 `which` 探测 PATH,已安装的再跑 `--version` 提取语义版本号(输出中匹配不到语义版本就留空——有些安装 shim 会输出无关提示文本);
- **注册**(`detect_backends`):一个工具一条注册记录,写入二进制路径、版本、默认能力/档位/成本,并按 `KNOWN_MODELS` 刷新工具自带模型清单(目前只有 claude 预置:`""`(CLI 默认)/haiku/sonnet/opus/fable);
- Runtime 管理页只呈现工具、版本与安装状态;每条记录有启用开关,停用的 runtime 不能被角色绑定(保存时 400),已绑定角色的执行会明确报"不可用"。

## 模型清单

角色编辑器的模型下拉合并两个来源:

1. **工具自带清单**(`Backend.models`):只有模型名的有序列表,`""` 表示 CLI 默认、排在最前。检测时按 `KNOWN_MODELS` 刷新,**不可编辑**——`POST /api/backends` 不接受 `models` 字段。平台不跟踪单个模型的档位与成本,配额一律按工具级 `cost_per_run` 扣减。
2. **runtime 动态发现**(`list_runtime_models`,服务端缓存 10 分钟):
   - codex:默认通过 `codex app-server` 的 `model/list` 分页读取当前账号可用目录；协议启动失败时退回 `codex debug models --bundled`；
   - opencode:`opencode models`(行式 `provider/model` 目录,过滤日志噪声行);
   - ACP 工具:一次性会话,从 `session/new` 响应解析模型目录——kimi 形态是 `configOptions` 中 `category=model` 的 select 选项;trae 形态是 `models.availableModels`(`{modelId,...}` 列表,含 `currentModelId`,与 Multica 的解析对齐),同时兼容 `available_models`/`available` 与裸数组;
   - claude:CLI 无枚举命令(`claude` 无 `models` 子命令,`--model` 传错值也不枚举),返回静态目录 `CLAUDE_MODEL_CATALOG`——只列 `--model` 接受的具体型号,按系列与新旧排列;稳定别名在工具自带清单里,不重复出现在这一组;
   - mock:返回工具自带清单。

保存角色时模型必须属于两份清单之一;空模型 = 显式使用 CLI 默认,总是合法。执行时把模型套用到本次执行配置上,**不写回注册表**——注册表始终保持工具级条目。

## Effort(推理力度)

部分工具支持按次指定推理力度,支持矩阵在 `EFFORT_SUPPORT`(adapter → 允许档位):

- claude:原生 `--effort` 标志,档位 low/medium/high/xhigh/max;
- codex:原生 `turn/start.effort`,档位 minimal/low/medium/high/xhigh/max/ultra(具体模型未必支持全部档位,越界时 app-server 自行报错并照常回流到频道);
- mock:low/medium/high,仅供测试/演示走通链路;
- 其余工具不支持:角色编辑器的 effort 下拉禁用,API 对非空 effort 直接 400。

effort 与模型一样属于角色定义时固定的执行组合：空值 = CLI 默认，总是合法；打印模式执行时经内置命令模板的 `{effort}` 占位符注入，为空时连同紧邻标志一起移除。

## 升级

`UPDATE_SPECS` 为每个工具声明升级渠道,`update_plan` 按安装方式选择:

- **npm 托管优先**:二进制 realpath 落在 `node_modules/` 下才认定为 npm 托管,此时检查(查询 npm registry `latest`)与更新(`npm install -g <pkg>@latest`)走同一渠道,避免自更新器把新版本装到别处、npm 里的旧副本继续占着 PATH;
- **非 npm 安装**用工具自带的更新子命令(`claude update`、`opencode upgrade` 等),自更新器了解自己的安装方式;
- copilot 常由 VS Code 扩展托管,非 npm 安装时不提供更新;kimi 的 PyPI 同名包与独立安装版版本序列对不上,kimi/trae 只提供自更新按钮、不做最新版比对。

约束:更新命令是固定白名单,不拼接用户输入;同一 runtime 的更新持锁互斥(并发请求 409);更新期间该 runtime 不派发聊天执行,避免 Agent 跑在半更新的二进制上;版本比较只认语义版本数字段。

## 执行环境

每次执行的进程环境中，工作目录仍是 Channel workdir（绑定代码仓时就是该仓），平台不会在其中创建 `.missioncrew`。`MISSIONCREW_WORKSPACE` 指向平台数据根内当前 channel×role 的 harness，集中放置项目资料、Task 快照、角色隔离历史、令牌和 Runtime 诊断。Agent 通过 Agent Tool 发布文档、编辑 Task 和追加状态简报；Task 快照只读，不参与执行后反向同步。Runtime 只能访问 Prompt 明确列出的目录，不应使用 `/tmp`、`/var/tmp` 或其他未授权路径；临时文件使用业务仓约定目录或 `$MISSIONCREW_WORKSPACE/temp`。Channel 中的 Agent 执行不设置时间上限，直到 Runtime 返回、失败或用户主动停止。

完整的目录职责、历史隔离和内部数据边界见 [Agent harness 工作区与项目资料边界](agent-harness-workspace.md)。

## 接入新工具

1. 实现 `RuntimeProvider` 的 `start`、`stop`、`capabilities` 和 `list_models`，通过 `runtime_manager.register(adapter, provider)` 注册。长驻 provider 还应实现 `shutdown`。业务层不增加 adapter 条件分支。
2. 如果复用内置打印模式或 ACP executor，只在 Runtime 包内部补充命令模板、会话参数和权限翻译；原始 executor 不对主程序导出。
3. 在 Runtime manager 内补充二进制发现、模型目录和升级策略；模型、effort 与 capability 均通过统一查询接口暴露。
4. 双向协议统一通过 `ExecutionConfig.emit` 输出过程事件，通过 `ExecutionConfig.interact` 请求权限或用户输入；provider 不得直接依赖 Store、FastAPI 或 Web 数据结构。
5. 为 provider 增加契约测试，并保留“`missioncrew/runtime` 之外不得导入原始执行器”的架构边界测试。测试使用确定性协议进程，不能依赖真实模型额度。
6. Runtime 启动命令属于 provider 实现，不进入 Backend 数据模型；新增工具时同时实现固定命令、权限翻译、会话复用和对应测试。
