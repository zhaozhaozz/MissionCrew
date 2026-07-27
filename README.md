# MissionCrew

MissionCrew 是一个 Channel 驱动的多 Agent 研发协作平台。它把本机已安装的 Agent CLI(Claude Code、Codex、Grok 等)组织成一个个项目里可 @ 的角色,让它们在频道里像一支真实团队那样协作:人类在聊天框里提需求,项目主控理解上下文、拆解任务、把简报派发给合适的角色,执行结果回到同一个频道继续讨论。整个平台纯本地运行——数据存在本地 SQLite,执行是本地 CLI 子进程,没有任何云端依赖。

它想解决的问题是:单个 Coding Agent 已经足够强,但真实研发工作仍然需要多视角协作——独立评审、安全审查、多模态验证、专项攻坚,以及把这些过程组织起来的持续上下文。直接开多个终端手工粘贴上下文既繁琐又不可追溯;而"把 Agent 当员工、预设固定流水线"的组织式平台又过重,与模型能力的增长方向相悖。MissionCrew 选择了一条中间路线:不预设阶段和流程,只提供项目、频道、角色和一套显式的派发机制,由主控 Agent 在预算内自主决定何时需要更多执行者。

这带来几个实际的优点。协作过程完整落在频道里,谁派发了什么、谁回了什么结果都可回溯;每个角色固定使用一组 runtime/model,行为可预期,不会被平台悄悄换掉;派发是显式命令而非正文里的 @ 文本,提及有可信边界,Agent 引用其他角色不会误触发级联;每个角色拥有隔离的 harness 工作区,项目文档、准则和 Skill 以文件形式按需读取,不会混入业务代码仓。项目之间完全隔离,一套平台可以同时管理多个互不相干的工作。

## 快速开始

```bash
uv sync

# 方式一:零成本演示(mock 后端,含派发、审批门禁、聊天级联)
uv run mc demo

# 方式二:接入真实本地 Agent
uv run mc backend detect      # 扫描注册本机 CLI;自动创建 default 项目(含角色/频道)
uv run mc serve               # 默认监听 0.0.0.0:8321,访问 http://<本机IP>:8321
```

之后的所有操作都在 Web 里完成。顶部的项目切换器是所有页面的第一入口;每个项目自带 `general` 频道和一套默认角色(`@lead` 主控、`@dev` 开发、`@reviewer` 评审等)。

- **聊天协作**:在频道聊天框里直接提需求,默认交给项目主控。键入 `@` 会弹出角色选择器:只选一个角色时该角色直接执行,结果留在频道;选多个角色时平台只启动主控,由它拿着完整名单决定并行、顺序或改选。手动打出来的 `@xxx` 只是普通文字,永远不会触发角色。
- **频道**:侧栏可创建、归档、删除频道;频道记录自己的用途和主工作目录,可以绑定真实代码仓协作。频道里有 Agent 排队或运行时,输入区提供"停止 Agent/停止全部"。
- **Task 看板**:Task 类似 Issue,保存标题、正文、状态、标签、Channel 绑定和追加式状态简报。点击"交给 Lead 处理"就是在绑定频道里向主控发一条消息,后续完全复用聊天协作,没有独立的任务执行循环。
- **项目设置**:维护角色(固定 runtime/model + 定位 + 能力 + 偏好)、多篇准则 Markdown、完整 Skill 包、版本化文档库和自定义面板。文档和准则由平台用独立 Git 管理版本,可查看历史、比较和回读;删除的内容统一进入项目回收站。
- **全局设置**:维护新项目角色模板和 Runtime 列表(安装状态、版本、启停开关、模型清单)。

`mc serve` 默认监听所有 IPv4 接口以方便局域网访问,但不提供公网身份验证;请只在可信网络中使用,或用 `uv run mc serve --host 127.0.0.1` 限制为本机访问。聊天执行并发上限默认 16,可用 `--chat-workers <N>` 或环境变量 `MISSIONCREW_CHAT_MAX_WORKERS` 调整。

## 架构与核心概念

```text
        Web(聊天 + 看板)                    统一协作入口
                    │
     ┌──────────────▼───────────────┐
     │           控制平面           │
     │  结构化提及验证/显式派发/级联防护 │   Channel 协作引擎(collab/chat.py)
     │  Task Issue/状态简报/Channel 绑定 │   Task 服务(collab/tasks.py)
     │  上下文装配: 项目准则+角色人格+频道历史
     └──────────────┬───────────────┘
                    │ 执行配置
     ┌──────────────▼───────────────┐
     │           执行平面           │
     │  本地 Agent CLI 子进程(runtime/adapters.py)
     │  claude / codex / grok / opencode / copilot / cursor-agent / …
     │  每频道×角色隔离的 .missioncrew harness 工作区
     └──────────────────────────────┘
```

| 概念 | 说明 |
|---|---|
| **Project(项目)** | 第一层级容器:唯一主控 + 角色/频道/任务/文档库/面板 + 准则、Skill 和受控资源,项目之间互不可见 |
| **Runtime(运行时)** | 本机安装的 Agent CLI,全局资源,所有项目共享;模型清单挂在工具下供角色选择 |
| **Role(角色)** | 项目内可 @ 的身份:固定 runtime/model + 定位 + 能力 + 偏好。定位是"选人"的专长画像,不是任务描述 |
| **Channel(频道)** | 项目内面向某类任务的协作场所,记录用途并装配完整项目上下文,可指定真实仓库工作目录 |
| **Task** | 类似 Issue 的协作入口:标题、正文、状态、标签、Channel 绑定与多条状态简报 |
| **Document Library** | 对 Runtime 是普通共享目录,对平台是可查询、可回读的 Git 版本库;准则复用同一套版本机制 |
| **Board** | 主控可创建和编辑的通用 12 列网格面板,动态保存组件类型、布局和内容 |

**派发模型**:只有项目主控能调度其他角色,且派发是显式命令——执行 Agent Tool 的 `message.publish` 并把角色 id 写进 `mentions` 数组,平台据此生成合法提及并触发执行。Agent 正文里的任何 `@角色` 都只是普通文字,永不触发,因此描述已完成的派发、引用其他角色的报告都安全。被派发角色完成或失败后,平台自动把完整结果交回主控;人类单选角色时结果留在频道,不自动唤起主控。平台不限制调度层级,只保留单条协作链的执行总次数兜底(项目级可配置,默认 100 次)。

**会话与上下文**:同一频道内同一角色复用 Runtime 原生会话;每轮重新注入带版本的项目公共上下文,项目设置更新后旧会话的下一轮会收到新版本。每个角色获得隔离的 `.missioncrew/` harness 工作区(位于平台数据根,不在业务代码仓内),其中提供脱敏频道历史、版本化文档库、只读 Task 快照、准则和 Skill 文件,Agent 结合任务按需读取。平台数据默认在 `./.missioncrew/`,可用 `MISSIONCREW_HOME` 覆盖。

更细的契约文档:[Runtime 接入](docs/runtimes.md)、[Agent Tool API](docs/agent-tool-api.md)、[资源与 URL 约定](docs/resources.md)、[harness 工作区边界](docs/agent-harness-workspace.md)、[项目 Skill 目录](docs/skills.md)。

## 支持的本地 Agent

`mc backend detect` 自动扫描本机安装的 CLI 并注册,一个工具一条记录。打印模式 CLI 通过内置命令模板传递 prompt,用各工具的 session/resume 参数恢复会话;ACP 协议 CLI 作为长驻 JSON-RPC 服务挂在 stdio 上,平台自动应答其权限请求。

| CLI | 适配器 | 接入方式 | 自带模型清单(自动填充,不可编辑) |
|---|---|---|---|
| `claude` (Claude Code) | `claude_code` | 打印模式 | (CLI 默认)/ haiku / sonnet / opus / fable;带版本号的型号见「来自 runtime」 |
| `codex` (OpenAI Codex) | `codex` | 打印模式(`codex exec` 工作区沙箱) | CLI 默认 |
| `grok` (Grok Build) | `grok_build` | ACP stdio(`grok agent stdio`) | CLI 默认 |
| `opencode` | `opencode` | 打印模式 | CLI 默认 |
| `copilot` (GitHub Copilot CLI) | `copilot` | 打印模式 | CLI 默认 |
| `cursor-agent` (Cursor) | `cursor` | 打印模式 | CLI 默认 |
| `codebuddy` | `codebuddy` | 打印模式 | CLI 默认 |
| `pi` | `pi` | 打印模式 | CLI 默认 |
| `kimi` (Kimi CLI) | `kimi` | ACP stdio 协议 | CLI 默认 |
| `kiro-cli` (Kiro) | `kiro` | ACP stdio 协议 | CLI 默认 |
| `qodercli` (Qoder) | `qoder` | ACP stdio 协议 | CLI 默认 |
| `traecli` (Trae) | `trae` | ACP stdio 协议 | CLI 默认 |

协议流程、检测与升级机制、模型清单来源、新工具接入步骤见 [docs/runtimes.md](docs/runtimes.md)。

## 测试

```bash
uv run pytest -q
```
