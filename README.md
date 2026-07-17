# MissionCrew

策略驱动的多 Agent 研发协作平台,**纯本地运行**:数据在本地 SQLite,执行是本地 Agent CLI 子进程,没有任何云端依赖。

和"把 Agent 当员工、按岗位分工、由 Leader Agent 派活"的组织式平台(如 Multica 的 Squad 模型)不同,MissionCrew 的协作模型是:

```text
任务/消息
→ 策略与流程控制(控制平面)
→ 动态装配执行配置(后端 + 模型 + 项目上下文 + 权限 + 预算)
→ 一个成熟 Agent 端到端完成一个阶段
→ 按风险验证和审批(证据驱动门禁)/ 回复贴回频道继续协作
```

## 项目是第一层级

MissionCrew 是一个**多项目管理器**,项目之间互不相干(类似 Multica 的 Workspace 隔离):

- 每个项目拥有**自己的一套角色、频道(聊天)、任务与准则**;新建项目自动获得默认角色和 `general` 频道;删除项目级联清理其角色与频道(消息保留可审计)。
- 一个项目的角色在另一个项目的频道里 @ 不到;角色名册、任务看板、设置页都只呈现当前项目。
- **后端是全局资源**:本机安装的 Agent CLI 由所有项目共享(如同 Multica 的 Runtime);聊天角色固定使用 runtime/model,结构化任务的阶段路由与配额全局记账。
- Web 顶部的项目切换器是所有页面的第一入口;CLI 用 `-p/--project` 限定。

## 两种协作面

**聊天协作(轻量)**:在 Web 聊天框里 `@角色` 布置工作。每个角色固定使用一个 runtime/model;角色定位、能力与偏好用于人类或 `@lead` 选人,不参与执行时路由。Agent 的回复原样发布到频道;回复中 `@其他角色` 即发起协作,平台自动级联触发。**所有消息(包括 Agent 之间的)对人类完全可见**。

```text
你:    @dev 给 hello.txt 追加一行,完成后请 @reviewer 检查。
@dev:   已完成。@reviewer 请检查 hello.txt 是否包含两行……
@reviewer: 检查完成,所有验收条件符合。✓
```

- **转交语义**:`完成后请 @reviewer` 这类跟在"完成后/然后/之后"后面的 @ 不会立即触发,由前序角色完成后在回复中 @ 才真正执行。
- **防失控**:级联深度上限 4 层(容纳 主管→执行→评审→主管汇总 的完整闭环)、单条协作链最多 10 次执行、角色不响应自己 @ 自己,截断时平台会在频道里说明。

**调度模型**:角色定位(人格)是"选人"的专长画像,**不是任务描述**。任务简报由调度方——人类,或 `@lead` 主管角色——根据任务需求结合项目章程撰写,随 @ 消息交给执行角色;平台在装配时自动附上项目准则与最近对话。想一句话交办复杂需求时用 `@lead`:它对照名册(名册携带各角色定位)拆解分派、写清每个子任务的背景/要求/验收标准,收齐汇报后核对并汇总。

**任务工作流(重量)**:结构化任务走阶段计划(复现→修复→回归→独立评审→合入),证据门禁 + 人工审批 + 全程审计,见下文。

## 为什么不是"更多 Agent"

1. **模型和 Agent 能力越来越强,不需要那么多固定 Agent。** 一个成熟的 Coding Agent 端到端完成工作;平台只在能力不足、安全等级、独立审查、多模态验证、人工决策时切换或增加执行者。
2. **划分多个 Agent 的真实动机主要是成本。** 成本是路由的显式维度:后端按 `economy / standard / expert` 分档,简单任务走低档,失败自动升级,预算上限和配额是硬约束。
3. **"Leader 领域理解不足会系统性误分"是伪问题。** 领域理解不属于 Agent,属于项目和角色描述:同一个后端装配不同的项目准则、角色人格和 Skill,就能做不同领域的工作。后端只体现**能力和成本**。

## 支持的本地 Agent

全局设置是一个 **运行时页(仿 Multica Runtime)**:只列支持的工具矩阵 + 安装状态/版本/路径 + 启停开关,**不在这里配置档位/成本/能力**——那些属于角色层。`mc backend detect` 自动扫描注册,**一个工具一条记录**;模型阶梯挂在工具下(自动填充),路由按 **工具×模型** 展开执行单元:

| CLI | 适配器 | 接入方式 | 模型阶梯(自动填充) |
|---|---|---|---|
| `claude` (Claude Code) | `claude_code` | 打印模式 | haiku(经济)/ 默认(标准)/ opus(专家) |
| `codex` (OpenAI Codex) | `codex` | 打印模式(`codex exec` 工作区沙箱) | CLI 默认 |
| `grok` (Grok Build) | `grok_build` | 打印模式(`grok -p`,自动批准工具调用) | CLI 默认 |
| `opencode` | `opencode` | 打印模式 | CLI 默认 |
| `copilot` (GitHub Copilot CLI) | `copilot` | 打印模式 | CLI 默认 |
| `cursor-agent` (Cursor) | `cursor` | 打印模式 | CLI 默认 |
| `codebuddy` | `codebuddy` | 打印模式 | CLI 默认 |
| `pi` | `pi` | 打印模式 | CLI 默认 |
| `kimi` (Kimi CLI) | `kimi` | ACP stdio 协议 | CLI 默认 |
| `kiro-cli` (Kiro) | `kiro` | ACP stdio 协议 | CLI 默认 |
| `qodercli` (Qoder) | `qoder` | ACP stdio 协议 | CLI 默认 |
| `traecli` (Trae) | `trae` | ACP stdio 协议 | CLI 默认 |

角色配置:必须先选 runtime,模型下拉自动带出该工具的阶梯(空模型名表示明确使用 CLI 默认),再配置角色定位、能力与偏好。默认角色在项目创建时一次性选择并保存固定组合,之后不会因成本、能力或历史成功率自动换 runtime/model。

两类接入方式的差别:打印模式 CLI 通过命令行直接传 prompt(命令模板支持 `{prompt}` / `{model}` 占位符);ACP 协议 CLI 作为 JSON-RPC 服务挂在 stdio 上(`initialize → session/new → session/prompt`,平台自动应答其权限请求),`Backend.command` 可覆盖默认的 serve 命令。

## 快速开始

```bash
uv sync

# 方式一:零成本演示(mock 后端,含路由升级、审批门禁、聊天级联)
uv run mc demo

# 方式二:接入真实本地 Agent
uv run mc backend detect      # 扫描注册本机 CLI;自动创建 default 项目(含角色/频道)

uv run mc serve               # http://127.0.0.1:8321(聊天 / 看板 / 项目 / 设置)
```

聊天也可以走 CLI(频道按项目命名空间,`-p` 限定项目):

```bash
uv run mc chat send -p default "@dev 修复购物车金额错误,完成后请 @reviewer 评审"
uv run mc chat log general -p default        # 查看频道完整记录(含 Agent 互相 @ 的消息)
uv run mc chat channels --create repo -p default --workdir ~/code/myrepo  # 在真实仓库上协作
uv run mc role list -p default
```

## 角色配置(Web 设置页)

角色 = **固定 runtime/model + 角色定位 + 能力 + 偏好**:

- **固定执行组合**(`runtime_id` + `model`):每个角色必填。runtime 停用或删除时该角色执行失败并明确报错,不会回退到其他 runtime。`model=""` 表示明确选择该 CLI 的默认模型。
- **角色定位**(description):自由文本,平台原样装配、不改写。它的用途是**供调度方选人**(名册中展示给人类和其他角色),在执行时也会告知角色自身,但明确标注"不是任务,不要据此自行发挥"——任务只来自 @ 消息里的简报。
- **能力**(capabilities):例如 `coding`、`reasoning`、`review`;作为角色名册中的专长标签,帮助调度方选人。
- **偏好标签**(traits):`快速`、`低成本`、`高质量`、`深度攻坚`、`多模态`、`联网检索`、`代码评审`、`安全审查`、`适合测试`、`适合文档`;描述工作风格与专长,不再改变执行组合。

默认角色:`@lead` 主管(调度,不亲自实现)、`@dev` 开发、`@reviewer` 评审、`@expert` 专家(深度攻坚)、`@vision` 视觉验证(多模态)、`@secure` 安全审查、`@tester` 测试、`@scribe` 文档。Web「设置」页可增删改角色、启停/调整后端、管理频道;「项目」页可管理项目准则与验证规则。YAML 方式见 `examples/roles.yaml`。

任务工作流:

```bash
uv run mc task create -p webshop -t bug --title "结算金额错误" --run
uv run mc task show <task_id> -v      # 阶段、证据、每次路由的决策轨迹
uv run mc approve <task_id> --approver alice
uv run mc audit                       # 全平台审计日志
```

平台数据默认在 `./.missioncrew/`(可用 `MISSIONCREW_HOME` 覆盖):数据库、频道/任务工作区、`secrets.yaml`(受控资源密钥,永不进入 Prompt)。

## 核心概念

| 概念 | 说明 |
|---|---|
| **Backend(后端)** | Agent CLI + 模型 + 档位 + 能力 + 安全许可 + 成本/配额。只回答"谁有能力干、多贵、可不可信" |
| **Project(项目)** | 第一层级容器与领域知识载体:自己的角色/频道/任务 + 项目准则、开发准则、Skill、受控资源、验证准则(Rule) |
| **Role(角色)** | 项目内可 @ 的身份:固定 runtime/model + 定位 + 能力 + 偏好,项目之间互不可见 |
| **Channel(频道)** | 项目内的协作场所,装配项目准则,可指定工作目录(真实仓库) |
| **Task + 阶段计划** | 结构化任务:基础工作流(feature/bug/chore/research)+ 准则修正,证据门禁推进 |
| **Evidence(证据)** | 任务是否完成由证据决定,执行者通过工作区 `evidence/manifest.json` 提交 |

## 架构

```text
        Web(聊天 + 看板)/ CLI                统一任务入口
                    │
     ┌──────────────▼───────────────┐
     │           控制平面           │
     │  @提及解析/转交语义/级联防护   │   聊天协作引擎(chat.py)
     │  工作流计划/证据门禁/审批     │   任务引擎(engine.py + workflow.py)
     │  任务阶段路由:安全→能力→适配→成功率→成本(router.py,决策可审计)
     │  上下文装配: 项目准则+角色人格+历史对话+证据契约(assembler.py/chat.py)
     │  受控资源: 阶段级限时授权,密钥不进 Prompt(resources.py)
     └──────────────┬───────────────┘
                    │ 执行配置
     ┌──────────────▼───────────────┐
     │           执行平面           │
     │  本地 Agent CLI 子进程(adapters.py)
     │  claude / codex / grok / opencode / copilot / cursor-agent / …
     │  每频道/每任务隔离工作区
     └──────────────────────────────┘
```

## 当前边界(后续方向)

- 聊天执行按"每条消息一次 CLI 调用"模型,上下文靠最近 20 条对话装配;基于各 CLI 的 session/resume 做连续会话是下一步。
- 任务工作流的阶段执行是同步的;聊天已是后台并发执行。
- 真实后端的证据核验只查存在性;内容级核验交给独立 Reviewer 角色。

## 测试

```bash
uv run pytest -q     # 54 项:路由链、工作流门禁、聊天触发/级联/防环、角色偏好/固定组合、调度语义、项目隔离、管理 API
```
