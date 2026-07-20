# Runtime(后端)接入说明

Runtime 指本机安装的 Agent CLI(代码中的 `Backend`)。它是**全局资源**:注册表存在 SQLite,所有项目共享;角色在项目内定义时固定绑定某个 runtime 与模型,执行时直接使用,不做运行时路由。

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

### 打印模式 CLI(`CliAdapter`)

一次执行 = 一个子进程:按命令模板渲染参数,在频道工作目录内启动,收集 stdout/stderr,退出码判定成败。模板在 `DEFAULT_COMMANDS` 中定义,`Backend.command` 可整体覆盖。

模板占位符(`render_command`):

- `{prompt}` — 装配好的完整提示词(角色定位、项目上下文、最近对话、触发消息);
- `{model}` — 角色固定的模型;为空时该 token 连同紧邻的 `--model`/`-m` 标志一起移除,即显式使用 CLI 默认模型;
- `{effort}` — 角色固定的推理力度(见下方 Effort 一节);为空时连同紧邻的 `--effort`/`-c` 标志一起移除;
- `{documents_dir}` — 项目文档库路径的兼容占位符；新模板应使用 `{allowed_dirs}`；
- `{allowed_dirs}` — 当前项目全部本地资源目录与文档库；会展开为重复的 `--add-dir <path>`；
- `{workdir}` — 本次主工作目录，用于需要显式工作根参数的 CLI。

默认模板统一带非交互参数(`--permission-mode acceptEdits`、`--sandbox workspace-write`、`--allow-all-tools`、`--always-approve` 等),保证无头执行不阻塞在确认提示上。Claude、Codex、Copilot、CodeBuddy 会逐个传入额外目录；OpenCode 通过 `OPENCODE_CONFIG_CONTENT.permission.external_directory` 注入精确规则；Grok/OpenCode 同时显式传主工作目录；Cursor print 模式带 `--force`。完整输出落盘到工作区 `.mc_last_output_<adapter>.log` 便于回查,聊天回复取输出尾部。

### ACP stdio(`AcpAdapter`,kimi / kiro / qoder / trae)

这类 CLI 不接受"命令行传 prompt"的调用方式,而是作为 JSON-RPC 2.0 服务挂在 stdio 上(换行分隔)。serve 命令在 `ACP_SERVE_COMMANDS` 中定义；Kimi、Qoder、Trae 会在启动 ACP 服务前逐个传入项目额外目录，Kiro 使用其 trust-all-tools 模式并由 ACP 权限请求应答完成外部访问。`Backend.command` 同样可覆盖，也可使用 `{allowed_dirs}` / `{workdir}` 占位符。协议流程(`acp.py`):

```text
initialize → session/new → [session/set_model] → session/prompt
```

- 回复文本来自 `session/update` 通知中的 `agent_message_chunk`,拼接为最终输出;
- Agent 反向发来的 `session/request_permission` 必须应答,否则 Agent 阻塞到内部超时、任务假死。平台无头运行,自动从 Agent 提供的选项里挑安全项:单次允许 > 会话允许 > 单次拒绝;都没有时返回协议错误(不能回 cancelled,那会取消整轮);
- 其余未知的 agent→client 请求返回空结果,避免阻塞;
- 整轮共享一个截止时间,进程 EOF 时让所有等待方立刻失败,不悬挂。

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

每次执行的进程环境:工作目录为频道 workdir(绑定代码仓的用仓路径,否则用平台自有目录并软链文档库)。`ExecutionConfig.allowed_dirs` 包含项目全部现存本地资源目录与文档库；所有 Runtime 都会收到 JSON 形式的 `MISSIONCREW_ALLOWED_DIRS`，支持原生多目录参数的适配器还会把它转换为目录授权。子进程 `PWD` 与实际 `cwd` 强制保持一致，避免 Runtime 从继承环境误判工作根。`MISSIONCREW_DOCUMENTS_DIR` 继续单独指向文档库；执行前后平台对文档库做快照提交,Agent 直接写目录的改动进入版本历史与审计。聊天执行超时 900 秒。

## 接入新工具

1. **打印模式 CLI**:在 `KNOWN_CLIS` 加检测项(二进制名、adapter 名、默认能力/档位/成本),在 `DEFAULT_COMMANDS` 加命令模板(带非交互参数);
2. **ACP 工具**:检测项之外,在 `ACP_SERVE_COMMANDS` 加 serve 命令即可,`get_adapter` 会自动路由到 `AcpAdapter`;
3. 可选:`UPDATE_SPECS` 声明升级渠道;`KNOWN_MODELS` 预置模型阶梯(需要差异化记账时);模型可枚举的工具在 `list_runtime_models` 加发现分支;
4. 二进制不在 PATH 或需要特殊参数时,注册后编辑 `Backend.command` 整体覆盖默认命令。
