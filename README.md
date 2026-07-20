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

- 每个项目拥有**自己的一套主控角色、其他角色、频道、任务、版本化文档库、面板与准则/Skills**;新建项目自动获得 `@lead` 主控和 `general` 频道。
- 一个项目的角色在另一个项目的频道里 @ 不到;角色名册、任务看板、设置页都只呈现当前项目。
- 项目通过 `orchestrator_role_id` 明确唯一主控;主控角色自己的固定 runtime/model 负责理解项目、拆解任务、创建频道/面板并调度其他角色。
- **后端是全局资源**:本机安装的 Agent CLI 由所有项目共享(如同 Multica 的 Runtime);聊天角色固定使用 runtime/model,结构化任务的阶段路由与配额全局记账。
- Web 顶部的项目切换器是所有页面的第一入口;CLI 用 `-p/--project` 限定。

## 两种协作面

**聊天协作(轻量)**:在 Web 聊天框里提交需求;人类消息不 `@` 任何角色时默认交给项目主控调度，也可直接 `@角色`。每个角色固定使用 runtime/model/effort;角色定位、能力与偏好只供人类和 `@lead` 选人,不参与执行时路由。**只有项目主控能在 Agent 回复中 `@其他角色` 发起工作**;执行角色看不到其他角色名册，完成或失败后由平台把完整结果自动交回主控。所有调度与执行结果都对人类可见，消息头显示实际执行组合。

```text
你:       给 hello.txt 追加一行并完成评审。
@lead:    @dev 请修改文件；验收标准：文件新增一行且原内容不变。
@dev:     已完成，文件新增一行且原内容保留。
@lead:    @reviewer 请独立检查该改动与验收标准。
@reviewer: 检查完成，所有验收条件符合。
@lead:    修改与评审均已完成。✓
```

- **转交语义**:主控消息中 `完成后请 @reviewer` 这类跟在“完成后/然后/之后”后面的 @ 不会立即触发；执行角色回传后，主控再决定是否进入该步骤。
- **防失控**:不限制主控的调度层级；只保留单条协作链的 Agent 执行总次数兜底，项目级可配置、默认 20 次。主控在预算内自主拆解和协作，达到上限时平台会在频道里说明；主控仍不响应自己 @ 自己。

**调度模型**:角色定位(人格)是“选人”的专长画像，**不是任务描述**。主控根据需求与项目章程对照私有名册拆解分派，为每个执行角色写清背景、要求和验收标准；Prompt 中的最近对话和触发消息使用格式化 JSON，正文、多行内容和消息边界不会混在一起。同一频道内同一角色复用 Runtime 原生会话；最近对话只用于新会话或恢复失败时补齐历史。每轮都会重新注入带版本的项目公共上下文，并要求 Runtime 压缩时完整保留；项目设置更新后，新版本在原会话的下一轮完整替换旧版本。执行角色需要回溯时可读取平台提供的脱敏频道历史 JSON，其中其他执行角色的身份与执行组合不会暴露。平台把结果完整交回主控，由主控核验并决定下一步。

**任务工作流(重量)**:结构化任务走阶段计划(复现→修复→回归→独立评审→合入),证据门禁 + 人工审批 + 全程审计,见下文。

## 为什么不是"更多 Agent"

1. **模型和 Agent 能力越来越强,不需要那么多固定 Agent。** 一个成熟的 Coding Agent 端到端完成工作;平台只在能力不足、安全等级、独立审查、多模态验证、人工决策时切换或增加执行者。
2. **划分多个 Agent 的真实动机主要是成本。** 成本是路由的显式维度:后端按 `economy / standard / expert` 分档,简单任务走低档,失败自动升级,预算上限和配额是硬约束。
3. **"Leader 领域理解不足会系统性误分"是伪问题。** 领域理解不属于 Agent,属于项目和角色描述:同一个后端装配不同的项目准则、角色人格和 Skill,就能做不同领域的工作。后端只体现**能力和成本**。

## 支持的本地 Agent

全局设置包含**新项目角色模板**和**运行时页(仿 Multica Runtime)**。角色模板可配置顺序、定位、能力、偏好以及固定 runtime/model/effort；创建项目时复制当前模板快照，首项作为默认主控，之后模板与已有项目角色互不联动。运行时页只列支持的工具矩阵 + 安装状态/版本/路径 + 启停开关，`mc backend detect` 自动扫描注册，**一个工具一条记录**；模型阶梯挂在工具下(自动填充)，路由按 **工具×模型** 展开执行单元:

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

角色配置:必须先选 runtime,模型下拉合并该工具的配置阶梯与向 runtime 动态查询的模型目录(空模型名表示明确使用 CLI 默认),再配置角色定位、能力与偏好。新项目从全局角色模板一次性复制并保存固定组合,之后不会因模板修改、成本、能力或历史成功率自动换 runtime/model。

两类接入方式的差别:打印模式 CLI 通过命令行直接传 prompt(命令模板支持 `{prompt}` / `{model}` 占位符),并用各工具的 session/resume 参数恢复会话;ACP 协议 CLI 作为长驻 JSON-RPC 服务挂在 stdio 上(`initialize → session/new|session/load → session/prompt`,平台自动应答其权限请求),`Backend.command` 可覆盖默认的 serve 命令。

接入细节(协议流程、检测与升级机制、模型清单来源、新工具接入步骤)见 [docs/runtimes.md](docs/runtimes.md)。

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
uv run mc chat send -p default "修复购物车金额错误并安排独立评审"
uv run mc chat log general -p default        # 查看主控调度与执行角色回传的完整记录
uv run mc chat channels --create repo -p default --workdir ~/code/myrepo  # 在真实仓库上协作
uv run mc role list -p default
```

## 角色配置(Web 设置页)

角色 = **固定 runtime/model + 角色定位 + 能力 + 偏好**:

- **固定执行组合**(`runtime_id` + `model`):每个角色必填。runtime 停用或删除时该角色执行失败并明确报错,不会回退到其他 runtime。`model=""` 表示明确选择该 CLI 的默认模型。
- **角色定位**(description):自由文本,平台原样装配、不改写。它的用途是**供人类和主控选人**；完整名册只装配给主控。执行时只会告知角色自身定位，并明确标注“不是任务,不要据此自行发挥”——任务只来自触发消息里的简报。
- **能力**(capabilities):固定选项,只收录硬性模态/工具事实(代码执行、深度推理、图像/视觉输入、语音输入、图像生成、联网检索),作为角色名册中的专长标签,帮助调度方选人,不参与执行路由。职责类描述(如代码评审、安全审查)写进定位/偏好自由文本;旧角色的这类标签读取时自动迁移进偏好。
- **偏好**(preference):自由文本(如「前端」「后端,偏好 Go」「严谨,只审不改」),描述工作风格与领域,供主控选人参考。

首次初始化的全局模板包含:`@lead` 主管(调度,不亲自实现)、`@dev` 开发、`@reviewer` 评审、`@expert` 专家(深度攻坚)、`@vision` 视觉验证(多模态)、`@secure` 安全审查、`@tester` 测试、`@scribe` 文档。Web「全局设置」可维护新项目角色模板和 Runtime；各项目设置可独立增删改已复制的角色，新增角色时也可从任意全局模板导入完整配置并在保存前调整；导入角色与项目现有角色同 ID 时，确认后可覆盖且保留原排序位置。YAML 方式见 `examples/roles.yaml`。

## 项目工作空间

- **任务频道**:频道记录自己的用途/任务边界和主工作目录。人类可管理频道;主控 Runtime 也可通过受限的 `missioncrew-action` 创建频道，其他角色不能冒用此权限。无论频道绑定哪个主目录，项目资源列表中的全部现存本地目录都会作为额外可读写目录装配给 Runtime。
- **频道历史 JSON**:平台在 `.missioncrew/channel-history/<channel>/` 原子更新不分页的完整消息记录，并通过 `MISSIONCREW_CHANNEL_HISTORY` 把当前角色可读的文件路径注入执行环境。主控读取原始记录；执行角色读取独立脱敏视图，其他执行角色统一匿名且不含其 runtime/model/effort。历史目录与真实代码仓、频道工作目录分离，不会污染业务仓库。
- **准则 Markdown 文件**:准则编辑器直接编辑完整 Markdown，YAML frontmatter 与后端统一使用 `name` / `description`，后端从文件头读取属性，不再维护或转换 `id` / `title` / `summary`。公共上下文只列出已启用准则的 `name`、`description`、内容版本和文件路径，不重复注入正文。平台把每篇准则原子写入 `projects/<id>/runtime-context/guidelines/<name>.md`，并通过 `MISSIONCREW_GUIDELINES_DIR` 注入和授权目录；Agent 先根据 `description` 判断相关性，只在任务需要时读取对应 Markdown。文件头或正文变化都会改变公共上下文版本，因此复用中的会话也会收到更新。
- **版本化文档库**:每项目的 `projects/<id>/documents/` 是所有 Runtime 都能直接读写的普通目录，路径同时通过 `MISSIONCREW_DOCUMENTS_DIR` 注入。Git 元数据独立保存在 `document-history.git`，API/Web 可创建、编辑、删除、查看文件历史和回读旧版本;每次聊天或任务执行后平台自动提交目录变化。准则与 Skill 使用普通相对 Markdown 链接关联其中的文件，Agent 仅在任务需要时主动读取，平台不维护额外引用列表，也不把链接正文预先注入上下文。Web 把文档目录树放在应用左侧栏，右侧主区只显示当前文档，版本历史由正文工具栏按钮展开；底部悬浮主控对话栏可围绕当前路径和选中行提问，明确要求修改时由主控通过受限 `write_document` action 写入新版本。
- **自定义面板**:除内置任务看板外，项目可创建任意 12 列网格面板。组件的类型、位置、尺寸和 JSON 内容都可编辑，内置示例包括需求管理、测试记录、日志分析、任务查询、指标、表格和 Markdown。
- **完整准则、Skills 与验证规则**:项目可保存多篇准则文档、多个结构化 Skill 和逐条验证规则。Web 把准则、Skill 和规则列表统一放在应用左侧栏，右侧主区均使用单栏编辑。所有执行者都会收到已启用准则的 `description` 和已启用 Skill，并结合当前任务自行判断哪些条目适用；准则全文按需读取，不再维护文件、Runtime 或角色绑定列表。各页顶部只有紧凑操作栏，底部共用的悬浮对话栏会显示当前页面、当前条目、选中字段与行号，并把当前草稿和选中文本结构化地交给项目主控；准则页还会明确传递完整 Markdown 及 frontmatter 约定。普通提问只返回回答，明确要求创建或修改时才通过受限 `save_guideline` / `save_skill` / `save_rule` action 保存。聊天和结构化任务共用同一套装配逻辑。

任务工作流:

```bash
uv run mc task create -p webshop -t bug --title "结算金额错误" --run
uv run mc task show <task_id> -v      # 阶段、证据、每次路由的决策轨迹
uv run mc approve <task_id> --approver alice
uv run mc audit                       # 全平台审计日志
```

平台数据默认在 `./.missioncrew/`(可用 `MISSIONCREW_HOME` 覆盖):数据库、频道/任务工作区、频道历史 JSON、`secrets.yaml`(受控资源密钥,永不进入 Prompt)。

## 核心概念

| 概念 | 说明 |
|---|---|
| **Backend(后端)** | Agent CLI + 模型 + 档位 + 能力 + 安全许可 + 成本/配额。只回答"谁有能力干、多贵、可不可信" |
| **Project(项目)** | 第一层级容器:唯一主控 + 角色/频道/任务/文档库/面板 + 完整准则、项目 Skill、受控资源和验证准则 |
| **Role(角色)** | 项目内可 @ 的身份:固定 runtime/model + 定位 + 能力 + 偏好,项目之间互不可见 |
| **Channel(频道)** | 项目内面向某类任务的协作场所,记录用途并装配完整项目上下文,可指定真实仓库工作目录 |
| **Document Library** | 对 Runtime 是普通共享目录,对平台是可查询、可回读的 Git 版本库 |
| **Board** | 主控可创建和编辑的通用网格面板,动态保存组件类型、布局和内容 |
| **Task + 阶段计划** | 结构化任务:基础工作流(feature/bug/chore/research)+ 准则修正,证据门禁推进 |
| **Evidence(证据)** | 任务是否完成由证据决定,执行者通过工作区 `evidence/manifest.json` 提交 |

## 架构

```text
        Web(聊天 + 看板)/ CLI                统一任务入口
                    │
     ┌──────────────▼───────────────┐
     │           控制平面           │
     │  @提及解析/转交语义/级联防护   │   聊天协作引擎(collab/chat.py)
     │  工作流计划/证据门禁/审批     │   任务引擎(taskflow/engine.py + workflow.py)
     │  任务阶段路由:安全→能力→适配→成功率→成本(taskflow/router.py,决策可审计)
     │  上下文装配: 项目准则+角色人格+历史对话+证据契约(taskflow/assembler.py / collab/chat.py)
     │  受控资源: 阶段级限时授权,密钥不进 Prompt(taskflow/resources.py)
     └──────────────┬───────────────┘
                    │ 执行配置
     ┌──────────────▼───────────────┐
     │           执行平面           │
     │  本地 Agent CLI 子进程(runtime/adapters.py)
     │  claude / codex / grok / opencode / copilot / cursor-agent / …
     │  每频道/每任务隔离工作区
     └──────────────────────────────┘
```

## 当前边界(后续方向)

- 聊天会话按“频道 × 角色”隔离并复用 Runtime 原生 session；打印模式每轮仍启动一个 CLI 子进程并 resume，ACP 在服务进程内复用长驻进程，服务重启后按 Runtime 声明的 `session/load` 能力恢复。自定义打印命令的 resume 参数语义未知，因此使用包含最近对话的完整恢复 Prompt。
- 任务工作流的阶段执行是同步的;聊天已是后台并发执行。
- 真实后端的证据核验只查存在性;内容级核验交给独立 Reviewer 角色。

## 测试

```bash
uv run pytest -q     # 路由链、工作流门禁、聊天级联、主控权限/配置生成、文档历史、动态面板、Runtime Skill 注入与管理 API
```
