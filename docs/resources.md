# MissionCrew 资源说明

MissionCrew 资源分为两个互相关联但用途不同的层级：

1. **公开资源 URL** 用于聊天 Markdown、Web 导航和 API 返回值，格式为 `/resources/<project>/<resource-type>/<id-or-path>`。
2. **Agent harness 文件入口** 位于 Runtime 获得的 `.missioncrew/` 工作区中，用于 Agent 读取文档、任务、准则和 Skill；聊天角色通过 Agent Tool 显式修改平台资源。

公开 URL 不是文件路径，也不会授予 Runtime 新的文件权限；harness 中的真实路径则只用于工具访问，不应发布到聊天消息中。

## 公开资源 URL

### 规范

```text
/resources/<project-id>/<resource-type>/<resource-id-or-relative-path>
```

- `project-id` 只能包含字母、数字、下划线和连字符。
- URL 的每个路径段分别进行百分号编码，不能把完整文件系统路径作为一个资源标识。
- 频道和自定义面板在数据库中的 ID 是 `<project-id>:<item-id>`；公开 URL 只使用项目内短 ID。
- 面板的规范公开类型是 `dashboards`。前端可以读取旧式 `boards`、`dashboard` 和 `panels`，随后会规范化为 `dashboards`。
- 资源地址使用站点根相对 URL，因此同一条 Markdown 消息可以在 localhost、局域网地址或反向代理域名下打开。

### 资源类型

| 资源 | 规范 URL | 稳定标识 | Web 打开结果 |
| --- | --- | --- | --- |
| 频道 | `/resources/<project>/channels/<channel-id>` | 项目内频道短 ID | 对应聊天频道 |
| 结构化任务 | `/resources/<project>/tasks/<task-id>` | 全局任务 ID | 内置任务看板及任务详情 |
| 自定义面板 | `/resources/<project>/dashboards/<board-id>` | 项目内面板短 ID | 对应自定义面板 |
| 内置任务看板 | `/resources/<project>/dashboards/tasks` | 保留标识 `tasks` | 项目任务看板 |
| 准则 | `/resources/<project>/guidelines/<name>` | Markdown frontmatter 的 `name` | 对应准则编辑/预览页 |
| Skill | `/resources/<project>/skills/<skill-id>` | 项目 Skill ID | Skill 的 `SKILL.md` 页面 |
| Skill 内文件 | `/resources/<project>/skills/<skill-id>/<relative-path>` | Skill 目录内相对路径 | 文件树中的对应文件 |
| 项目文档 | `/resources/<project>/documents/<relative-path>` | 文档库内相对路径 | 对应版本化文档页 |

例如：

```markdown
[架构文档](/resources/science_agent/documents/architecture/current.md)
[端到端测试准则](/resources/science_agent/guidelines/web-e2e-testing)
[任务看板](/resources/science_agent/dashboards/tasks)
[架构讨论频道](/resources/science_agent/channels/architecture-doc)
```

## 各类资源的存储与行为

### 频道

频道及消息的事实源是平台数据库。频道保存用途、工作目录、归档状态和最近活动时间；频道 URL 打开项目聊天页并选择对应频道。

归档频道默认不进入 Agent 可见范围。人类打开归档频道时仍可查看历史；Agent 向归档频道发布新消息前，平台会重新激活该频道。项目内部的 `general` 频道是保留默认频道，其资源 URL 使用短 ID `general`。

频道级停止操作会同时结束该频道中 `queued`、`running` 和 `waiting_user` 的聊天运行，释放待处理交互，并留下可见平台消息和审计记录。原生 Runtime 使用 interrupt 保留可复用会话，不支持 interrupt 的 Runtime 终止当前执行；已完成的文件写入不会回滚。

主要 API：

- `GET /api/chat/channels`
- `POST /api/chat/channels`
- `GET /api/chat/<channel-id>/messages`
- `POST /api/chat/<channel-id>/messages`
- `POST /api/chat/<channel-id>/stop`

### 结构化任务

任务状态、阶段、审批、执行记录和证据索引的事实源是平台数据库。任务 URL 打开内置任务看板并展开指定任务的详情对话框。

Agent harness 同时提供 `.missioncrew/tasks/` Markdown 视图。Agent 可以新建任务，也可以编辑既有任务的标题、描述、类型、标签、风险、密级和成本上限；状态、阶段和审批仍由平台控制。任务文件同步后获得正式任务 ID，对外引用应使用任务 URL，而不是任务 Markdown 的真实路径。

主要 API：

- `POST /api/tasks`
- `GET /api/tasks/<task-id>`
- `POST /api/tasks/<task-id>/advance`
- `POST /api/tasks/<task-id>/approve`

### 面板

自定义面板的事实源是平台数据库中的 `Board`。面板包含 12 列布局及 `markdown`、`table`、`card`、`chart`、`list`、`log`、`code` 等组件；组件也可以从任务、文档、审计或频道消息读取实时数据。

内置任务看板不是可删除的 `Board` 记录，而是平台根据项目任务实时生成的特殊面板，因此使用保留地址 `/dashboards/tasks`。代码内部仍使用 `boards` 命名模型和 API，但公开 URL 统一使用面向用户的 `dashboards`。

主要 API：

- `GET /api/projects/<project>/boards`
- `POST /api/projects/<project>/boards`
- `DELETE /api/projects/<project>/boards/<board-id>`
- `POST /api/projects/<project>/widget_data`

### 准则

准则与项目文档使用同一套版本文件库：`projects/<project>/guidelines/` 是普通 Markdown 工作树，同级 `guideline-history.git` bare Git 仓库保存完整历史。Markdown 工作树是内容事实源；项目配置中的准则数据是为兼容现有项目模型和快速组装而保留的索引缓存，启动和读取时会从 Markdown 重新解析刷新；启用状态不属于 Markdown 内容版本。文件头只使用与后端一致的 `name` 和 `description`：

```markdown
---
name: code-review
description: 修改业务代码或接口后进行独立审查
---

# 代码审查准则
```

修改和重命名都会生成 Git 版本；恢复旧版本会再写入一个新版本，不改写旧历史。已启用准则会物化到 Agent harness 的 `.missioncrew/guidelines/<name>.md`。公共上下文只提供 `description`、内容版本、内部按需读取路径和 Web URL；Agent 判断相关时再读取全文。准则 URL 使用 `name`，不使用文件系统绝对路径。

主要 API：

- `GET /api/projects/<project>/guidelines`
- `POST /api/projects/<project>/guidelines`
- `GET /api/projects/<project>/guidelines/<name>/history`
- `GET /api/projects/<project>/guidelines/<name>/history/<revision>`
- `POST /api/projects/<project>/guidelines/<name>/restore`
- `DELETE /api/projects/<project>/guidelines/<name>`

### Skill

Skill 的事实源是项目托管 Skill 目录，每个 Skill 至少包含 `SKILL.md`，并可包含 `scripts/`、`references/`、`assets/` 等文件。项目支持 ZIP 上传、本地目录导入以及直接复制到项目 Skill 投放目录；完整格式和导入边界见 [项目 Skill 完整目录](skills.md)。

已启用 Skill 会映射到 Agent harness 的 `.missioncrew/skills/<skill-id>/`。Skill 根 URL 打开 `SKILL.md`，追加相对路径可以直接打开 Skill 内文件。该 URL 仅用于 Web 阅读；Agent 执行脚本或读取引用资料时仍使用 harness 中经过授权的真实目录。

主要 API：

- `GET /api/projects/<project>/skills`
- `GET /api/projects/<project>/skills/library`
- `POST /api/projects/<project>/skills`
- `GET /api/projects/<project>/skills/<skill-id>/file?path=<relative-path>`

### 项目文档

文档的事实源是项目文档工作树，独立 bare Git 仓库保存版本历史。聊天角色通过 `document.publish` Agent Tool 动作发布文件；直接编辑后的执行结束快照只作为旧会话兼容。结构化任务仍按任务工作区协议写入。人类也可以在 Web 文档页一次选择多个文件上传：未选中文档时保存到文档库根目录，选中文档时保存到该文档所在目录；同名文件必须确认后才能覆盖，且每个文件分别形成版本。单文件上限为 50 MB。

文本文件上传后可以继续在线编辑和预览。二进制或非 UTF-8 文件按原始字节保存，不会被文本转换；文档页提供当前版本和历史版本下载。

文档 URL 使用文档库内相对路径。平台发布 Agent 回复前会把已知的文档库真实路径转换成资源 URL，前端也兼容历史消息中保存的 MissionCrew 文档路径。普通业务源码绝对路径不会被自动转换成资源链接。

主要 API：

- `GET /api/projects/<project>/documents`
- `POST /api/projects/<project>/documents/upload?path=<relative-path>&overwrite=<bool>`（请求体为单个文件的原始字节）
- `GET /api/projects/<project>/documents/download/<relative-path>?revision=<revision>`
- `GET /api/projects/<project>/documents/file/<relative-path>`
- `PUT /api/projects/<project>/documents/file/<relative-path>`
- `GET /api/projects/<project>/documents/history?path=<relative-path>`

## Runtime 可见的文件资源

每次聊天角色或结构化任务执行都会获得独立的 `MISSIONCREW_WORKSPACE`。典型结构如下：

```text
.missioncrew/
├── README.md
├── project.md
├── documents/
├── tasks/
├── guidelines/
├── skills/
├── channel-history.json
├── .agent-tool-token
├── evidence/
└── runtime/
```

| 变量 | 用途 |
| --- | --- |
| `MISSIONCREW_PROJECT_URL` | 当前项目公开资源 URL 前缀 `/resources/<project>` |
| `MISSIONCREW_DOCUMENTS_URL` | 当前项目文档 URL 前缀 |
| `MISSIONCREW_WORKSPACE` | 当前角色或任务的 harness 根目录 |
| `MISSIONCREW_DOCUMENTS_DIR` | 项目文档读取入口；聊天写入使用 Agent Tool |
| `MISSIONCREW_TASKS_DIR` | 项目任务 Markdown 快照；聊天写入使用 Agent Tool |
| `MISSIONCREW_GUIDELINES_DIR` | 已启用准则的 Markdown 目录 |
| `MISSIONCREW_SKILLS_DIR` | 已启用 Skill 的完整目录视图 |
| `MISSIONCREW_CHANNEL_HISTORY` | 当前角色可见的频道历史 JSON；仅聊天执行 |
| `MISSIONCREW_AGENT_TOOL_URL` | 聊天角色调用统一平台动作的 API 根地址 |
| `MISSIONCREW_AGENT_TOKEN_FILE` | 当前频道和角色的 Agent Tool 令牌文件 |
| `MISSIONCREW_AGENT_RUN_ID` | 进程启动时的回合 ID；实际调用以最新 Prompt 为准 |

完整目录权限、同步和隔离边界见 [Agent harness 工作区与项目资料边界](agent-harness-workspace.md)。

## 不属于公开项目资源 URL 的内容

下列内容可能位于 MissionCrew 工作区或设置中，但没有项目资源 URL：

- `channel-history.json` 是供当前 Agent 按需读取的角色隔离历史，不是频道事实源的公开下载地址。
- `evidence/` 是结构化任务阶段产物目录；任务详情展示证据索引，但证据文件目前没有独立 Web 资源 URL。
- `runtime/` 是诊断和最近输出目录，不应出现在最终回复或业务提交中。
- 项目代码仓和普通本地路径属于 Runtime 授权资源，不由 `/resources/...` 暴露。
- Runtime、模型、全局设置和项目设置是系统页面，不是项目内容资源。

这一区分可以避免“允许 Agent 读取某个目录”被误解为“该目录可以通过 Web 公开访问”。

## API、聊天与 Web 的统一契约

### API

资源列表和写入结果返回 `resource_url`。该字段只用于导航，是只读派生值，不进入项目、准则或 Skill 的持久化模型。客户端把包含 `resource_url` 的项目对象回传时，后端会忽略该只读字段。

### Agent 与主控动作

项目公共上下文提供 `MISSIONCREW_PROJECT_URL`、六类资源格式以及当前准则和 Skill 的 Web URL。聊天角色通过带角色令牌的 Agent Tool API 显式创建频道、保存面板、准则、Skill、任务或文档以及发布消息；平台在同一回合返回结构化结果或错误，成功回执使用可点击 Markdown 链接。完整调用与权限契约见 [MissionCrew Agent Tool API](agent-tool-api.md)。

Agent 最终回复应使用：

```markdown
[可读标题](/resources/<project>/<resource-type>/<id-or-path>)
```

不要输出 `.missioncrew` 绝对路径、平台数据根路径或 `file://` 链接。

### Web

统一 Markdown 组件识别同源资源 URL，并交给应用路由处理。路由会恢复项目、资源类型和具体条目；文档与 Skill 内文件按路径逐段解码。用户在应用内切换频道、任务、面板、准则、Skill 或文档时，地址栏同步更新为规范资源 URL。

服务端对资源地址返回单页应用入口，资源是否存在由前端结合总览/API 数据验证。资源已经删除或标识无效时，前端不会尝试读取同名本地路径，而是回到对应资源类型的有效页面或显示找不到资源。

## 新增资源类型

增加新的项目内容资源时，应同时完成以下工作：

1. 在 `missioncrew/collab/resource_urls.py` 登记规范类型并提供 URL 构造函数。
2. 在资源 API 与 `/api/overview` 中返回派生的 `resource_url`，但不要把它写入持久化模型。
3. 在 Agent 公共上下文和相关平台动作回执中给出规范 Markdown 链接。
4. 在 `missioncrew/web/js/markdown.js` 中登记类型或兼容别名。
5. 在 `missioncrew/web/js/router.js` 中实现 URL 到页面状态、页面状态到规范 URL 的双向映射。
6. 验证资源链接可以点击、刷新、跨项目打开并支持浏览器前进/后退。
7. 补充 URL 编码、API `resource_url`、历史兼容和真实数据冒烟测试。

资源 URL 是公开导航契约。内部数据库表名、文件布局或 UI tab 可以重构，但已经发布到聊天历史中的规范 URL 应保持可解析。
