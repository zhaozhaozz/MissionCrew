# 定时自动化与任务自动处理

两个机制拼成一条自动化流水线：

1. **自动化脚本（Automation）**：按 crontab 定时、由外部系统的 webhook 投递或手动触发一个脚本，平台为该次运行签发一次性 Agent Tool 令牌，脚本用它调用平台动作——创建任务、同步数据源、发消息、发布文档等，也就是"脚本更新 MissionCrew"的能力。
2. **任务自动处理规则（Task 自动规则）**：新建 Task 的标签命中某条规则的标签表达式时，平台自动把它派发进频道交给角色处理，与人工「交给角色处理」完全同构。

两者组合，外部系统的条目可以全自动流转：**GitHub/GitCode Issue、PR、告警 → 定时或 webhook 触发的脚本同步成 Task → 规则命中 → 自动派发给角色 → Agent 在频道里处理 → 结果经 `task.brief`/`task.update` 回写**。人类只在频道里看进展、必要时介入。

## 自动化脚本

- **创建**：Web 侧栏「自动化」的 ＋，或让主控在聊天里用 `automation.save` 建；主控改脚本前用 `automation.list`（带 `id`）读取当前全文，改完可用 `automation.run` 立即试跑。字段：`id`、`name`、`description`、`script`（带 shebang 时按可执行文件运行，否则交给 bash）、`cron`（五段 crontab，空 = 不定时）、`enabled`（只管定时触发）、`webhook`（是否接受 webhook 投递，可与 cron 同时开启，见下文「Webhook 触发」）、`actions`、`timeout_seconds`。手动触发始终可用。
- **调度**：所有 cron 触发都走统一定时入口；同一脚本同时只运行一次，上一次还没跑完时定时与手动触发直接跳过，webhook 投递排队；停机期间错过的定时触发不补跑。
- **权限**：脚本身份的可用动作以自身 `actions` 白名单为事实源，默认 `task.create`、`task.update`、`task.brief`、`message.publish`、`document.publish`、`dashboard.save`、`board_source.save`；审计 actor 记为 `automation:<项目>:<脚本>`。
- **运行记录**：自动化详情页可查看每次运行的触发方式（定时 / Webhook / 手动，webhook 另记来源、事件名与投递 id）、退出码与 stdout/stderr。退出码非零记为失败。运行记录保留 7 天，不限条数；每个脚本最近一次的记录始终保留。主控用 `automation.list` 带 `id` 读取最近几次运行（输出只回传末尾），需要更早的记录时用 `before_id` 向前翻页，据此排查失败的脚本。

### 运行环境

- **工作目录**：`<数据目录>/automations/<项目 id>/<脚本短 id>/`，跨次运行保留，可以存放状态文件（例如上次同步的时间）。每次运行前平台把脚本正文写成该目录下的 `script`，不要用这个文件名保存别的内容。
- **环境变量**：脚本继承 MissionCrew 服务进程的环境，`PATH` 以及 `gh`、`gitcode` 等 CLI 的登录状态都来自启动服务的用户；平台另外注入：

  | 变量 | 含义 |
  | --- | --- |
  | `MISSIONCREW_AGENT_TOOL_PYTHON` | 平台自身的 Python 解释器，用它运行 `-m missioncrew.agent_tool` |
  | `MISSIONCREW_AGENT_TOOL_URL` | Agent Tool API 根地址 |
  | `MISSIONCREW_AGENT_TOKEN_FILE` | 本次运行的一次性令牌文件（工作目录下的 `.agent-tool-token`）；CLI 自行读取，脚本不需要读取，也不要打印 |
  | `MISSIONCREW_PROJECT_ID` | 项目 id |
  | `MISSIONCREW_AUTOMATION_ID` | 脚本 id，格式 `<项目 id>:<脚本短 id>` |
  | `MISSIONCREW_TRIGGER` | 本次运行的触发来源：`cron`、`webhook` 或 `manual`（人在页面点「立即运行」或主控 `automation.run`） |
  | `MISSIONCREW_WEBHOOK_EVENT_FILE` | 仅 webhook 触发：本次事件文件的路径，格式见下文「Webhook 触发」 |
  | `MISSIONCREW_WEBHOOK_PROVIDER` / `MISSIONCREW_WEBHOOK_EVENT` / `MISSIONCREW_WEBHOOK_DELIVERY` | 仅 webhook 触发：来源（`github`、`gitcode`、`gitlab` 或空）、事件名（如 `issues`、`Issue Hook`）与投递 id，便于脚本直接按事件分支 |

- **令牌与超时**：令牌在运行结束时立即撤销，超时被终止时同样撤销；运行超过 `timeout_seconds` 的脚本会被终止并记为超时。
- **调用平台动作**：

  ```bash
  "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call <action> --arguments '<JSON>'

  # 参数较大（例如整批同步卡片）时从文件或 stdin 读，避免超出单个命令行参数的长度上限
  jq -c '{id: "github", cards: .}' cards.json \
    | "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call board_source.save --arguments-file -
  ```

  CLI 成功时退出码为 0；平台返回错误、无法连接或参数不合法时退出码非零，并把错误 JSON 打印到 stdout。bash 脚本开头加 `set -euo pipefail`，任何一步失败都会让本次运行失败并留下记录。

## Webhook 触发

开启 `webhook` 的脚本可以由外部系统（GitHub、GitCode、GitLab 或自建服务）推送事件触发，事件一到就运行，不必等下一次定时；同一个脚本可以同时保留 cron 做定期兜底。

- **专用端口**：`mc serve` 另起一个只接收 webhook 的端口，默认 `8322`（`--webhook-port` 或环境变量 `MISSIONCREW_WEBHOOK_PORT` 修改，`0` 关闭），监听地址默认与 `--host` 相同，可用 `--webhook-host` 或 `MISSIONCREW_WEBHOOK_HOST` 单独指定。这个端口上只有 `POST /hooks/<项目 id>/<脚本短 id>` 一条路由：其他路径一律 404、其他方法 405，不提供页面、API 或静态文件。主服务没有身份验证，需要让外部系统访问时只转发 webhook 端口（端口转发、隧道或反向代理），不要暴露主服务端口。端口被占用时主服务照常启动，自动化详情页的「Webhook」区显示未监听的原因。
- **地址与密钥**：开启后在自动化详情页的「Webhook」区查看投递地址与密钥，可以复制或重新生成（重新生成后旧密钥立即失效，外部系统要同步修改）。密钥在首次开启时生成，关闭 webhook 后保留。它只在这个页面显示，总览数据和 Agent Tool 的结果里都没有：主控用 `automation.save` 开启 webhook 后，应把投递路径告诉人，由人到页面取密钥去配置外部系统。
- **验签**：每次投递至少带下表中的一种凭据，并且用本脚本的密钥验得过，否则返回 401。带了多种时任一种验过即可：经转发服务送来的请求常常原样保留上游平台的签名头（例如 GitHub 的 `X-Hub-Signature-256`，它是用上游平台的密钥签的），这种验不过的签名不会导致拒收，只要同一请求里还有一种凭据验得过。

  | 方式 | 请求头 | 适用 |
  | --- | --- | --- |
  | HMAC-SHA256 签名（对原始正文计算） | `X-Hub-Signature-256: sha256=<hex>` | GitHub（Secret 填密钥）、Gitea 及自建服务 |
  | HMAC-SHA256 签名 | `X-GitCode-Signature-256: sha256=<hex 或 base64>` | GitCode 签名密钥模式 |
  | Standard Webhooks 签名 | `webhook-id`、`webhook-timestamp`、`webhook-signature: v1,<base64>`（`svix-*` 同义） | 遵循 [Standard Webhooks](https://www.standardwebhooks.com/) 规范的发送方、签名库与转发服务 |
  | 密码 | `X-GitCode-Token`、`X-Gitlab-Token` 或 `Authorization: Bearer <密钥>` | GitCode 密码模式、GitLab 的 Secret token、能配置附加请求头的转发服务与自建服务 |

  Standard Webhooks 签名的内容是 `<webhook-id>.<webhook-timestamp>.<原始正文>`，时间戳与平台时间相差超过 5 分钟即拒收（防重放）。新生成的密钥是规范格式 `whsec_<base64>`，规范的签名库可以直接使用，签名时用 base64 解码后的字节；早先生成的非 `whsec_` 密钥用原文字节（签名库的 raw 格式）。GitHub 风格签名与密码方式始终把整串密钥当作原文。

- **请求与响应**：正文支持 JSON 与 GitHub 的表单格式（`payload=<json>`），上限 25 MiB（超出返回 413），声明为 JSON 却解析失败返回 400。接受后立即返回 `202 {"ok": true, "run_id": <运行记录 id>}`，脚本在后台运行；GitHub 的 `ping` 事件由平台直接回 `200 {"ok": true, "pong": true}`，不运行脚本。脚本不存在、不属于该项目或没开 webhook 时一律 404，不区分原因。
- **排队**：脚本正在运行时到达的投递按到达顺序排队，返回 `202 {"ok": true, "queued": true}`；每个脚本最多排 20 条，满了返回 503。本次运行结束后接着跑下一条，排到时以脚本最新定义为准：这期间关掉 webhook 或删除脚本，排队的事件会被丢弃并记审计。队列只在内存里，服务重启时丢失，所以需要不漏事件的同步要靠 cron 定期全量兜底。
- **事件文件**：webhook 触发的运行里，事件写在工作目录下的 `.webhook-event.json`（路径即 `MISSIONCREW_WEBHOOK_EVENT_FILE`），运行结束即删除：

  ```json
  {
    "provider": "github",
    "event": "issues",
    "delivery": "<投递 id>",
    "received_at": 1760000000.0,
    "content_type": "application/json",
    "headers": {"user-agent": "GitHub-Hookshot/...", "x-github-event": "issues"},
    "payload": {"action": "opened", "issue": {"number": 42}},
    "body": null
  }
  ```

  `provider` 按事件名头识别（`X-GitHub-Event`、`X-GitCode-Event`、`X-Gitlab-Event`），都没有时为空串，这时 `delivery` 取 Standard Webhooks 的 `webhook-id`；`headers` 是键为小写的请求头，已去掉携带密钥的 `Authorization`、`X-GitCode-Token`、`X-Gitlab-Token`；`payload` 是解析后的 JSON，正文不是 JSON 时为 `null`，原文放在 `body`。
- **过滤交给脚本**：平台不按事件类型、仓库或动作过滤，脚本读事件后对不关心的投递直接 `exit 0`。这样的运行也会留一条记录，便于排查到过哪些事件。

示例：Issue 打开、关闭或重新打开时立即同步，其余事件忽略；cron 每 30 分钟兜底一次。注意 `board_source.save` 是整体同步，webhook 触发时也要拉全量再保存，不能只拿事件里那一条 Issue 去保存，否则其他任务会被删除。

```bash
#!/usr/bin/env bash
set -euo pipefail

if [ "$MISSIONCREW_TRIGGER" = webhook ]; then
  [ "$MISSIONCREW_WEBHOOK_EVENT" = issues ] || { echo "忽略事件 $MISSIONCREW_WEBHOOK_EVENT"; exit 0; }
  action=$(jq -r '.payload.action' "$MISSIONCREW_WEBHOOK_EVENT_FILE")
  case "$action" in
    opened|closed|reopened) ;;
    *) echo "忽略 issues.$action"; exit 0 ;;
  esac
fi

# 定时、手动与命中的 webhook 走同一段全量同步，写法见下文「闭环示例」
```

## 外部列表同步成任务（数据源）

`board_source.save` 创建或更新一个自定义任务数据源：`columns` 声明状态取值（顺序与颜色），`cards` 按 id **整体同步**为该源的任务——新增、更新、删除本次未出现的条目。

> **本次 `cards` 里没有的任务会连同全部状态简报永久删除，不进回收站。** 所以拉取外部列表失败时脚本必须非零退出，不能拿空列表或半份列表去保存；分页拉取要拉全；卡片 `id` 的生成规则要跨次保持不变，否则每次都会删掉旧任务、再新建一份。

整体同步只管理带 `external_id` 的任务：由卡片 `id` 建出的，以及用 `task.create` 带 `external_id` 登记进来的。要在数据源里单独加、改、删一个任务，和内置任务一样用 `task.create`（传 `source`）、`task.update`、`task.delete`。单独登记外部列表里的条目（例如先补一个 Issue，不等下次同步）时，带上 `external_id`（卡片会用的那个 id）和 `url`：下次整体同步时同 id 的卡片会更新这个任务，不会再建一份；之后同步的 `cards` 里没有这个 id 时，任务会被删除。`external_id` 在同一数据源内唯一，重复登记会返回 `already_exists`。不带 `external_id` 的任务，整体同步既不会更新它，也不会因为它不在 `cards` 里而删除它。

### 卡片字段

| 字段 | 格式 | 说明 |
| --- | --- | --- |
| `id` | 字符串，必填 | 外部条目的稳定标识，例如 `#231`；同步按它匹配已有任务 |
| `title` | 字符串 | 新建任务时必填，更新时可省略 |
| `summary` | 字符串 | 简介 |
| `status` | 字符串 | 状态文本，落库为 `status: 文本` 标签；合并规则见下文 |
| `labels` | 字符串数组 | 普通标签（如 `bug`）或 `属性: 值` 高级标签（如 `owner: 张三`） |
| `properties` | 对象 | `{属性: 值 \| [值...] \| null}`，单独设置某个属性，`null` 清空 |
| `url` | 字符串 | 原始链接；Agent 在任务快照的 `url` 字段和自动派发消息里能看到它 |
| `meta` | 字符串数组 | 卡片上显示的附注文本，例如 `["@alice", "3 条评论"]` |
| `updated_at` | Unix 秒或带时区的 ISO 8601 字符串 | 外部系统里的更新时间，存为任务的 `external_updated_at`，看板优先显示并按它排序；没带时保留原值，与 MissionCrew 自动记录的系统更新时间分开 |

`columns` 是 `[{value, color}]` 数组：新建数据源时不传，默认 `待处理`、`处理中`、`已阻塞`、`已完成` 四个状态；更新时不传保留原有状态取值，传了就整体替换。状态文本本身不做校验，可以写 `columns` 之外的值。页面独立的「按状态分列」视图会保留这些未声明状态，并把缺少状态的任务放在「未设置」列；自定义表达式列仍只显示命中各列表达式的任务。

### 合并规则

已有任务按「**卡片带了才改，没带保留**」合并，所以 Agent 或人在本地推进的状态、补充的属性不会被下一次同步冲掉：

- `title`、`summary`、`url`、`meta`：带了才覆盖；值为 `null` 等同没带。
- `status`：带了才设置；没带时保留本地状态，新建任务落到第一个状态取值。脚本只在外部源有明确结论时带上它，比如 Issue 已关闭时带 `已完成`。
- `labels` 里的普通标签（如 `bug`）：只要卡片带了 `labels` 字段，任务的普通标签就整组以卡片为准，外部源删掉的标签会同步删掉；`labels` 为空数组或只有 `属性: 值` 时，普通标签会全部清掉。不想让同步管理普通标签就不要带 `labels`；本地要长期保留的标记请用 `属性: 值` 形式，并避开脚本会带的属性名。
- `labels` 里的 `属性: 值` 与 `properties`（`{属性: 值 | [值...] | null}`）：只替换同名属性，`null` 清空，没提到的属性不动。同一属性同时出现时，`status` 字段优先于 `properties`，`properties` 优先于 `labels`。
- 内容（含外部更新时间）没有变化的任务不写库，系统 `updated_at` 不变，不会让 Agent 手里的 `task.update` 快照失效。

换句话说，脚本带上的字段归外部源管理，每次同步都会改回外部的值；处理任务的 Agent 应把进展记在状态、`属性: 值` 标签和状态简报里，只要脚本不带这些字段，它们就会一直保留。

同步进来的条目就是普通 Task：可以打开详情、追加状态简报、绑定频道、被派发；**新同步的任务会立即走项目自动处理规则**。同步任务在派发前没有绑定频道。用 `task.delete` 删掉的同步任务，只要外部条目还在，下次同步就会重新建出来；这时再从回收站恢复旧任务会返回冲突。再用 `dashboard.save` 建一块 taskboard 面板绑定该源即可查看。共享配置仍用 `filters` 定义自定义表达式列，`group_by` 定义属性分列；页面上的个人筛选与显示偏好按下文独立处理。

### 任务看板筛选与分列

内置任务看板与自定义任务看板使用同一套交互，显示范围可选未归档、已归档或全部：

- **搜索**：不区分大小写地匹配标题、简介、任务 ID、外部编号、标签及卡片附注（`meta`）中的文本，不检索任务正文。
- **多选筛选**：同一属性的多个取值按「或」匹配，不同属性之间按「且」匹配；属性可以选择「未设置」。多个普通标签按「或」匹配，并与属性筛选共同生效。例如同时选择 `owner: alice`、`owner: bob` 和 `priority: high`，表示负责人是 alice 或 bob，且优先级为 high。已选条件显示在看板上，可逐项移除或一键清除。
- **高级标签表达式**：需要 `!`、括号等组合时可输入表达式；它作为独立的「且」条件，与搜索和多选筛选共同限制结果，不会改写共享的表达式列。
- **分列**：通过下拉框选择状态、数据里已有的属性（如 `owner`、`priority`），或自定义表达式列。首次打开时，已有 `group_by` 的看板继承该属性；配置了额外或自定义表达式列的看板保留表达式视图；标准新看板按状态分列。之后以本浏览器保存的个人偏好为准。属性分列只改变组织方式，不使用共享表达式列的并集暗中限制任务范围；同一任务有多个属性取值时，可以出现在多个对应列中。
- **编辑表达式列**：分列选中自定义表达式列时，下拉框旁出现 ✎ 按钮，打开共享表达式列的编辑对话框。对话框里可以修改列名和表达式、增删列，并拖动 ⠿ 或用 ↑ ↓ 调整列顺序；右侧按当前显示范围（不含个人筛选）实时预览正在编辑的列匹配哪些任务，并统计有多少任务不在任何列中，下方附表达式语法说明。修改点「保存」后一次性提交。内置看板的四个状态列不可删除或改写，但可以排序；绑定了 ⚡ 自动处理规则的列不能改表达式，也不能移除。
- **显示**：列内默认按最近更新排序，也可选择最早更新、系统最近更新、最近创建或标题。「更新」取外部系统的更新时间（同步卡片的 `updated_at` 或 Agent 设置的 `external_updated_at`），没有时退回 MissionCrew 系统更新时间，卡片上会标为「系统更新」；「系统最近更新」只看 MissionCrew 自动记录的时间。任务详情页同时列出更新时间、系统更新时间与创建时间，系统时间只读。可隐藏空列，单独折叠列或全部折叠/展开。折叠列保留列名和数量，方便重新展开。

归档范围、搜索、筛选、分列、排序、空列显示和折叠状态是**个人视图偏好**，按项目、看板和数据源保存在当前浏览器中，重新打开页面时恢复，不回写项目或看板的共享配置。通过分列旁的 ✎ 修改表达式列，以及通过列头 ⚡ 配置自动处理规则，仍会更新共享配置；这些配置与临时筛选的用途不同。

上述交互参考 [Linear 的显示选项](https://linear.app/docs/display-options)、[GitHub Projects 的筛选语义](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/filtering-projects)和 [Trello 的列表折叠](https://support.atlassian.com/trello/docs/collapse-or-expand-a-list/)。

#### 只读视图参数

内置看板 `GET /api/projects/<project>/builtin_board/data` 和自定义任务看板 `GET /api/projects/<project>/boards/<board-id>/data` 接受同一组参数：

| 参数 | 行为 |
| --- | --- |
| `scope` | `active`（默认）、`archived` 或 `all`，指定当前归档范围 |
| `view_group_by` | 显式非空时按该属性独立分列，例如 `status`、`owner`；候选任务来自数据源的当前 `scope`，不先取共享 `filters` 的并集。显式传空值（`view_group_by=`）切回已保存的表达式列。省略此参数则保留旧 API 行为：沿用共享 `group_by`，它非空时仍先取 `filters` 并集再分列 |
| `filter_query` | 额外的高级标签表达式，与以下快筛条件按「且」组合 |
| `search` | 上述普通文本搜索，与其他条件按「且」组合 |
| `filter_label`（可重复） | 精确标签值，如 `owner: alice`；同属性取值之间按「或」，不同属性之间按「且」，所有普通标签共同作为一个按「或」匹配的维度 |
| `missing_property`（可重复） | 匹配缺少某属性的任务，例如 `owner`；与该属性已选取值按「或」组合，再与其他筛选维度按「且」组合 |

这些参数不会持久化修改项目或面板。标签匹配沿用平台的标签归一化并忽略大小写，不把标签值当表达式解析；复杂表达式使用 `filter_query`。列与可选标签取自当前归档范围，之后再应用快筛，所以筛选不会使其他可选条件消失。响应的 `counts` 始终统计整个数据源的各归档范围；`matched_count` 对快筛命中的任务去重，表达式模式下也包括未匹配任何列表达式的任务。表达式列可以重叠或未覆盖所有任务，不能把各列数量相加当作任务总数。

## 任务自动处理规则

规则存在项目上，每条 = **标签表达式 + 处理要求 + 可选的 @角色提及 + 启停开关**。规则对项目内所有数据源的任务生效，不属于某块看板。

- **配置入口**：侧栏「面板 → 自动处理规则」页（挂在内置任务看板下，项目有规则后才显示）列出全部规则并按顺序编号，只能新建和编辑，不提供筛选或分组；任意任务看板（内置或自定义）列头的 ⚡ 图标、看板「显示选项 → 新建自动处理规则」以及面板里任务看板组件的 ⚡ 打开同一个规则编辑器，从列头打开时条件预填该列的表达式，已有同条件规则则直接编辑它。规则只能由人在页面上配置，Agent Tool 没有对应动作；Agent 被要求设置规则时，应告诉人去上述入口设置，并给出建议的表达式和处理要求。
- **规则编辑器**：条件可在「可视化」与「表达式」两种模式间切换。可视化模式按行编辑，整体选择满足「全部」或「任一」条件；每行选标签（包含 / 不包含）、状态或某个属性（是 / 不是 / 已设置 / 未设置），候选取自项目各数据源已有的标签。括号嵌套或与、或混用的条件只能在表达式模式编辑。右侧预览列出项目所有数据源当前未归档的任务中命中条件的任务，并标出去向：本规则处理、已完成不派发，或由排在前面的规则先处理。预览只是演示命中范围，规则本身只对之后新建的任务生效。同一条件只能有一条规则；条件保存为规范写法，看板列与规则按规范写法对应，不受空格、`&&` 等书写差异影响。
- **触发时机**：任务新建时——人在页面建、Agent 用 `task.create` 建、数据源同步新建，按规则列表顺序取第一条命中的启用规则派发；状态已是 `已完成` 的新任务不派发。已有任务后来改了标签，即使命中规则也不会触发。`task.create` 触发派发时，返回结果带 `auto_dispatch`（`rule_query`、`sent`）。
- **派发行为**：在任务的首个绑定频道发一条带任务详情（含原始链接）的消息，只发这一个频道，任务没有绑定频道时用项目的 `general` 频道，派发后任务状态改为 `处理中`（自定义数据源没有声明 `处理中` 这个状态取值时保持原状态，遵循该数据源声明的工作流）；提及单个角色则该角色直接执行，多个角色或未提及则交给项目主控调度，后续完全复用聊天协作。收到这条消息的 Agent 会看到它来自任务自动处理规则。
- **表达式语法**：整串标签匹配（`bug`、`owner: 张三`）、`属性: *` 存在性匹配，用 `&`、`|`、`!` 与括号组合。不带冒号的项既匹配同名普通标签，也匹配以它为属性名的标签：`owner` 等价于 `owner | owner: *`。例：`bug & !status: 已完成`（未完成的缺陷）、`!owner` 或 `!owner: *`（还没有负责人；前者同时排除带普通标签 `owner` 的任务）。

## 闭环示例：自动处理 GitCode/GitHub Issue

1. **同步脚本**：建一个 cron `*/30 * * * *` 的自动化脚本（想让 Issue 一变化就同步，再开启 webhook 并按上文过滤事件），用 `gitcode`/`gh` CLI 拉取仓库 Issue 列表，整理成 cards 后调用 `board_source.save`。Issue 编号作卡片 id，Issue 标签与指派人写成 `labels`（如 `bug`、`owner: xxx`），链接写 `url`。只给已关闭的 Issue 带 `status: 已完成`，打开的 Issue 不带 `status`，这样 Agent 回写的处理进度会一直保留。

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   # gh 失败时 set -e 让脚本非零退出，不会拿半份列表去同步
   LIMIT=1000
   gh issue list --repo acme/webshop --state all --limit "$LIMIT" \
     --json number,title,body,state,labels,url > issues.json
   # 条数到了上限说明可能没拉全，同样失败退出
   [ "$(jq length issues.json)" -lt "$LIMIT" ] || { echo "Issue 超过 $LIMIT 条，请调大 LIMIT" >&2; exit 1; }

   jq '[.[] | {
         id: ("#" + (.number | tostring)),
         title: ("#" + (.number | tostring) + " " + .title),
         summary: (.body // "" | .[0:300]),
         labels: [.labels[].name],
         url: .url
       } + (if .state == "CLOSED" then {status: "已完成"} else {} end)]' issues.json > cards.json

   jq -c '{id: "github", name: "GitHub Issues",
     columns: [{value: "待处理"}, {value: "处理中"}, {value: "待验证"}, {value: "已完成"}],
     cards: .}' cards.json \
     | "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call board_source.save --arguments-file -
   ```

2. **看板**：用 `dashboard.save` 建 taskboard 绑定该源，按状态或负责人分列查看。
3. **规则**：人在看板列头 ⚡ 配置，如表达式 `bug & status: 待处理`，处理要求「@dev 请分析并修复，完成后追加状态简报」。建议先手动运行一次脚本把存量 Issue 同步进来，再开启规则，避免存量一次性全部派发。
4. **自动流转**：新 Issue 被同步成 Task → 命中规则 → 自动派发给 dev → dev 在频道内分析修复（也可以用 `gh`/`gitcode` CLI 把结论回写到外部系统）→ 用 `task.brief`/`task.update` 更新任务状态；同步脚本不带 `status` 时，这个状态在后续同步中保留，直到 Issue 关闭被同步为 `已完成`。这种写法下，重新打开的 Issue 不会自动退回 `待处理`，需要时由人或 Agent 改状态，或让脚本对重新打开的 Issue 带上状态。

PR 审查、告警值守同理：任何能写成「外部列表 → cards」的数据都能进入这条流水线，处理要求和角色选择决定它被怎样消化。

## 相关文档

- 动作清单、参数与权限模型：[agent-tool-api.md](agent-tool-api.md)
- Agent 工作区与任务快照边界：[agent-harness-workspace.md](agent-harness-workspace.md)
