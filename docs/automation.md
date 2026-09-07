# 定时自动化与任务自动处理

两个机制拼成一条自动化流水线：

1. **自动化脚本（Automation）**：按 crontab 定时（或手动）触发一个脚本，平台为该次运行签发一次性 Agent Tool 令牌，脚本用它调用平台动作——创建任务、同步数据源、发消息、发布文档等，也就是"脚本更新 MissionCrew"的能力。
2. **任务自动处理规则（Task 自动规则）**：新建 Task 的标签命中某条规则的标签表达式时，平台自动把它派发进频道交给角色处理，与人工「交给主控处理」完全同构。

两者组合，外部系统的条目可以全自动流转：**GitHub/GitCode Issue、PR、告警 → 定时脚本同步成 Task → 规则命中 → 自动派发给角色 → Agent 在频道里处理 → 结果经 `task.brief`/`task.update` 回写**。人类只在频道里看进展、必要时介入。

## 自动化脚本

- **创建**：Web 侧栏「自动化」的 ＋，或让主控在聊天里用 `automation.save` 建。字段：`id`、`name`、`description`、`script`（带 shebang 时按可执行文件运行，否则交给 bash）、`cron`（五段 crontab，空 = 仅手动触发）、`enabled`、`actions`、`timeout_seconds`。
- **执行**：所有 cron 触发都走统一定时入口调度；脚本在独立工作目录运行，平台把一次性令牌写入该目录的 `.agent-tool-token`，并注入 `MISSIONCREW_AGENT_TOKEN_FILE`、`MISSIONCREW_AGENT_TOOL_URL`、`MISSIONCREW_AGENT_TOOL_PYTHON`（平台自身的 Python 解释器）等环境变量，运行结束立即撤销令牌。脚本内调用平台动作的方式：

  ```bash
  "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call <action> --arguments '<JSON>'
  ```

- **权限**：脚本身份的可用动作以自身 `actions` 白名单为事实源，默认 `task.create`、`task.update`、`task.brief`、`message.publish`、`document.publish`、`dashboard.save`、`board_source.save`；审计 actor 记为 `automation:<项目>:<脚本>`。
- **运行记录**：自动化详情页可查看每次运行的触发方式、退出码与 stdout/stderr；停机期间错过的触发不补跑。

## 外部列表同步成任务（数据源）

`board_source.save` 创建或更新一个自定义任务数据源：`columns` 声明状态取值（顺序与颜色），`cards` 按 id **整体同步**为该源的任务——新增、覆盖、删除本次未出现的条目。每张卡片可带 `title`、`summary`、`status`（状态文本，落库为 `status: 文本` 标签）、`labels`（支持 `属性: 值` 高级标签，如 `owner: 张三`）、`url`（原始链接）、`meta`。

同步进来的条目就是普通 Task：可以打开详情、追加状态简报、绑定频道、被派发；**新同步的任务会立即走项目自动处理规则**。再用 `dashboard.save` 建一块 taskboard 面板绑定该源即可查看，列用标签表达式（`filters`）定义，或传 `group_by` 按属性取值动态分列（此时 `filters` 的并集只划定卡片范围）。

## 任务自动处理规则

规则存在项目上，每条 = **标签表达式 + 处理要求 + 可选的 @角色提及 + 启停开关**。配置入口是任意任务看板（内置或自定义）列头的 ⚡ 图标，表达式预填该列的查询。

- **触发时机**：任务新建时——人在页面建、Agent 用 `task.create` 建、数据源同步新建，标签命中第一条启用规则即派发。
- **派发行为**：在任务绑定的频道（缺省项目 `general`）发一条带任务详情的消息；提及单个角色则该角色直接执行，多个角色或未提及则交给项目主控调度，后续完全复用聊天协作。
- **表达式语法**：整串标签匹配（`bug`、`owner: 张三`）、`属性: *` 存在性匹配，用 `&`、`|`、`!` 与括号组合。例：`bug & !status: 已完成`（未完成的缺陷）、`!owner: *`（还没有负责人）。

## 闭环示例：自动处理 GitCode/GitHub Issue

1. **同步脚本**：建一个 cron `*/30 * * * *` 的自动化脚本，用 `gitcode`/`gh` CLI 拉取仓库 Issue 列表，整理成 cards 后调用 `board_source.save`。Issue 编号作卡片 id，Issue 标签与指派人写成 `labels`（如 `bug`、`owner: xxx`），链接写 `url`。
2. **看板**：用 `dashboard.save` 建 taskboard 绑定该源，按状态或负责人分列查看。
3. **规则**：在看板列头 ⚡ 配置，如表达式 `bug & status: 待处理`，处理要求「@dev 请分析并修复，完成后追加状态简报」。
4. **自动流转**：新 Issue 被同步成 Task → 命中规则 → 自动派发给 dev → dev 在频道内分析修复（也可以用 `gh`/`gitcode` CLI 把结论回写到外部系统）→ 用 `task.brief`/`task.update` 更新任务状态。

PR 审查、告警值守同理：任何能写成「外部列表 → cards」的数据都能进入这条流水线，处理要求和角色选择决定它被怎样消化。

## 相关文档

- 动作清单、参数与权限模型：[agent-tool-api.md](agent-tool-api.md)
- Agent 工作区与任务快照边界：[agent-harness-workspace.md](agent-harness-workspace.md)
