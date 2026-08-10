# MissionCrew Agent Tool API

MissionCrew 把聊天角色对平台状态的修改收敛到一个 Runtime 无关的工具边界。Agent 不再通过最终回复中的特殊文本块请求平台猜测并执行动作，而是在执行回合内调用 HTTP API 或随服务安装的 CLI，立即获得成功结果或结构化错误，并可以根据错误继续处理。

这套边界适用于所有聊天 Runtime。Claude、Codex、ACP 和打印模式 CLI 都通过同一组环境变量、角色令牌和动作契约调用 MissionCrew，Runtime provider 不需要导入 Store、FastAPI 或各资源的内部实现。

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
  --arguments '{"title":"补齐接口测试","summary":"覆盖错误返回","channel_ids":["demo:general"],"labels":["api"]}'

"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool publish-file \
  --source reports/result.md \
  --path reports/result.md

"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call document.rename \
  --arguments '{"source":"reports/result.md","target":"archive/result.md"}'
```

安装项目后也可以使用等价的 `missioncrew-tool` 命令。CLI 在成功时退出码为 0；API 错误、连接错误或客户端参数错误时退出码非零，并把 JSON 原样打印到 stdout，便于 Runtime 在同一回合读取、修正和重试。

## 身份、权限与生命周期

令牌按 `project × channel × role × run` 分配，而不是按 Runtime 分配，所以角色更换 Claude、Codex 或 ACP 后仍遵循同一权限。明文只写入该角色隔离工作区中路径稳定的 `.agent-tool-token`，文件权限为 `0600`；每个 Run 获得执行锁后原子替换文件内容。SQLite 只保存 SHA-256 哈希、随机 `token_id`、绑定的 `run_id`、作用域、签发时间、过期时间和最后使用时间。

令牌最多有效七天，但正常情况下会在对应 Run 结束时立即撤销并删除文件；下一 Run 在同一路径写入新的 capability。服务端从令牌直接得到 `run_id`，并确认该回合属于令牌中的频道和角色且状态仍是 `queued`、`running` 或 `waiting_user`。旧客户端提交的 `run_id` 只作为兼容校验字段，不能改变令牌绑定的 Run。因此获得旧令牌不足以在已结束回合中继续写入。

当前权限策略如下：

| 动作 | 普通角色 | 项目主控 |
| --- | --- | --- |
| `task.create` / `task.update` / `task.brief` | 允许 | 允许 |
| `task.delete` | 禁止 | 允许 |
| `document.publish` | 允许 | 允许 |
| `document.rename` | 允许 | 允许 |
| `document.delete` | 禁止 | 允许 |
| `message.publish` | 禁止 | 允许 |
| `channel.create` | 禁止 | 允许 |
| `dashboard.save` / `dashboard.delete` | 禁止 | 允许 |
| `guideline.save` / `guideline.delete` | 禁止 | 允许 |
| `skill.save` / `skill.delete` | 禁止 | 允许 |
| `recycle.list` / `recycle.restore` / `recycle.purge` | 禁止 | 允许 |

`guideline.save` 与 Web 准则编辑器共用 Git 版本库。每次 Markdown 内容变更都返回 `revision`，记录为当前角色的操作；重命名会继续原文件的历史链。删除属于主控权限：文档、准则、Skill 和面板都会进入项目统一回收站，文档与准则删除同时形成新 Git 提交，历史不会被抹除。`recycle.restore` 在原标识已被占用时返回 `already_exists` 并保留回收项；`recycle.purge` 是不可撤销的永久删除，只应在用户明确要求时调用。工具响应不会暴露回收目录的本地路径。

`message.publish` 的 `mentions` 是独立的角色 ID 数组，也是**唯一**的派发通道：只有数组中的合法角色会被调度；正文里出现的 `@reviewer`、`@[reviewer]` 等文本一律只是普通内容。主控的 Runtime 最终回复会由平台自动发布到当前 Channel；普通答复、结论和状态汇总不应再通过空 `mentions` 的 `message.publish` 重复发布。派工仍使用 `message.publish` 并显式传入目标 `mentions`。普通角色既没有该动作的 scope，也看不到其他执行角色的名册。至少一个角色实际启动时，结果还会返回 `handoff: "end_turn"` 和后续恢复说明；主控应立即在 Runtime 最终回复中简短说明已派发并结束当前 turn，不再为这条说明调用一次 `message.publish`；同时不用 `sleep` 或轮询频道、工作树、运行状态来等待，也不应向执行中的同一角色再次派发“报告中间状态”之类的消息。同一频道同一角色的持久会话不能在执行中插入第二个 turn，这类请求只会排在原任务后面，不能提供实时进度。角色完成或失败后，平台会自动启动新的主控 turn 并交回完整结果。协作链预算导致无人启动时，`dispatched` 为空且不会返回该 handoff。

成功执行写操作后，平台会在发起调用的 Channel 会话中追加一条 `agent_tool` 类型的平台回执，显示调用角色、动作名和动作摘要。失败调用和 `recycle.list` 等只读调用不生成回执；`message.publish` 已经直接产生可见消息，因此不会再重复插入一条工具回执。

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
- `document.rename` 使用文档库内的 `source` 和 `target` 相对路径；源文件必须存在、目标路径必须不存在。移动和仅修改文件名使用同一动作，并以一次 Git 提交保留原文件的历史链。若源文档已有页面对话绑定，该频道、消息和 Runtime 会话会迁移到新路径。
- `task.delete`、`document.delete`、`dashboard.delete`、`guideline.delete` 和 `skill.delete` 都要求项目主控身份。目标不存在时返回 `task_not_found` 或 `not_found`，不会把删除不存在的资源误报为成功；成功结果包含 `recycle_item`。
- `recycle.list` 返回当前项目全部类型的回收项；`recycle.restore` 和 `recycle.purge` 使用回收项 `id`，均要求项目主控身份。
- `task.update` 必须带读取任务时得到的 `snapshot_updated_at`。任务已经被其他执行更新时返回 `version_conflict`，防止旧快照覆盖新状态。
- `task.create` 和 `task.update` 使用 `title`、`summary`、`body`、`status`、`labels`、`channel_ids`；每个 Task 至少绑定一个当前项目的可用 Channel。
- `task.brief` 追加状态简报，可用 `status` 同时更新 `open`、`in_progress`、`blocked`、`done` 状态。简报是追加记录，不覆盖正文。
- `task.delete` 把 Task 正文和全部状态简报一起移入项目回收站；恢复后保留原 Task id、字段、简报作者、内容和时间。
- 频道、面板、准则和 Skill 的 ID、项目归属、工作目录和 Markdown 属性都在统一动作实现中校验。
- 成功结果包含规范 `/resources/...` URL；Agent 应把该 URL 放入频道回复，不应发布 `.missioncrew` 的真实路径。

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

失败响应始终包含机器可判断的 `code`、给 Runtime 阅读的 `message` 和是否适合重试的 `retryable`：

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

常见错误包括 `missing_token`、`invalid_token`、`expired_token`、`run_mismatch`、`run_inactive`、`permission_denied`、`invalid_request`、`invalid_arguments`、`already_exists`、`version_conflict` 和 `internal_error`。

## 审计与安全边界

每次动作调用都会写入 `agent_tool_called` 审计项，记录项目、频道、角色、`token_id`、`run_id`、`request_id`、动作、结果状态和错误码。审计详情不保存 Bearer token、消息正文、文档正文或完整参数；资源自己的创建、修改和发布仍写各自的领域审计项。

角色令牌提供的是 MissionCrew 应用层身份、最小权限和追溯边界，不是针对恶意本地进程的操作系统沙箱。当前服务的普通 Web API 没有公网身份验证，YOLO Runtime 也可能拥有主机网络和文件权限；因此服务仍应只运行在可信网络，并通过 Runtime 文件系统/网络策略、主机防火墙或反向代理限制不可信执行。不能把 Bearer scope 描述成对同一主机上任意进程都不可绕过的强隔离。

## 兼容迁移

历史 `missioncrew-action` 文本块仍可读取，避免旧的持久 Runtime 会话或历史测试立即失效；解析后只转发到同一个动作注册表，不再拥有独立写入逻辑。其中 `post_message` 与 `message.publish` 同契约：只有块内显式携带 `mentions` 数组才会派发，正文里的 `@[角色ID]` 不再触发任何执行。新上下文不会要求 Runtime 生成该格式，旧入口也无法像工具调用一样把错误返回给同一 Agent 回合，因此只作为迁移兼容，不应新增依赖。

聊天角色直接编辑 `.missioncrew/documents/` 的执行后同步仍属于兼容路径；新实现应调用 `document.publish`。`.missioncrew/tasks/` 只是只读快照，Task 只有在 Agent 显式调用 `task.create`、`task.update`、`task.brief` 或 `task.delete` 时才会改变。Task 处理统一进入 Channel 协作，不存在独立的任务阶段 Runtime。

文档库没有独立的目录资源或目录动作。目录树由各文件的相对路径隐式形成：发布 `a/b/c.md` 后，Web 会展示目录 `a/b` 下的 `c.md`。创建目标父目录由 `document.publish` / `document.rename` 自动完成；空目录不会进入文档清单或 Git 历史，也没有单独的创建、移动、重命名或删除接口。
