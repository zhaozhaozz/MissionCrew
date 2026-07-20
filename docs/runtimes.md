# Runtime(后端)接入说明

Runtime 指本机安装的 Agent CLI(代码中的 `Backend`)。它是**全局资源**:注册表存在 SQLite,所有项目共享；全局角色模板和项目角色都固定绑定某个 runtime 与模型，执行时直接使用,不做运行时路由。新项目复制当时的全局角色模板，首项作为默认主控，之后模板与项目角色独立维护。

本文说明支持哪些工具、用什么技术接入、如何检测/升级、模型清单从哪来,以及如何接入新工具。所有事实以 `missioncrew/runtime/adapters.py` 与 `missioncrew/runtime/acp.py` 为准。

## 支持的工具矩阵

检测表 `KNOWN_CLIS` 定义了支持的 12 个本地 CLI(另有 `mock` 适配器用于测试/演示):

| 二进制 | adapter | 接入方式 | 升级方式 |
|---|---|---|---|
| `claude` | `claude_code` | 打印模式 CLI | npm(`@anthropic-ai/claude-code`)或 `claude update` |
| `codex` | `codex` | 打印模式 CLI | npm(`@openai/codex`) |
| `grok` | `grok_build` | 打印模式 CLI | `grok update` |
| `opencode` | `opencode` | 打印模式 CLI | npm(`opencode-ai`)或 `opencode upgrade` |
| `copilot` | `copilot` | 打印模式 CLI | npm(`@github/copilot`) |
| `cursor-agent` | `cursor` | 打印模式 CLI | `cursor-agent update` |
| `codebuddy` | `codebuddy` | 打印模式 CLI | npm(`@tencent-ai/codebuddy-code`) |
| `pi` | `pi` | 打印模式 CLI | 不支持自动更新 |
| `kimi` | `kimi` | ACP stdio | `kimi upgrade`(不做最新版比对) |
| `kiro-cli` | `kiro` | ACP stdio | 不支持自动更新 |
| `qodercli` | `qoder` | ACP stdio | 不支持自动更新 |
| `traecli` | `trae` | ACP stdio | `traecli update`(不做最新版比对) |

每个工具在检测表中还带默认能力位、档位与单次成本估算,注册时自动填充;这些属性服务于结构化任务的阶段路由与配额记账,聊天角色不用它们做路由。

## 接入技术

### 逐 Runtime 会话复用矩阵

聊天会话统一以 `channel::role` 为键，但每个 Runtime 的原生接口不同。下表中的“后续轮次”都只发送最新 MissionCrew 公共上下文和当前触发消息，不再重复回放最近对话；只有新建会话、原会话无法恢复或没有可靠原生接口时，才发送包含最近对话 JSON 的恢复输入。

| Runtime / adapter | 首轮如何创建 | 后续轮次如何复用 | 会话 ID 来源与进程生命周期 |
|---|---|---|---|
| Claude / `claude_code` | MissionCrew 生成 UUID，通过 `--session-id <id>` 启动 | 新进程使用 `--resume <id>` | 固定 ID；每轮一个 CLI 进程 |
| Codex / `codex` | 执行 `codex exec`，从输出头部捕获原生 session/thread id | 新进程执行 `codex … exec resume <id> <prompt>` | Runtime 返回 ID；每轮一个 CLI 进程。首轮未返回 ID 时不保存，下一轮用恢复输入新建 |
| Grok / `grok_build` | MissionCrew 生成 UUID，通过 `--session-id <id>` 启动 | 新进程使用 `--resume <id>` | 固定 ID；每轮一个 CLI 进程 |
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

会话元数据保存在 SQLite `chat_sessions`：除原生 ID 外，还记录 backend id、adapter、解析后的 workdir 和最后成功接收的公共上下文版本。只有 backend、adapter、workdir 均兼容时才恢复；角色、频道或 backend 删除时同步清理。模型、effort 或项目设置变化不会换 session，而是在下一轮把新执行参数和完整新版公共上下文注入原会话。若 Runtime 明确报告 session/thread 不存在或无效，平台删除旧记录，下一轮用恢复输入新建。

`Backend.command` 的处理取决于接入方式：ACP 自定义命令仍遵循统一协议，因此可以正常复用；打印模式自定义命令可能是任意 wrapper，平台无法安全猜测其 session 参数，所以不追加原生 resume 标志，也不沿用旧 ID，而是每轮发送完整恢复输入。若新增打印 Runtime，必须在 `DEFAULT_COMMANDS` 和会话策略集合中同时登记，并补首轮、续接和缺失 ID 的测试。

### 打印模式 CLI(`CliAdapter`)

一次执行 = 一个子进程:按命令模板渲染参数,在频道工作目录内启动,收集 stdout/stderr,退出码判定成败。聊天执行按“频道 × 角色”持久化原生会话 id，每轮用对应 CLI 的 create/resume 参数继续；不同频道或不同角色不会共用会话。同一会话的执行串行化，避免并行轮次交叉。模板在 `DEFAULT_COMMANDS` 中定义,`Backend.command` 可整体覆盖；自定义打印命令的参数语义未知，平台不会猜测其 resume 标志，而是每轮发送带最近对话的完整恢复 Prompt。

模板占位符(`render_command`):

- `{prompt}` — 装配好的完整提示词(角色定位、项目上下文、JSON 格式的最近对话与触发消息、按需读取的频道历史文件路径);
- `{model}` — 角色固定的模型;为空时该 token 连同紧邻的 `--model`/`-m` 标志一起移除,即显式使用 CLI 默认模型;
- `{effort}` — 角色固定的推理力度(见下方 Effort 一节);为空时连同紧邻的 `--effort`/`-c` 标志一起移除;
- `{documents_dir}` — 项目文档库路径的兼容占位符；新模板应使用 `{allowed_dirs}`；
- `{allowed_dirs}` — 当前项目全部本地资源目录与文档库；会展开为重复的 `--add-dir <path>`；
- `{workdir}` — 本次主工作目录，用于需要显式工作根参数的 CLI。

默认模板统一带非交互参数(`--permission-mode acceptEdits`、`--sandbox workspace-write`、`--allow-all-tools`、`--always-approve` 等),保证无头执行不阻塞在确认提示上。Claude、Codex、Copilot、CodeBuddy 会逐个传入额外目录；OpenCode 通过 `OPENCODE_CONFIG_CONTENT.permission.external_directory` 注入精确规则；Grok/OpenCode 同时显式传主工作目录；Cursor print 模式带 `--force`。诊断输出尾部落盘到独立 harness 工作区 `.missioncrew/runtime/last-output-<adapter>.log` 便于回查，不在业务代码仓生成日志；频道消息保存 Runtime 返回的完整最终回复，超长内容只在 Web 端视觉折叠。

### ACP stdio(`AcpAdapter`,kimi / kiro / qoder / trae)

这类 CLI 不接受"命令行传 prompt"的调用方式,而是作为 JSON-RPC 2.0 服务挂在 stdio 上(换行分隔)。serve 命令在 `ACP_SERVE_COMMANDS` 中定义；Kimi、Qoder、Trae 会在启动 ACP 服务前逐个传入项目额外目录，Kiro 使用其 trust-all-tools 模式并由 ACP 权限请求应答完成外部访问。`Backend.command` 同样可覆盖，也可使用 `{allowed_dirs}` / `{workdir}` 占位符。协议流程(`acp.py`):

```text
initialize → session/new|session/load → [session/set_model] → session/prompt
```

- 回复文本来自 `session/update` 通知中的 `agent_message_chunk`,拼接为最终输出;
- Agent 反向发来的 `session/request_permission` 必须应答,否则 Agent 阻塞到内部超时、任务假死。平台无头运行,自动从 Agent 提供的选项里挑安全项:单次允许 > 会话允许 > 单次拒绝;都没有时返回协议错误(不能回 cancelled,那会取消整轮);
- 其余未知的 agent→client 请求返回空结果,避免阻塞;
- 整轮共享一个截止时间,进程 EOF 时让所有等待方立刻失败,不悬挂。

聊天场景中，同一“频道 × 角色”的 ACP serve 进程和 `sessionId` 会在 MissionCrew 服务进程内长驻复用；空闲 30 分钟后回收。MissionCrew 重启或进程退出后，平台读取 SQLite 中的会话 id，并且仅当 `initialize.agentCapabilities.loadSession=true` 时调用 `session/load`。Runtime 不支持或无法恢复时，平台明确降级为 `session/new`，并把格式化最近对话随新会话首轮输入补回。

### 公共上下文与压缩

聊天 Prompt 分为两部分：MissionCrew 公共上下文（harness 简介、角色、项目准则 description 与 Skills、独立 `.missioncrew` 工作区、目录权限和协作规则）和本轮任务输入。公共上下文明确 MissionCrew 是 Agent harness 而不是业务代码仓，并说明 harness 文件不会进入业务源码。公共区块带内容哈希版本及压缩提示，每轮都重新注入，要求 Runtime 只压缩普通对话、工具过程和任务细节，完整保留最新公共区块。项目或角色设置变更会改变版本；准则正文虽然不直接进入 Prompt，但其内容版本会参与公共上下文哈希，因此已有会话下一轮仍会收到更新标记和完整新上下文。后收到的版本整体替换旧版本。最近对话 JSON 只进入新建/恢复降级的首轮，正常 resume 不重复回放；完整频道历史、文档、准则、Skills 和任务分别位于 `MISSIONCREW_WORKSPACE` 下，并提供 `MISSIONCREW_CHANNEL_HISTORY`、`MISSIONCREW_DOCUMENTS_DIR`、`MISSIONCREW_GUIDELINES_DIR`、`MISSIONCREW_SKILLS_DIR`、`MISSIONCREW_TASKS_DIR` 兼容入口。Agent 可直接创建/编辑文档和任务；平台在执行后版本化文档并校验同步任务 Markdown。准则编辑器、后端模型和运行时文件统一使用 `name` / `description` YAML frontmatter，后端直接解析文件头，不从 `id` / `summary` 转换。

### Mock(`MockAdapter`)

确定性模拟后端,零成本走通全流程:任务阶段按要求产出证据文件,聊天协作可按触发消息生成包含 @ 的回复；平台只接受主控回复中的 @ 调度，执行角色结果自动返回主控。它同时模拟档位能力边界(hard 标签下 economy 档失败等)，测试与演示种子数据使用它。

## 检测与注册

- **检测**(`detect_report`):对检测表逐个 `which` 探测 PATH,已安装的再跑 `--version` 提取语义版本号(输出中匹配不到语义版本就留空——有些安装 shim 会输出无关提示文本);
- **注册**(`detect_backends`):一个工具一条注册记录,写入二进制路径、版本、默认能力/档位/成本,并按 `KNOWN_MODELS` 播种模型阶梯(目前只有 claude 预置三档:haiku/默认/opus);
- Runtime 管理页只呈现工具、版本与安装状态;每条记录有启用开关,停用的 runtime 不能被角色绑定(保存时 400),已绑定角色的执行会明确报"不可用"。

## 模型清单

角色编辑器的模型下拉合并两个来源:

1. **配置阶梯**(`Backend.models`):带档位与成本的条目,检测时自动播种、可在全局设置编辑。它的作用是差异化记账——执行时若角色模型命中阶梯条目,本次配额按该档成本扣减;`name=""` 条目表示 CLI 默认模型。
2. **runtime 动态发现**(`list_runtime_models`,服务端缓存 10 分钟):
   - codex:`codex debug models --bundled`(JSON 目录,过滤 `visibility=hide`);
   - opencode:`opencode models`(行式 `provider/model` 目录,过滤日志噪声行);
   - ACP 工具:一次性会话,从 `session/new` 响应解析模型目录——kimi 形态是 `configOptions` 中 `category=model` 的 select 选项;trae 形态是 `models.availableModels`(`{modelId,...}` 列表,含 `currentModelId`,与 Multica 的解析对齐),同时兼容 `available_models`/`available` 与裸数组;
   - claude:CLI 无枚举命令,返回静态目录 `CLAUDE_MODEL_CATALOG`——稳定别名(haiku/sonnet/opus,自动跟随最新版)在前,`--model` 实际接受的具体型号在后;
   - mock:返回配置阶梯。

保存角色时模型必须属于两个目录之一;空模型 = 显式使用 CLI 默认,总是合法。执行时把模型(及命中的档位/成本)套用到本次执行配置上,**不写回注册表**——注册表始终保持工具级条目。

## Effort(推理力度)

部分工具支持按次指定推理力度,支持矩阵在 `EFFORT_SUPPORT`(adapter → 允许档位):

- claude:原生 `--effort` 标志,档位 low/medium/high/xhigh/max;
- codex:配置覆盖 `-c model_reasoning_effort=<档位>`,档位 minimal/low/medium/high/xhigh/max/ultra(具体模型未必支持全部档位,越界时 CLI 自行报错并照常回流到频道);
- mock:low/medium/high,仅供测试/演示走通链路;
- 其余工具不支持:角色编辑器的 effort 下拉禁用,API 对非空 effort 直接 400。

effort 与模型一样属于角色定义时固定的执行组合:空值 = CLI 默认,总是合法;执行时经命令模板的 `{effort}` 占位符注入,为空时连同紧邻标志一起移除,结构化任务的阶段执行不使用它。注意:用 `Backend.command` 覆盖默认模板时,模板需自带 `{effort}` 占位符,否则角色配置的 effort 不会生效。

## 升级

`UPDATE_SPECS` 为每个工具声明升级渠道,`update_plan` 按安装方式选择:

- **npm 托管优先**:二进制 realpath 落在 `node_modules/` 下才认定为 npm 托管,此时检查(查询 npm registry `latest`)与更新(`npm install -g <pkg>@latest`)走同一渠道,避免自更新器把新版本装到别处、npm 里的旧副本继续占着 PATH;
- **非 npm 安装**用工具自带的更新子命令(`claude update`、`opencode upgrade` 等),自更新器了解自己的安装方式;
- copilot 常由 VS Code 扩展托管,非 npm 安装时不提供更新;kimi 的 PyPI 同名包与独立安装版版本序列对不上,kimi/trae 只提供自更新按钮、不做最新版比对。

约束:更新命令是固定白名单,不拼接用户输入;同一 runtime 的更新持锁互斥(并发请求 409);更新期间该 runtime 不派发聊天执行,避免 Agent 跑在半更新的二进制上;版本比较只认语义版本数字段。

## 执行环境

每次执行的进程环境:工作目录仍是频道 workdir（绑定代码仓时就是该仓），平台不会在其中创建 `.missioncrew`、文档链接或诊断日志。另一个绝对路径 `MISSIONCREW_WORKSPACE` 指向平台数据根内、当前 channel×role 或结构化任务独享的 `.missioncrew` harness 工作区；其中集中放置 `README.md`、`project.md`、`documents/`、`tasks/`、`guidelines/`、`skills/`，聊天执行另有角色隔离的 `channel-history.json`，任务执行另有 `evidence/`。`ExecutionConfig.allowed_dirs` 包含项目全部现存本地资源目录、真实文档工作树、完整项目 Skill 根及该 harness 根；所有 Runtime 都会收到 JSON 形式的 `MISSIONCREW_ALLOWED_DIRS`，支持原生多目录参数的适配器还会把它转换为目录授权。`MISSIONCREW_SKILLS_DIR` 指向 harness 中仅含已启用 Skill 的目录视图，每个条目保留完整包结构。子进程 `PWD` 与实际 cwd 强制保持一致，避免 Runtime 从继承环境误判工作根。文档入口可直接读写，执行前后平台做 Git 快照；任务 Markdown 可创建/编辑，执行后按可编辑字段同步，状态、阶段和审批仍由平台控制。聊天执行超时 900 秒。

完整的目录职责、历史隔离、证据和内部数据边界见 [Agent harness 工作区与项目资料边界](agent-harness-workspace.md)。

## 接入新工具

1. **打印模式 CLI**:在 `KNOWN_CLIS` 加检测项(二进制名、adapter 名、默认能力/档位/成本),在 `DEFAULT_COMMANDS` 加命令模板(带非交互参数);
2. **ACP 工具**:检测项之外,在 `ACP_SERVE_COMMANDS` 加 serve 命令即可,`get_adapter` 会自动路由到 `AcpAdapter`;
3. 可选:`UPDATE_SPECS` 声明升级渠道;`KNOWN_MODELS` 预置模型阶梯(需要差异化记账时);模型可枚举的工具在 `list_runtime_models` 加发现分支;
4. 二进制不在 PATH 或需要特殊参数时,注册后编辑 `Backend.command` 整体覆盖默认命令。
