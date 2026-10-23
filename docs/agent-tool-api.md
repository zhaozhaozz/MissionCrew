# MissionCrew Agent Tool API

MissionCrew 把聊天角色对平台状态的修改收敛到一个 Runtime 无关的工具边界。Agent 不再通过最终回复中的特殊文本块请求平台猜测并执行动作，而是在执行回合内调用 HTTP API 或随服务安装的 CLI，立即获得成功结果或结构化错误，并可以根据错误继续处理。

这套边界适用于所有聊天 Runtime。Claude、Codex、ACP 和打印模式 CLI 都通过同一组环境变量、角色令牌和动作契约调用 MissionCrew，Runtime provider 不需要导入 Store、FastAPI 或各资源的内部实现。

对人类而言，这意味着频道、Task、文档、准则、Skill、面板与自动化脚本都可以由主控在对话中自动创建、查看和维护（包括修改与归档频道、归档 Task、读取面板/数据源/脚本的当前配置、试跑脚本）——人类在聊天里提需求，主控调用下文的动作完成并把资源链接贴回频道；普通角色也能创建和更新 Task、发布文档、创建频道，并发消息提及主控。Web 页面与 CLI 只是查看与人工干预的入口，不是这些资源唯一的录入方式。

## 调用链

```text
Runtime
  └─ missioncrew-tool / python -m missioncrew.agent_tool
       └─ Authorization: Bearer <channel×role token>
            └─ POST /api/agent/v1/actions
                 └─ 身份、scope、token 绑定 Run、参数与资源边界校验
                      └─ 统一动作注册表
                           ├─ 平台状态或文档版本写入
                           ├─ 资源 URL 结果
                           └─ 不含正文和令牌的审计记录
```

聊天上下文会注入：

| 环境变量 | 含义 |
| --- | --- |
| `MISSIONCREW_AGENT_TOOL_URL` | 当前服务的 Agent Tool API 根地址，默认是 `http://127.0.0.1:8321/api/agent/v1` |
| `MISSIONCREW_AGENT_TOKEN_FILE` | 稳定路径的逐 Run capability 文件；CLI 自行读取，Agent 不应打印或发布内容 |
| `MISSIONCREW_AGENT_TOOL_PYTHON` | 可以导入当前 MissionCrew 包的 Python 解释器 |

先查看当前角色获准执行的动作：

```bash
"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool actions
```

写调用不传 `run_id`；服务端从 Bearer token 确定当前 Run：

```bash
"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call task.create \
  --arguments '{"title":"补齐接口测试","summary":"覆盖错误返回","channel_ids":["general"],"labels":["api"]}'

"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool publish-file \
  --source reports/result.md \
  --path reports/result.md

"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call document.rename \
  --arguments '{"source":"reports/result.md","target":"archive/result.md"}'
```

CLI 共四个子命令：`actions`、`call <action> --arguments '<JSON>'`（参数较大时改用 `--arguments-file <文件>`，`--arguments-file -` 从 stdin 读，避免超出单个命令行参数的长度上限）、`publish-file --source <本地文件> --path <文档库路径> [--message <版本说明>] [--overwrite]`、`publish-message --channel <频道短 id> --content <正文> [--mention <角色 id>]...`；后两者分别是 `document.publish` 与 `message.publish` 的便捷封装。安装项目后也可以使用等价的 `missioncrew-tool` 命令。CLI 在成功时退出码为 0；API 错误、连接错误或客户端参数错误时退出码非零，并把 JSON 原样打印到 stdout，便于 Runtime 在同一回合读取、修正和重试。

## 身份、权限与生命周期

令牌按 `project × channel × role × run` 分配，而不是按 Runtime 分配，所以角色更换 Claude、Codex 或 ACP 后仍遵循同一权限。明文只写入该角色隔离工作区中路径稳定的 `.agent-tool-token`，文件权限为 `0600`；每个 Run 获得执行锁后原子替换文件内容。SQLite 不保存明文，只保存 SHA-256 哈希、随机 `token_id`、项目/频道/角色、`kind`（`run` 或 `automation`）、绑定的 `run_id`、作用域、签发/过期/最后使用/撤销时间。

令牌最多有效七天，但正常情况下会在对应 Run 结束时立即撤销并删除文件；下一 Run 在同一路径写入新的 capability。服务端从令牌直接得到 `run_id`，并确认该回合属于令牌中的频道和角色且状态仍是 `queued`、`running` 或 `waiting_user`。旧客户端提交的 `run_id` 只作为兼容校验字段，不能改变令牌绑定的 Run。因此获得旧令牌不足以在已结束回合中继续写入。

当前权限策略如下：

| 动作 | 普通角色 | 项目主控 |
| --- | --- | --- |
| `task.create` / `task.update` / `task.brief` | 允许 | 允许 |
| `task.delete` / `task.archive` / `task.restore` | 禁止 | 允许 |
| `document.publish` | 允许 | 允许 |
| `document.rename` | 允许 | 允许 |
| `document.history` | 允许 | 允许 |
| `document.delete` | 禁止 | 允许 |
| `message.publish` | 允许（`mentions` 只能是主控） | 允许 |
| `channel.runs.list` / `channel.run.stop` / `channel.history` | 禁止 | 允许 |
| `channel.create` | 允许 | 允许 |
| `channel.update` / `channel.archive` / `channel.restore` | 禁止 | 允许 |
| `dashboard.list` / `dashboard.save` / `dashboard.delete` | 禁止 | 允许 |
| `board_source.list` / `board_source.save` / `board_source.delete` | 禁止 | 允许 |
| `guideline.list` / `guideline.history` / `guideline.save` / `guideline.delete` | 禁止 | 允许 |
| `skill.list` / `skill.history` / `skill.save` / `skill.delete` | 禁止 | 允许 |
| `automation.list` / `automation.save` / `automation.run` / `automation.delete` | 禁止 | 允许 |
| `recycle.list` / `recycle.restore` / `recycle.purge` | 禁止 | 允许 |

项目未选择主控（无主控模式）时没有「普通角色」一列：每个角色都按「项目主控」列授权，但角色之间不能派发——`message.publish` 只能发消息，带非空 `mentions` 时返回 `permission_denied`（`actions` 列出的该动作说明也不含 `mentions` 参数）；角色只由人类点名启动，结果只留在频道。派发权限由 `Project.can_dispatch`（能否派发任意角色，只有主控为真）与 `Project.can_mention`（能否提及某个角色）判定。

`guideline.save` 与 Web 准则编辑器共用准则 Git 版本库。`skill.save` 同样把当前完整 Skill 目录提交到项目 Skill 版本库，因此已有 `scripts/`、`references/`、`assets/` 等辅助文件会与更新后的 `SKILL.md` 一起构成版本；两个动作成功后都返回 `revision`，并记录当前角色。准则重命名会继续原文件的历史链。删除属于主控权限：文档、准则、Skill 和面板都会进入项目统一回收站，文档、准则与 Skill 删除同时形成新 Git 提交，历史不会被抹除。`recycle.restore` 在原标识已被占用时返回 `already_exists` 并保留回收项；`recycle.purge` 是不可撤销的永久删除，只应在用户明确要求时调用。工具响应不会暴露回收目录的本地路径。

`message.publish` 的 `mentions` 是独立的角色 ID 数组，也是**唯一**的派发通道：只有数组中的合法角色会被调度，开启「仅人工点名」的角色对 Agent 视同不存在（返回 `role_not_found`，自动化脚本身份不受此限）；正文里出现的 `@reviewer`、`@[reviewer]` 等文本一律只是普通内容。主控的 Runtime 最终回复会由平台自动发布到当前 Channel；普通答复、结论和状态汇总不应再通过空 `mentions` 的 `message.publish` 重复发布。派工仍使用 `message.publish` 并显式传入目标 `mentions`。普通角色也能调用该动作，但 `mentions` 只能是主控（提及其他角色返回 `permission_denied`，`actions` 列出的说明会写明主控 id），用于把主控请进另一个频道（例如自己刚用 `channel.create` 建的频道）或在被人类直接点名时把后续工作交给主控；主控在那个频道处理，回复不会交回发起的普通角色，结果也不返回 `handoff`。主控这一轮的「本轮触发」会标明这是执行角色的显式请求而不是结果回报。普通角色不带 `mentions` 发出的消息只发布、不触发任何角色；它们仍看不到其他执行角色的名册。仅人工点名的角色不进 Agent 协作图，带 `mentions` 一律返回 `permission_denied`。至少一个角色实际启动时，结果还会返回 `handoff: "end_turn"` 和后续恢复说明；主控应立即在 Runtime 最终回复中简短说明已派发并结束当前 turn，不再为这条说明调用一次 `message.publish`；同时不用 `sleep` 或轮询频道、工作树、运行状态来等待，也不应向执行中的同一角色再次派发“报告中间状态”之类的消息。同一频道同一角色的持久会话不能在执行中插入第二个 turn，这类请求只会排在原任务后面，不能提供实时进度。角色完成或失败后，平台会自动启动新的主控 turn 并交回完整结果。协作链预算导致无人启动时，`dispatched` 为空且不会返回该 handoff。

当前 Channel 的活动 Run 状态不会直接注入主控上下文。人类询问当前运行情况或要求停止角色时，主控可按需调用只读动作 `channel.runs.list`，取得 `queued`、`running`、`waiting_user` 的一次性快照；每项包含 `run_id`、角色、执行组合、是否为当前主控 Run 以及是否可停止。随后可用 `channel.run.stop` 和选定的 `run_id` 停止同一 Channel 内的目标。服务端拒绝跨 Channel、已结束和当前主控自停请求；停止排队 Run 只取消队列项，不终止同角色的其他 Runtime。查询不产生会话回执，停止动作自身会生成 `agent_stop` 平台消息，因此也不重复生成工具回执。主控不应把按需查询用于等待循环。

`channel.runs.list` 与只读动作 `channel.history` 都接受可选的 `channel`（项目内频道短 id，默认当前 Channel），用于跨频道核对：主控在 #general 把任务派到专用频道后被停止、再次被唤起时，`message.publish` 发出的消息只存在于目标频道，源频道只有一条派发回执，此时用 `channel.runs.list` 看目标频道是否有活动运行、用 `channel.history` 读目标频道最近消息，就能判断执行到哪一步，避免重复开工。`channel.history` 返回 `limit`（默认 20，最多 200）条最新记录，`before_id` 向前翻页，记录格式与工作区 `channel-history.json` 一致（含 `agent_tool` 回执与 `agent_stop` 等平台消息），并附频道消息总数 `message_count`。跨频道查询到的运行 `stoppable` 恒为 false，`channel.run.stop` 仍只接受当前 Channel 的 `run_id`。

成功执行写操作后，平台会在发起调用的 Channel 会话中追加一条 `agent_tool` 类型的平台回执，显示调用角色、动作名和动作摘要。失败调用和 `channel.list`、`recycle.list`、各类 `*.list` 等只读调用不生成回执。`message.publish` 发到当前 Channel 时消息本身就是痕迹，不再插入回执；发到其他 Channel 时在发起调用的 Channel 插入回执，内容带目标频道链接与已派发名单（与工具返回的 `summary` 相同），`context.agent_tool` 额外记录目标 `channel` 与 `message_id`，这样主控被停止后再唤起也能从自己频道的历史看出已经派发。

### 自动化脚本身份（`kind=automation`）

除逐 Run 绑定的角色令牌外，还有一类**自动化脚本令牌**：`automation.save` 定义的项目脚本经 cron、webhook 投递或手动触发时，平台为该次运行签发一次性令牌（`kind=automation`，不绑定 Run），写入脚本工作目录的 `.agent-tool-token` 并在运行结束后立即撤销。脚本身份的可用动作以脚本自身的 `actions` 白名单为事实源（默认 `task.create`、`task.update`、`task.brief`、`message.publish`、`document.publish`、`dashboard.save`、`board_source.save`），与主控权限位无关；白名单只能包含除 `channel.runs.list`、`channel.run.stop`、`channel.history`、`automation.run`（避免脚本互相触发）之外的动作，`automation.save` 传入这些动作会返回 `invalid_arguments`。审计 actor 记为 `automation:<项目id>:<脚本短id>`，可与角色触发区分。

脚本身份的 `message.publish` 以 `author_type=automation` 发布：不传 `mentions` 时只发消息、不触发任何角色；显式提及单个角色时直接派发该角色（结果不自动交回主控），提及多个角色时与人类消息一致收敛为只启动主控。任何身份调用 `task.create` 时，若项目配置的 Task 自动处理规则命中 label，都会按规则的处理要求和处理角色立即派发（状态已是 `已完成` 的任务除外），结果附带 `auto_dispatch`（`rule_query`、`sent`）；脚本同步外部 Task 是最常见的场景。规则只能由人在任务看板列头的 ⚡ 配置，没有对应的 Agent Tool 动作，详见 [automation.md](automation.md)。

## 动作和并发规则

`GET /api/agent/v1/actions` 返回令牌身份与动作说明。`POST /api/agent/v1/actions` 请求格式为：

```json
{
  "action": "document.publish",
  "request_id": "publish-report-1",
  "arguments": {
    "path": "reports/result.md",
    "content": "# Result\n",
    "overwrite": false
  }
}
```

- 文档必须且只能提供 UTF-8 `content` 或 `content_base64`；单文件上限 50 MB。默认不覆盖已有文件，显式传 `overwrite: true` 才能覆盖并形成新版本。
- `document.rename` 使用文档库内的 `source` 和 `target` 相对路径；源文件必须存在、目标路径必须不存在。移动和仅修改文件名使用同一动作，并以一次 Git 提交保留原文件的历史链。若源文档已有页面对话绑定，该频道、消息和 Runtime 会话会迁移到新路径；若目标路径已绑定另一个页面对话，返回 `channel_conflict`。
- `task.delete`、`document.delete`、`dashboard.delete`、`guideline.delete` 和 `skill.delete` 都要求项目主控身份。目标不存在时返回 `task_not_found` 或 `not_found`，不会把删除不存在的资源误报为成功；成功结果包含 `recycle_item`。
- `recycle.list` 返回当前项目全部类型的回收项；`recycle.restore` 和 `recycle.purge` 使用回收项 `id`，均要求项目主控身份。
- `channel.runs.list` 与 `channel.history` 默认查询 token 绑定的当前 Channel，传 `channel` 可查项目内其他频道；`channel.run.stop` 只接受当前 Channel 查询返回的正整数 `run_id`，不能停止其他频道的运行或当前主控自身的 Run。
- `task.create`、`task.update`、`task.brief` 成功时结果都带 `task`，即写入后的完整任务（含新的 `updated_at`）。`updated_at` 是 MissionCrew 的系统更新时间：每次写入由平台自动刷新，不能设置，同时充当下文的版本号；同时立即刷新调用角色工作区里的 `tasks/` 快照，不必等下一轮。
- `task.update` 必须带 `snapshot_updated_at`，取值是任务当前的版本：`tasks/` 快照里的 `snapshot_updated_at` 字段，或上一次 `task.*` 结果里 `task` 的 `updated_at`。任务已经被其他执行更新时返回 `version_conflict`，防止旧快照覆盖新状态；此时平台已刷新快照，错误的 `details.task` 就是当前任务，核对变化后用它的 `updated_at` 重试即可。`task.brief` 不需要版本号，只记录进展或只改状态时用它更省事。
- `task.create` 和 `task.update` 使用 `title`、`summary`、`body`、`status`、`labels`、`channel_ids`、`url`、`meta`、`external_id`、`external_updated_at`；`status` 是状态文本（落库为 `status: 文本` 标签），标签支持 `属性: 值` 高级形式。Channel 绑定可选：`task.create` 不传 `channel_ids` 时绑定当前频道，`task.update` 传 `[]` 解除全部绑定；派发任务时才要求至少一个频道，没有绑定时用项目的 `general` 频道。`task.update` 的 `labels` 整体替换标签列表；只想改某个属性时用 `properties`（如 `{"owner": "张三", "priority": null}`），值可以是字符串或数组，`null` 清空该属性，其余标签不动；同时给了 `status` 时以 `status` 为准。
- `task.create` 的 `source` 指定任务所属的数据源：缺省为内置源 `built-in`，也可以是本项目自定义数据源的短 id（不存在时返回 `invalid_arguments`）。任务随后出现在绑定该源的 taskboard 面板上；没给状态时落到该源的第一个状态取值（内置源为 `待处理`）。内置源与自定义源的任务操作完全对等：`task.update`、`task.brief`、`task.archive`、`task.restore`、`task.delete` 都按任务 id 单个操作，不需要带上数据源里的其他任务。
- `url`（外部条目链接）、`meta`（看板卡片附注，字符串数组）和 `external_id`（外部条目 id，如 Issue 编号）对两类数据源的任务都可写，口径与 `board_source.save` 的卡片字段一致。`external_updated_at` 是外部系统里的更新时间（如 Issue 的最后更新时间），取 Unix 秒或带时区的 ISO 8601 字符串（如 `2026-10-19T08:00:00Z`），不带时区或疑似毫秒的值会被拒绝；看板优先显示并按它排序，`task.update` 传 `null` 清空。`external_id` 在同一数据源内唯一：已被占用时返回 `already_exists`（409），`error.details.task` 是已有任务，应改用 `task.update` 更新它；`task.update` 传空串去掉它。自定义源的整体同步按 `external_id` 匹配：用 `task.create` 登记外部条目时带上它（卡片会用的那个 id），下次 `cards` 里同 id 的卡片会更新这个任务而不是再建一张，之后的 `cards` 里没有它时任务会被删除；不带 `external_id` 的任务不参与整体同步。内置源没有整体同步，`external_id` 只用来识别外部条目、防止重复登记。
- `task.brief` 追加状态简报，可用 `status` 同时更新状态文本（如 `待处理`、`处理中`、`已阻塞`、`已完成`）。简报是追加记录，不覆盖正文。
- `task.delete` 把 Task 正文和全部状态简报一起移入项目回收站；恢复后保留原 Task id、字段、简报作者、内容和时间。Task 所属的自定义数据源已删除，或同一数据源里已有同一 `external_id` 的任务（被后来的同步重建，或又登记过一次）时，恢复会被拒绝。
- `board_source.save` 创建或更新自定义任务数据源（`columns` 声明状态取值，`cards` 按 id 整体同步为该源的任务：新增、更新、删除本次未出现的），供 `dashboard.save` 的 taskboard 面板通过 `source` 绑定；新任务会走项目自动处理规则。已有任务只更新卡片带了的字段与属性：没带 `status` 保留本地状态，`labels` 里的普通标签整组替换，`属性: 值` 标签与 `properties` 只替换同名属性（`null` 清空），合并规则详见 [automation.md](automation.md)。整体同步只管理带 `external_id` 的任务（由卡片 `id` 建出的，以及 `task.create` 带 `external_id` 登记的）；不带 `external_id` 的任务不参与匹配，也不会因为不在 `cards` 里而被删除。单个增删任务不要拿半份 `cards` 去保存。`board_source.delete` 在数据源仍被面板引用时返回 `in_use`，删除连带该源全部任务（包括 `task.create` 加进去的），不进回收站。
- `automation.save` 按短 id 新建或按字段合并更新脚本：`script` 是脚本全文（有 shebang 按可执行文件运行，否则用 bash），`cron` 是五段 crontab（空字符串 = 不定时），`enabled` 只管定时触发，`webhook` 为 `true` 时接受专用 webhook 端口的投递（可与 cron 并用，结果带 `webhook_path`；验签密钥只在人类的自动化页面显示，Agent 拿不到，需要请人取密钥配置外部系统，详见 [automation.md](automation.md)），`actions` 是脚本令牌的动作白名单，`timeout_seconds` 是单次运行超时。`automation.delete` 删除脚本并撤销其令牌、清除运行记录。`automation.run` 立即在后台手动触发一次（停用定时的脚本也可以），返回 `run_id`；同一脚本上一次还没跑完时返回 `in_use`。
- 不在 Agent 工作区里的配置用只读的 `*.list` 动作按需读取，改之前先读：`dashboard.list`（带 `id` 返回完整 layout/filters/group_by）、`board_source.list`（状态取值、任务数、绑定的面板）、`automation.list`（带 `id` 返回脚本全文与最近 `runs` 次运行，默认 5、最多 20，每次运行的 stdout/stderr 只回传末尾 4000 字符并标注是否截断；附该脚本保留的运行记录总数 `run_count`，传 `before_id` 只取 id 更小的记录，可向前翻页查完全部保留记录）、`guideline.list` / `skill.list`（含停用条目；带 `name` / `id` 返回完整 Markdown，Skill 另带包内文件清单，可原样交回 `guideline.save` / `skill.save` 修改或重新启用）。
- 文档、准则与 Skill 的 Git 历史保存在 Agent 工作区之外的独立版本库里，用只读的 `*.history` 读取：`document.history`（`path`）、`guideline.history`（`name`）、`skill.history`（`id`）。不带 `revision` 时按新到旧列出版本（`revision`、`created_at`、`actor`、`message`，默认 20 条、`limit` 最多 100；文档历史跟随重命名，已删除的文档也能查）；带 `revision` 时返回该版本内容：文档为 `content`（非 UTF-8 文件为 `content_base64`），准则为完整 `markdown`，Skill 为 `SKILL.md` 与文件清单，另传 `file` 读取包内单个文件。单个版本超过 5 MB 返回 `file_too_large`。没有单独的恢复动作：回滚就是读出旧版本再用对应的 `*.publish` / `*.save` 保存，形成新版本、历史保持完整。
- `channel.update` 修改频道名称、用途或工作目录（不传 `id` 时默认当前频道）；工作目录规则与 `channel.create` 相同，传空字符串改回平台内置工作区，新目录从该频道下一轮执行起生效。`channel.archive` 把频道归档为只读并停止其持久 Runtime，`general`、页面对话频道和仍有活动运行的频道（包括当前频道）会被拒绝（`general_channel`、`content_channel`、`in_use`）；`channel.restore` 取消归档。已归档或页面对话频道不能修改（`channel_archived`、`content_channel`）。Web 端侧栏频道菜单里的「编辑频道」与 `PATCH /api/chat/channels/{id}` 共用同一套规则。
- `task.archive` 把 Task 从看板和 `tasks/` 快照中收起，保留正文与简报；`task.restore` 恢复归档 Task。
- 频道、面板、准则和 Skill 的 ID、项目归属、工作目录和 Markdown 属性都在统一动作实现中校验。
- 涉及频道、文档、任务、准则、Skill、自动化和回收站的动作，成功结果包含规范 `/resources/...` URL（`resource_url`）；Agent 应把该 URL 放入频道回复，不应发布 `.missioncrew` 的真实路径。

成功响应：

```json
{
  "ok": true,
  "request_id": "publish-report-1",
  "action": "document.publish",
  "result": {
    "resource_url": "/resources/demo/documents/reports/result.md"
  }
}
```

失败响应始终包含机器可判断的 `code`、给 Runtime 阅读的 `message` 和是否适合重试的 `retryable`；能帮调用方当场恢复时另带 `details`（如 `version_conflict` 的当前任务）：

```json
{
  "ok": false,
  "request_id": "publish-report-1",
  "error": {
    "code": "already_exists",
    "message": "文档已存在: reports/result.md",
    "retryable": false
  }
}
```

常见错误包括 `missing_token`、`invalid_token`、`expired_token`、`run_mismatch`、`run_inactive`、`permission_denied`、`invalid_request`、`invalid_arguments`、`already_exists`、`version_conflict` 和 `internal_error`。资源类错误还包括 `not_found`、`task_not_found`、`task_archived`、`channel_not_found`、`channel_archived`、`content_channel`、`general_channel`、`unavailable`、`role_not_found`、`run_not_found`、`cannot_stop_self`、`file_too_large`、`channel_conflict`、`in_use`、`unsupported_action`；CLI 在无法连接或本地参数错误时自行生成 `connection_failed` / `client_error`。

## 审计与安全边界

每次动作调用都会写入 `agent_tool_called` 审计项，记录项目、频道、角色、`token_id`、`run_id`、`request_id`、动作、结果状态和错误码。审计详情不保存 Bearer token、消息正文、文档正文或完整参数；资源自己的创建、修改和发布仍写各自的领域审计项。

角色令牌提供的是 MissionCrew 应用层身份、最小权限和追溯边界，不是针对恶意本地进程的操作系统沙箱。当前服务的普通 Web API 没有公网身份验证，YOLO Runtime 也可能拥有主机网络和文件权限；因此服务仍应只运行在可信网络，并通过 Runtime 文件系统/网络策略、主机防火墙或反向代理限制不可信执行。不能把 Bearer scope 描述成对同一主机上任意进程都不可绕过的强隔离。

## 兼容迁移

历史 `missioncrew-action` 文本块仍可读取，避免旧的持久 Runtime 会话或历史测试立即失效；解析后只转发到同一个动作注册表，不再拥有独立写入逻辑。其中 `post_message` 与 `message.publish` 同契约：只有块内显式携带 `mentions` 数组才会派发，正文里的 `@[角色ID]` 不再触发任何执行。新上下文不会要求 Runtime 生成该格式，旧入口也无法像工具调用一样把错误返回给同一 Agent 回合，因此只作为迁移兼容，不应新增依赖。

聊天角色直接编辑 `.missioncrew/documents/` 的执行后同步仍属于兼容路径；新实现应调用 `document.publish`。`.missioncrew/tasks/` 只是只读快照，Task 只有在 Agent 显式调用 `task.create`、`task.update`、`task.brief` 或 `task.delete` 时才会改变。Task 处理统一进入 Channel 协作，不存在独立的任务阶段 Runtime。

文档库没有独立的目录资源或目录动作。目录树由各文件的相对路径隐式形成：发布 `a/b/c.md` 后，Web 会展示目录 `a/b` 下的 `c.md`。创建目标父目录由 `document.publish` / `document.rename` 自动完成；空目录不会进入文档清单或 Git 历史，也没有单独的创建、移动、重命名或删除接口。
