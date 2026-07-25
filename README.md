# MissionCrew

Channel 驱动的多 Agent 研发协作平台，**纯本地运行**：数据在本地 SQLite，执行是本地 Agent CLI 子进程，没有任何云端依赖。

和"把 Agent 当员工、按岗位分工、由 Leader Agent 派活"的组织式平台(如 Multica 的 Squad 模型)不同,MissionCrew 的协作模型是:

```text
Task / 人类消息
→ 项目 Channel
→ Lead 理解上下文并协调角色
→ 角色使用固定 Runtime/模型执行
→ 结果回到 Lead，继续在同一 Channel 协作
```

## 项目是第一层级

MissionCrew 是一个**多项目管理器**,项目之间互不相干(类似 Multica 的 Workspace 隔离):

- 每个项目拥有**自己的一套主控角色、其他角色、频道、任务、版本化文档库、面板与准则/Skills**;新建项目自动获得 `@lead` 主控和 `general` 频道。
- 一个项目的角色在另一个项目的频道里 @ 不到;角色名册、任务看板、设置页都只呈现当前项目。
- 项目通过 `orchestrator_role_id` 明确唯一主控;主控角色自己的固定 runtime/model 负责理解项目、拆解任务、创建频道/面板并调度其他角色。
- **后端是全局资源**:本机安装的 Agent CLI 由所有项目共享（如同 Multica 的 Runtime）；每个项目角色固定使用 runtime/model。
- Web 顶部的项目切换器是所有页面的第一入口;CLI 用 `-p/--project` 限定。

## 两种协作面

**聊天协作(轻量)**:在 Web 聊天框里提交需求；未选择角色时默认交给项目主控。人类直接键入的 `@角色` 只是普通正文，不会触发该角色；键入 `@` 会弹出角色选择器，也可以点击角色徽标，只有从列表确认后生成的提及才携带结构化角色 ID 与精确字符范围。只选择一个角色时平台直接启动该角色；同一消息选择多个角色时平台只启动主控，并把完整提及名单交给主控统一协调。

每个角色固定使用 runtime/model/effort；角色定位、能力与偏好只供人类和 `@lead` 选人，不参与执行时路由。只有项目主控能在 Agent 回复中调度其他角色：主控必须输出 `@[角色ID]`，平台会把它归一化为频道中带合法样式的 `@角色ID`；普通 `@角色ID` 可安全地引用已有报告或其他角色，不参与调度。执行角色看不到其他角色名册，也不能调度其他角色。主控派发的角色完成或失败后，平台自动把完整结果交回主控；人类只选择一个角色时，该角色直接执行且结果留在频道，不自动触发主控；人类选择多个角色时，这些角色不会直接执行，主控根据触发消息保留的完整名单决定并行、顺序或调整人选。

```text
你（从选择器选择 dev）: @dev 只检查这个问题，结果留在频道。
你（选择 dev 和 reviewer）: @dev 修改文件；@reviewer 独立检查。  # 只启动主控协调两者
@lead 原始输出:             @[dev] 请修改文件；验收标准：新增一行且原内容不变。
频道显示:                  @dev 请修改文件；验收标准：新增一行且原内容不变。
@dev:                     已完成，原报告由 @reviewer 提供。  # 此处普通 @reviewer 仅是引用
@lead 原始输出:             @[reviewer] 请独立检查该改动与验收标准。
@reviewer:                检查完成，所有验收条件符合。
```

- **提及可信边界**:Web 只接受选择器生成并由后端重新校验的 Unicode 字符范围；粘贴或手动输入的 `@xxx` 不会升级为合法提及。主控 Runtime 只识别方括号语法 `@[角色ID]`，未知角色、自身角色和普通 `@角色ID` 都不会触发。消息持久化用户选择的角色名单与合法提及范围；人类多选时实际执行目标单独收敛为主控，主控仍能从触发 JSON 读取原始名单。
- **顺序协作**:未来步骤直接写普通 `@reviewer` 作为说明即可，不会提前触发；前序结果回到主控后，主控再用 `@[reviewer]` 明确发起下一步。
- **频道生命周期**:侧栏频道标题后的筛选菜单可查看全部、活跃或已归档频道。每个项目的 `general` 永远排在第一位且不能归档或删除；其余频道按最近消息时间倒序，尚无消息时以创建时间作为活动时间。归档频道只读并从主控的现有频道清单中排除；若运行中的 Agent 或主控跨频道动作确实向归档频道发出消息，平台会自动恢复该频道。普通频道删除会停止持久 Runtime 并把频道移入项目回收站，消息记录仍保留用于恢复和审计。文档、准则和 Skill 内容页的专属频道同样可以归档；单独删除专属频道会永久清空页面对话但保留内容本身，删除内容则会联动清空对应对话，之后再次发消息会创建全新频道。
- **停止频道 Agent**:频道存在排队、运行或等待交互的 Agent 时，聊天输入区显示“停止 Agent/停止全部”按钮。确认后平台把这些运行统一标记为 `stopped`，取消待处理的权限与用户输入，优先调用 Runtime 原生 interrupt 以保留会话；不支持 interrupt 的 Runtime 会终止当前进程。停止后的迟到输出不会发布回复、执行主控动作或继续调度，已经写入的文件修改不会自动回滚。
- **防失控**:不限制主控的调度层级；只保留单条协作链的 Agent 执行总次数兜底，项目级可配置、默认 100 次。主控在预算内自主拆解和协作，达到上限时平台会在频道里说明；主控不能调度自己。

**调度模型**:角色定位(人格)是“选人”的专长画像，**不是任务描述**。主控根据需求与项目章程对照私有名册拆解分派，为每个执行角色写清背景、要求和验收标准；人类多选的角色是协作意图，不代表这些 Runtime 已启动，主控可决定并行、顺序或改选。Prompt 中的最近对话和触发消息使用格式化 JSON，正文、多行内容、合法提及范围和消息边界不会混在一起。同一频道内同一角色复用 Runtime 原生会话；最近对话只用于新会话或恢复失败时补齐历史。每轮都会重新注入带版本的项目公共上下文，并要求 Runtime 压缩时完整保留；项目设置更新后，新版本在原会话的下一轮完整替换旧版本。公共上下文会明确说明 MissionCrew 是 Agent harness 而不是业务代码仓，并给出独立 `.missioncrew` 工作区；执行角色可在其中按需读取脱敏频道历史、项目文档、任务、准则和 Skill。主控派发的结果自动回到主控；人类单选角色的结果只写入频道历史，等待用户后续决定是否再唤起主控。

**Task（Issue）**:Task 保存标题、简介、正文、状态、标签、Channel 绑定和多条状态简报。点击“交给 Lead 处理”后，平台只是在绑定 Channel 中向 Lead 发消息，后续完全复用上面的聊天协作，不存在独立 Task 执行循环。

## 为什么不是"更多 Agent"

1. **模型和 Agent 能力越来越强,不需要那么多固定 Agent。** 一个成熟的 Coding Agent 端到端完成工作;平台只在能力不足、安全等级、独立审查、多模态验证、人工决策时切换或增加执行者。
2. **角色数量按真实协作需要增长。** Lead 在 Channel 中根据任务上下文选择执行者；Task 模型本身不预设固定阶段或强制拉起一组 Agent。
3. **"Leader 领域理解不足会系统性误分"是伪问题。** 领域理解不属于 Agent,属于项目和角色描述:同一个后端装配不同的项目准则、角色人格和 Skill,就能做不同领域的工作。后端只体现**能力和成本**。

## 支持的本地 Agent

全局设置包含**新项目角色模板**和**运行时页(仿 Multica Runtime)**。角色模板可配置顺序、定位、能力、偏好以及固定 runtime/model/effort；创建项目时复制当前模板快照，首项作为默认主控，之后模板与已有项目角色互不联动。运行时页列出支持的工具矩阵、安装状态、版本、路径和启停开关，`mc backend detect` 自动扫描注册，**一个工具一条记录**；模型列表挂在工具下，供角色选择固定执行组合：

| CLI | 适配器 | 接入方式 | 模型阶梯(自动填充) |
|---|---|---|---|
| `claude` (Claude Code) | `claude_code` | 打印模式 | haiku(经济)/ 默认(标准)/ opus(专家) |
| `codex` (OpenAI Codex) | `codex` | 打印模式(`codex exec` 工作区沙箱) | CLI 默认 |
| `grok` (Grok Build) | `grok_build` | ACP stdio(`grok agent stdio`,结构化过程与会话复用) | CLI 默认 |
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

两类接入方式的差别:打印模式 CLI 通过内置命令模板传递 prompt，并用各工具的 session/resume 参数恢复会话；ACP 协议 CLI 作为长驻 JSON-RPC 服务挂在 stdio 上(`initialize → session/new|session/load → session/prompt`,平台自动应答其权限请求)。Runtime 启动命令由对应 provider 固定维护，不属于 Backend 配置。

接入细节(协议流程、检测与升级机制、模型清单来源、新工具接入步骤)见 [docs/runtimes.md](docs/runtimes.md)。

## 快速开始

```bash
uv sync

# 方式一:零成本演示(mock 后端,含路由升级、审批门禁、聊天级联)
uv run mc demo

# 方式二:接入真实本地 Agent
uv run mc backend detect      # 扫描注册本机 CLI;自动创建 default 项目(含角色/频道)

uv run mc serve               # 默认监听 0.0.0.0:8321，访问 http://<本机IP>:8321
```

`mc serve` 默认监听所有 IPv4 网络接口，方便局域网设备访问。服务当前不提供公网身份验证；请只在可信网络中使用，或通过主机防火墙、反向代理限制来源。需要只允许本机访问时，运行 `uv run mc serve --host 127.0.0.1`。

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

项目内容统一使用 `/resources/<project>/<type>/<id-or-path>` 公开 URL；资源类型、API、Agent 路径和 Web 路由契约见 [MissionCrew 资源说明](docs/resources.md)。聊天角色通过带角色令牌的 [MissionCrew Agent Tool API](docs/agent-tool-api.md) 显式发布消息、文档和任务并获得即时错误；目录层级、文件读写和同步边界见 [Agent harness 工作区与项目资料边界](docs/agent-harness-workspace.md)。源码仓的 `docs/` 与运行时 `.missioncrew/documents/` 是两个不同层级。

- **任务频道**:频道记录自己的用途/任务边界和主工作目录。人类可管理频道；主控 Runtime 也可通过受限的 `channel.create` Agent Tool 动作创建频道，普通角色的令牌不包含该权限。无论频道绑定哪个主目录，项目资源列表中的全部现存本地目录都会作为额外可读写目录装配给 Runtime。
- **统一 Agent harness 工作区**:每个聊天角色都会获得一个隔离的 `.missioncrew/`，绝对路径通过 `MISSIONCREW_WORKSPACE` 注入。它位于平台数据根而不是频道绑定的业务代码仓，因此 Agent 在其中读取的任务、文档和诊断文件不会混入业务源码或业务提交。目录内的 `README.md` 说明读写约定，`project.md` 提供项目简介；业务代码仍在执行 `workdir` 或项目资源仓中修改。
- **频道历史 JSON**:当前角色的完整频道记录位于 `.missioncrew/channel-history.json`，路径同时通过 `MISSIONCREW_CHANNEL_HISTORY` 注入。主控读取原始记录；执行角色读取独立脱敏视图，其他执行角色统一匿名且不含其 runtime/model/effort。每个角色使用不同的 harness 工作区，不会横向看到其他角色视图。
- **准则与 Skill 文件**:准则编辑器直接编辑完整 Markdown，YAML frontmatter 与后端统一使用 `name` / `description`。准则和项目文档共用“普通工作树 + 独立 bare Git”版本机制，Web 可查看、回读和恢复历史，恢复会产生新版本。公共上下文只列出已启用准则的属性、内容版本和 `.missioncrew/guidelines/<name>.md`，不重复注入正文；项目 Skill 同时物化为 `.missioncrew/skills/<id>/SKILL.md`。Agent 结合任务按需读取；设置变化会刷新文件并改变公共上下文版本，因此复用中的会话也会收到更新。
- **完整 Skill 目录**:每个项目都有独立 `skills/` 投放目录，支持上传 ZIP、导入服务器本地目录或直接复制 Skill 文件夹后自动扫描。`SKILL.md`、`scripts/`、`references/`、`assets/` 等完整保留，并统一映射到 Agent harness；所有 Runtime 都收到同一份摘要、路径和目录授权。格式、冲突与安全规则见 [项目 Skill 完整目录](docs/skills.md)。
- **版本化文件库**:`.missioncrew/documents/` 是所有 Runtime 都能读取的项目文档入口，路径同时通过 `MISSIONCREW_DOCUMENTS_DIR` 注入。聊天角色使用 `document.publish` 发布文本或二进制文件，以便立即得到覆盖冲突、路径或权限错误；直接编辑后的执行结束快照只作为兼容路径。实际文档工作树和独立 Git 历史由平台管理。准则目录也使用这个共享实现，Skill 可以用普通相对 Markdown 链接关联项目文档；Web/API 可批量选择并逐个上传文档、确认覆盖同名文件、下载当前或历史版本，也可创建、删除、查看历史和回读旧文本版本。文档历史位于正文之前，标记最新版本，并允许为纯文本文件选择任意两个版本进行比较；二进制和非 UTF-8 文件不参与比较。
- **项目统一回收站**:文档、准则、完整 Skill 包、自定义面板、频道、角色和项目资源删除后进入同一项目级回收站页面。恢复目标已被同名资源占用时，平台保留回收项并返回冲突；永久删除和清空操作必须二次确认。文档与准则的 Git 历史、频道消息和审计记录按各自保留策略独立存在。
- **Issue 化 Task**:`.missioncrew/tasks/`（`MISSIONCREW_TASKS_DIR`）提供当前项目的只读 Task 快照；Agent 必须通过 Agent Tool 显式创建、更新、追加简报或删除 Task，回合结束不会把快照文件修改同步回事实源。Task 包含标题、简介、正文、状态、标签和一个或多个 Channel 绑定；人类与 Agent 都能编辑。状态简报是追加式历史，Agent 使用 `task.brief` 写入。Task 按最近活动时间倒序排列，字段编辑、状态变化、派发和追加简报都会让任务上浮；归档会把 Task 从默认看板和 Agent 快照中隐藏，恢复后重新进入最新位置。`task.update` 通过 `snapshot_updated_at` 防止并发覆盖；删除会把 Task 与简报一并移入项目回收站，项目主控也可使用 `task.delete`。
- **自定义面板**:除内置任务看板外，项目可创建任意 12 列网格面板。组件的类型、位置、尺寸和 JSON 内容都可编辑，内置示例包括需求管理、测试记录、日志分析、任务查询、指标、表格和 Markdown。
- **完整准则与 Skills**:项目可保存多篇准则 Markdown 和多个结构化 Skill。验证、审查、安全、审批等项目要求统一写入准则，由 Channel 中的 Agent 结合 Task 判断是否适用；平台不再维护按任务属性机械匹配的独立验证规则。Web 把准则和 Skill 列表放在应用左侧栏，右侧主区使用单栏编辑。所有执行者都会收到已启用准则的 `description` 和已启用 Skill，准则全文按需读取，不再维护文件、Runtime 或角色绑定列表。
- **内容页专属对话**:项目文档、准则和 Skill 在首次发送页面内消息时才创建独立 Channel；打开文章、刷新概览或读取列表只查找已有绑定，不会预先创建频道。内容页底部是该 Channel 的紧凑视图，直接复用正式频道的消息 Markdown、执行过程和交互控件；输入默认交给项目主控，不提供多角色 `@` 选择器。页面选区和当前文件快照作为消息上下文单独传给主控，不污染频道里可见的用户正文。底部视图与频道页读取同一份消息和运行记录，因此两边实时同步，刷新页面也会从 Channel 恢复历史，无需额外保存会话。

Task 操作：

```bash
uv run mc task create -p webshop --title "结算金额错误" \
  --summary "优惠券叠加后金额错误" --channel general
uv run mc task process <task_id>       # 在绑定 Channel 中交给 Lead
uv run mc task brief <task_id> -m "已复现，正在修复" --status in_progress
uv run mc task delete <task_id>        # 确认后移入项目回收站
uv run mc task show <task_id>
uv run mc audit                       # 全平台审计日志
```

平台数据默认在 `./.missioncrew/`（可用 `MISSIONCREW_HOME` 覆盖）：数据库、项目资料、Agent harness 工作区、频道历史、Runtime 诊断输出和 `secrets.yaml`。只有各执行者独立 workspace 中的协作文件会被授权；数据库、文档 Git 元数据、原始历史和密钥不会暴露给执行角色。

## 核心概念

| 概念 | 说明 |
|---|---|
| **Backend(后端)** | Agent CLI + 模型 + 档位 + 能力 + 安全许可 + 成本/配额。只回答"谁有能力干、多贵、可不可信" |
| **Project(项目)** | 第一层级容器:唯一主控 + 角色/频道/任务/文档库/面板 + 完整准则、项目 Skill 和受控资源 |
| **Role(角色)** | 项目内可 @ 的身份:固定 runtime/model + 定位 + 能力 + 偏好,项目之间互不可见 |
| **Channel(频道)** | 项目内面向某类任务的协作场所,记录用途并装配完整项目上下文,可指定真实仓库工作目录 |
| **Document Library** | 对 Runtime 是普通共享目录,对平台是可查询、可回读的 Git 版本库 |
| **Board** | 主控可创建和编辑的通用网格面板,动态保存组件类型、布局和内容 |
| **Task** | 类似 Issue 的协作入口：标题、简介、正文、状态、标签、Channel 绑定与多条状态简报 |

## 架构

```text
        Web(聊天 + 看板)/ CLI               统一协作入口
                    │
     ┌──────────────▼───────────────┐
     │           控制平面           │
     │  结构化提及验证/显式调度/级联防护 │   Channel 协作引擎(collab/chat.py)
     │  Task Issue/状态简报/Channel 绑定 │   Task 服务(collab/tasks.py)
     │  Task 处理 → 向绑定 Channel 的 Lead 发消息 → 复用聊天协作引擎
     │  上下文装配: 项目准则+角色人格+Channel 历史(collab/chat.py)
     └──────────────┬───────────────┘
                    │ 执行配置
     ┌──────────────▼───────────────┐
     │           执行平面           │
     │  本地 Agent CLI 子进程(runtime/adapters.py)
     │  claude / codex / grok / opencode / copilot / cursor-agent / …
     │  每频道×角色隔离的 .missioncrew harness 工作区
     └──────────────────────────────┘
```

## 当前边界(后续方向)

- 聊天会话按“频道 × 角色”隔离并复用 Runtime 原生 session；打印模式每轮仍启动一个 CLI 子进程并 resume，ACP 在服务进程内复用长驻进程，服务重启后按 Runtime 声明的 `session/load` 能力恢复。自定义打印命令的 resume 参数语义未知，因此使用包含最近对话的完整恢复 Prompt。
- Task 自身不执行阶段循环；需要验证、审查或拆分时，由 Lead 在绑定 Channel 中按项目准则协调角色。

## 测试

```bash
uv run pytest -q     # 路由链、工作流门禁、聊天级联、主控权限/配置生成、文档历史、动态面板、Runtime Skill 注入与管理 API
```
