# Agent harness 工作区与项目资料边界

MissionCrew 是本地多 Agent harness：它负责装配角色、Runtime/模型、项目上下文、共享资料以及聊天协作和结构化任务流程。它不是业务代码仓，也不应把平台运行文件写进业务代码目录。

## 两种 `docs` / `documents` 不属于同一层

- 源码仓库的 `docs/` 是 MissionCrew 本身的开发与使用文档，随源码提交。本文件就位于这个目录。
- Agent harness 的 `.missioncrew/documents/` 是某个 MissionCrew 项目的版本化文档入口，供执行中的 Agent 读写。它位于平台数据根，不在业务源码仓内。

因此，源码仓使用 `docs/` 不代表运行时入口也应叫 `docs/`。运行时读写入口的稳定名称是 `documents/`，环境变量是 `MISSIONCREW_DOCUMENTS_DIR`；向频道或 Web 暴露的项目资源前缀是 `MISSIONCREW_PROJECT_URL`，文档入口另有便捷变量 `MISSIONCREW_DOCUMENTS_URL`。

## 工作目录与 harness 目录

每次执行同时涉及两个位置：

1. `workdir` 是实际工作目录。频道绑定代码仓时，它通常就是代码仓，业务源码和交付物应在这里修改。
2. `MISSIONCREW_WORKSPACE` 指向平台数据根中的独立 `.missioncrew/`。这里保存 MissionCrew 提供的项目资料、任务视图、历史和运行记录，不会进入业务源码或业务 Git 提交。

聊天工作区按 `project × channel × role` 隔离并复用；结构化任务拥有自己的任务工作区。不同角色不会共享同一个 `channel-history.json` 或 Agent Tool 令牌，但 `.missioncrew/documents/` 链接到同一项目文档库，所以项目文档对获准执行的角色共享。

典型目录如下：

```text
.missioncrew/
├── README.md
├── project.md
├── documents/              # 项目版本化文档库入口
├── tasks/                  # 项目任务的 Markdown 视图
├── guidelines/             # 已启用准则的项目级共享实时视图
├── skills/                 # 已启用 Skill 的项目级共享实时视图
├── channel-history.json    # 仅聊天工作区；按角色隔离
├── .agent-tool-token       # 仅聊天工作区；0600 角色令牌
├── evidence/               # 仅结构化任务；阶段证据及 manifest
└── runtime/                # Runtime 最近输出等诊断文件
```

## 各目录的读写语义

### `documents/`

`documents/` 是指向项目文档工作树的符号链接。Agent 可以读取 Markdown 或其他项目资料；聊天角色发布或替换文件时应使用 `document.publish` Agent Tool 动作，以便在当前回合得到路径、覆盖、大小和权限错误，并产生带角色身份的审计。主控可用 `document.delete` 删除文件，删除本身也会形成 Git 版本。执行结束后扫描直接编辑内容只作为旧会话兼容。结构化任务仍按自己的工作区协议写入。该文档库的 Git 历史不属于业务代码仓。人类从 Web 文档页上传的文本或二进制文件也进入同一工作树和版本历史，不会复制到业务源码目录。

准则和 Skill 中需要引用项目文档时，使用普通 Markdown 链接。Agent 根据任务按需读取链接目标，不需要平台维护额外的引用清单。

内部路径只用于 Runtime 工具访问，不是 Web 地址。聊天回复和 API 使用根相对资源 URL：

```text
/resources/<project-id>/documents/<文档库相对路径>
```

例如 `[架构说明](/resources/science_agent/documents/architecture/current.md)`。该 URL 可点击、刷新和复制，不包含平台数据目录；Web 会把它解析为对应项目的文档页。平台发布 Agent 回复前还会把误输出的文档库真实路径规范化为资源 URL，前端则兼容已经保存的历史路径链接。

## 统一资源 URL

频道、任务、面板、准则、Skill 和文档使用同一组项目级资源 URL。聊天 Markdown、平台动作回执和 API 的 `resource_url` 都使用这些地址；点击、刷新、复制以及浏览器前进/后退会恢复对应项目和条目。完整的资源生命周期、API、Agent 与 Web 契约见 [MissionCrew 资源说明](resources.md)。

| 资源 | URL |
| --- | --- |
| 频道 | `/resources/<project>/channels/<channel-id>` |
| 结构化任务 | `/resources/<project>/tasks/<task-id>` |
| 自定义面板 | `/resources/<project>/dashboards/<board-id>` |
| 内置任务看板 | `/resources/<project>/dashboards/tasks` |
| 准则 | `/resources/<project>/guidelines/<name>` |
| Skill / Skill 内文件 | `/resources/<project>/skills/<skill-id>[/<relative-path>]` |
| 项目文档 | `/resources/<project>/documents/<relative-path>` |

`channel-id` 和 `board-id` 使用项目内短 ID，URL 不暴露数据库使用的 `<project>:<id>` 命名空间。`dashboards` 是面板的公开资源类型；Web 读取旧式 `boards`、`dashboard` 或 `panels` 地址时会规范化为它。真实目录只用于 Runtime 工具调用，不能用来替代这些 Web URL。

### `tasks/`

`tasks/` 只存放项目任务记录，不是协作草稿、报告或证据目录。聊天角色新建任务使用 `task.create`，修改任务使用带 `snapshot_updated_at` 的 `task.update`；工具会立即报告版本冲突。带 YAML frontmatter 的文件同步保留给历史聊天会话和结构化任务工作区。没有声明 frontmatter 的普通 Markdown 不参与任务同步，也不会创建任务；此类内容应通过 `document.publish` 放到文档库下合适的草稿或报告目录。

`status`、当前阶段、阶段结果和审批是平台控制字段，不能通过修改 Markdown 绕过。编辑既有任务时还应保留 `id` 和 `snapshot_updated_at`，平台用时间戳避免旧快照覆盖较新的任务状态。

### `guidelines/` 与 `skills/`

`guidelines/` 指向项目级共享实时视图，保存所有已启用准则的完整 Markdown，frontmatter 使用 `name` 和 `description`。准则源文件由普通工作树和独立 bare Git 历史管理；保存、恢复、启停或删除时，平台先更新事实源和项目索引，再以原子文件替换刷新实时视图，最后才返回成功。所有频道、角色和结构化任务工作区都链接到同一视图，所以同一 Agent 回合在 `guideline.save` 返回后重新读取即可得到新版，不需要等待下一次装配；删除或停用也会立即从视图消失。服务启动时会把历史工作区中的准则副本迁移成共享链接。公共上下文只提供准则摘要、内容版本和文件路径，Agent 先根据 `description` 判断相关性，需要时再读取全文。

`skills/` 同样指向项目级共享实时视图，其中每个已启用 Skill 都链接到项目的完整 Skill 目录，除 `SKILL.md` 外还可包含 `scripts/`、`references/`、`assets/` 等相对文件。`skill.save`、`skill.delete`、Web 编辑和扫描完成前都会刷新共享视图，因此动作成功返回后，新建、启停、内容更新和删除都已对所有既有工作区可见。准则和 Skill 不绑定角色或 Runtime，由 Agent 结合当前任务判断是否适用。直接复制到项目 Skill 投放目录的新包会在下一次扫描或执行装配时进入项目索引。导入和直接投放规则见 [项目 Skill 完整目录](skills.md)。

## 一致性边界

| 资源 | 事实源 | Agent 入口 | 成功返回时的保证 |
| --- | --- | --- | --- |
| 文档 | 项目文档工作树 + 独立 Git 历史 | 工作树目录链接 | 发布、覆盖或删除已直接反映到所有工作区 |
| 准则 | 准则工作树 + Git 历史 + Project 索引 | 项目级已启用准则实时视图 | 索引和原子镜像均已刷新；所有工作区读取同一版 |
| Skill | 项目 Skill 包目录 + Project 索引 | 项目级已启用 Skill 实时视图 | 包内容和启用成员列表均已刷新 |
| 任务 | SQLite 任务记录 | 每次装配生成的角色工作区 Markdown | 执行开始时从 Store 重建；工具修改会刷新调用角色的快照并以 `snapshot_updated_at` 防止旧写覆盖 |

这里的“成功返回”指 Agent Tool 或 Web API 已完成整个同步链。若 Git、索引或实时视图任一步失败，调用会返回结构化错误而不是成功；准则视图使用同目录临时文件替换，读取者只会看到完整旧版或完整新版，不会读到半写入正文。已经发送给 Runtime 的 Prompt 不会在回合中被反向修改，但 Prompt 给出的文件路径会实时指向新版；下一轮装配会重新计算公共上下文版本并把新摘要发给复用 session。

### `channel-history.json`

聊天工作区中的历史文件是结构化 JSON，供 Agent 在最近对话片段不足时主动读取。主控角色可以看到完整角色信息；执行角色读取独立脱敏视图，其他执行角色会被匿名，且不暴露其 runtime、model 或 effort。只有主控可以调度其他角色。

### `evidence/`

`evidence/` 只属于结构化任务，不是普通聊天的通用附件目录。阶段执行者把计划、变更摘要、测试报告、审查结论等产物写到这里，并在 `.missioncrew/evidence/manifest.json` 中登记：

```json
[
  {
    "type": "test_report",
    "path": ".missioncrew/evidence/test-report.md",
    "summary": "关键回归测试结果"
  }
]
```

平台摄入 `type`、`path` 和 `summary`，并根据工作流要求的证据类型决定阶段是否具备推进条件；后续阶段可以读取实际证据文件进行审查。不要把业务源码或正式交付物放进 `evidence/`。

### `runtime/`

`runtime/` 保存 Runtime 的最近输出等诊断信息，用于实时显示、排错和恢复。它属于平台运行状态，不应提交到业务代码仓。

## 注入的环境变量

| 变量 | 含义 |
| --- | --- |
| `MISSIONCREW_WORKSPACE` | 当前角色或任务的 `.missioncrew/` 根目录 |
| `MISSIONCREW_PROJECT_URL` | 当前项目的 Web 资源 URL 前缀，即 `/resources/<project>` |
| `MISSIONCREW_DOCUMENTS_DIR` | 当前项目文档入口，即 `.missioncrew/documents/` |
| `MISSIONCREW_DOCUMENTS_URL` | 当前项目文档的 Web 资源 URL 根；最终回复用它构造链接 |
| `MISSIONCREW_GUIDELINES_DIR` | 已启用准则 Markdown 目录 |
| `MISSIONCREW_SKILLS_DIR` | 已启用 Skill 目录 |
| `MISSIONCREW_TASKS_DIR` | 项目任务 Markdown 目录 |
| `MISSIONCREW_CHANNEL_HISTORY` | 当前角色可见的频道历史文件；仅聊天执行 |
| `MISSIONCREW_ALLOWED_DIRS` | 本次明确授权的项目资源和 harness 目录列表 |
| `MISSIONCREW_AGENT_TOOL_URL` | 统一 Agent Tool API 根地址；仅聊天执行 |
| `MISSIONCREW_AGENT_TOKEN_FILE` | 当前频道和角色的 Bearer token 文件；仅聊天执行 |
| `MISSIONCREW_AGENT_RUN_ID` | 进程启动时的回合 ID；持久会话应使用最新 Prompt 中显式给出的值 |
| `MISSIONCREW_AGENT_TOOL_PYTHON` | 可执行 Agent Tool CLI 模块的 Python 解释器 |

Runtime 的实际 `PWD` 仍是 `workdir`。支持原生多目录授权的适配器会把允许目录转换为相应命令行参数；其他 Runtime 也能从环境变量和提示上下文获知这些路径。Agent Tool 的身份、scope、动作和错误契约见 [MissionCrew Agent Tool API](agent-tool-api.md)。

## 平台内部数据边界

项目数据库、密钥、文档 Git 元数据、平台保存的原始频道历史和其他角色的历史视图不在 Agent 授权范围内。Agent 只获得当前执行需要的业务目录和自己的 harness 工作区。

## 开发约束

- 不要在频道绑定的业务代码仓中自动创建 `.missioncrew/`。
- 不要因为源码文档位于 `docs/` 就重命名运行时的 `.missioncrew/documents/`。
- 迁移运行时目录时，只自动处理平台创建且目标明确的符号链接；普通文件或目录必须保留并报告冲突。
- 新增可写文件时，应明确它属于共享项目资料、角色隔离状态还是结构化任务状态，并据此选择目录。
- 修改项目设置后，刷新物化文件和持久公共上下文，不能让复用会话继续使用旧配置。
- 业务代码和交付物写入 `workdir` 或项目资源仓；项目文档、任务协作资料和阶段证据才写入 harness 工作区。
- Agent 回复不得发布 `.missioncrew` 真实路径或 `file://` 链接；引用平台资源时使用 `/resources/<project>/<resource-type>/<id-or-path>`。
