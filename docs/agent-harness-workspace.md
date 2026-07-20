# Agent harness 工作区与项目资料边界

MissionCrew 是本地多 Agent harness：它负责装配角色、Runtime/模型、项目上下文、共享资料以及聊天协作和结构化任务流程。它不是业务代码仓，也不应把平台运行文件写进业务代码目录。

## 两种 `docs` / `documents` 不属于同一层

- 源码仓库的 `docs/` 是 MissionCrew 本身的开发与使用文档，随源码提交。本文件就位于这个目录。
- Agent harness 的 `.missioncrew/documents/` 是某个 MissionCrew 项目的版本化文档入口，供执行中的 Agent 读写。它位于平台数据根，不在业务源码仓内。

因此，源码仓使用 `docs/` 不代表运行时入口也应叫 `docs/`。运行时入口的稳定名称是 `documents/`，环境变量是 `MISSIONCREW_DOCUMENTS_DIR`。

## 工作目录与 harness 目录

每次执行同时涉及两个位置：

1. `workdir` 是实际工作目录。频道绑定代码仓时，它通常就是代码仓，业务源码和交付物应在这里修改。
2. `MISSIONCREW_WORKSPACE` 指向平台数据根中的独立 `.missioncrew/`。这里保存 MissionCrew 提供的项目资料、任务视图、历史和运行记录，不会进入业务源码或业务 Git 提交。

聊天工作区按 `project × channel × role` 隔离并复用；结构化任务拥有自己的任务工作区。不同角色不会共享同一个 `channel-history.json`，但 `.missioncrew/documents/` 链接到同一项目文档库，所以项目文档对获准执行的角色共享。

典型目录如下：

```text
.missioncrew/
├── README.md
├── project.md
├── documents/              # 项目版本化文档库入口
├── tasks/                  # 项目任务的 Markdown 视图
├── guidelines/             # 已启用准则的 Markdown 快照
├── skills/                 # 已启用 Skill，每项含 SKILL.md
├── channel-history.json    # 仅聊天工作区；按角色隔离
├── evidence/               # 仅结构化任务；阶段证据及 manifest
└── runtime/                # Runtime 最近输出等诊断文件
```

## 各目录的读写语义

### `documents/`

`documents/` 是指向项目文档工作树的符号链接。Agent 可以直接创建、读取和编辑 Markdown 或其他项目资料；聊天或任务执行前后，平台检查变化并记录到该文档库自己的 Git 历史。该历史不属于业务代码仓。

准则和 Skill 中需要引用项目文档时，使用普通 Markdown 链接。Agent 根据任务按需读取链接目标，不需要平台维护额外的引用清单。

### `tasks/`

`tasks/` 将项目任务物化为带 YAML frontmatter 的 Markdown。Agent 可以新建任务，也可以编辑既有任务的标题、描述、类型、标签、风险、密级和成本上限。执行结束后，平台校验并同步这些可编辑字段。

`status`、当前阶段、阶段结果和审批是平台控制字段，不能通过修改 Markdown 绕过。编辑既有任务时还应保留 `id` 和 `snapshot_updated_at`，平台用时间戳避免旧快照覆盖较新的任务状态。

### `guidelines/` 与 `skills/`

`guidelines/` 保存已启用准则的完整 Markdown，frontmatter 使用 `name` 和 `description`。公共上下文只提供准则摘要、内容版本和文件路径，Agent 先根据 `description` 判断相关性，需要时再读取全文。

`skills/<id>/SKILL.md` 保存项目 Skill。准则和 Skill 不绑定角色或 Runtime，由 Agent 结合当前任务判断是否适用。项目设置变化后，平台在下一次装配时刷新这些文件和持久公共上下文；复用中的 Runtime 会话会收到新版本上下文替换旧版本。

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
| `MISSIONCREW_DOCUMENTS_DIR` | 当前项目文档入口，即 `.missioncrew/documents/` |
| `MISSIONCREW_GUIDELINES_DIR` | 已启用准则 Markdown 目录 |
| `MISSIONCREW_SKILLS_DIR` | 已启用 Skill 目录 |
| `MISSIONCREW_TASKS_DIR` | 项目任务 Markdown 目录 |
| `MISSIONCREW_CHANNEL_HISTORY` | 当前角色可见的频道历史文件；仅聊天执行 |
| `MISSIONCREW_ALLOWED_DIRS` | 本次明确授权的项目资源和 harness 目录列表 |

Runtime 的实际 `PWD` 仍是 `workdir`。支持原生多目录授权的适配器会把允许目录转换为相应命令行参数；其他 Runtime 也能从环境变量和提示上下文获知这些路径。

## 平台内部数据边界

项目数据库、密钥、文档 Git 元数据、平台保存的原始频道历史和其他角色的历史视图不在 Agent 授权范围内。Agent 只获得当前执行需要的业务目录和自己的 harness 工作区。

## 开发约束

- 不要在频道绑定的业务代码仓中自动创建 `.missioncrew/`。
- 不要因为源码文档位于 `docs/` 就重命名运行时的 `.missioncrew/documents/`。
- 迁移运行时目录时，只自动处理平台创建且目标明确的符号链接；普通文件或目录必须保留并报告冲突。
- 新增可写文件时，应明确它属于共享项目资料、角色隔离状态还是结构化任务状态，并据此选择目录。
- 修改项目设置后，刷新物化文件和持久公共上下文，不能让复用会话继续使用旧配置。
- 业务代码和交付物写入 `workdir` 或项目资源仓；项目文档、任务协作资料和阶段证据才写入 harness 工作区。
