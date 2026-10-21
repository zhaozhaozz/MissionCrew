# MissionCrew Agent Tool API

MissionCrew routes every change that chat roles make to platform state through a single Runtime-agnostic tool boundary. Agents no longer ask the platform to guess and execute actions through special text blocks in their final reply; instead, during the executing turn they call the HTTP API or the CLI installed with the service, immediately get a success result or a structured error, and can continue working based on that error.

This boundary applies to every chat Runtime. Claude, Codex, ACP and print-mode CLIs all call MissionCrew through the same set of environment variables, role tokens and action contracts, and Runtime providers do not need to import Store, FastAPI or the internal implementation of any resource.

For humans, this means channels, tasks, documents, guidelines, skills, dashboards and automation scripts can all be created, inspected and maintained automatically by the orchestrator during a conversation (including changing and archiving channels, archiving tasks, reading the current configuration of dashboards, data sources and scripts, and test-running scripts): a human states a request in chat, and the orchestrator completes it by calling the actions below and posts the resource links back to the channel; regular roles can also create and update tasks and publish documents. The Web pages and the CLI are only entry points for viewing and manual intervention, not the only way to enter these resources.

## Call chain

```text
Runtime
  └─ missioncrew-tool / python -m missioncrew.agent_tool
       └─ Authorization: Bearer <channel×role token>
            └─ POST /api/agent/v1/actions
                 └─ Validation of identity, scope, token-bound run, arguments and resource boundaries
                      └─ Unified action registry
                           ├─ Writes to platform state or document versions
                           ├─ Resource URL results
                           └─ Audit records without bodies or tokens
```

The chat context injects:

| Environment variable | Meaning |
| --- | --- |
| `MISSIONCREW_AGENT_TOOL_URL` | Root URL of the current service's Agent Tool API; the default is `http://127.0.0.1:8321/api/agent/v1` |
| `MISSIONCREW_AGENT_TOKEN_FILE` | Per-run capability file at a stable path; the CLI reads it itself, and the agent should not print or publish its contents |
| `MISSIONCREW_AGENT_TOOL_PYTHON` | A Python interpreter that can import the current MissionCrew package |

First, list the actions the current role is allowed to perform:

```bash
"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool actions
```

Write calls do not pass `run_id`; the server determines the current run from the Bearer token:

```bash
"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call task.create \
  --arguments '{"title":"补齐接口测试","summary":"覆盖错误返回","channel_ids":["general"],"labels":["api"]}'

"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool publish-file \
  --source reports/result.md \
  --path reports/result.md

"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call document.rename \
  --arguments '{"source":"reports/result.md","target":"archive/result.md"}'
```

The CLI has four subcommands: `actions`; `call <action> --arguments '<JSON>'` (for large arguments, use `--arguments-file <file>` instead, and `--arguments-file -` reads from stdin, to avoid exceeding the length limit of a single command-line argument); `publish-file --source <local-file> --path <document-library-path> [--message <version-note>] [--overwrite]`; and `publish-message --channel <channel-short-id> --content <body> [--mention <role-id>]...`. The last two are convenience wrappers for `document.publish` and `message.publish` respectively. After the project is installed, the equivalent `missioncrew-tool` command can also be used. The CLI exits with code 0 on success; on API errors, connection errors or client argument errors it exits with a non-zero code and prints the JSON verbatim to stdout, so the Runtime can read it, correct the call and retry within the same turn.

## Identity, permissions and lifecycle

Tokens are issued per `project × channel × role × run`, not per Runtime, so a role keeps the same permissions after switching to Claude, Codex or ACP. The plaintext is written only to `.agent-tool-token` at a stable path in that role's isolated workspace, with file mode `0600`; each run atomically replaces the file contents after it acquires the execution lock. SQLite does not store the plaintext; it stores only the SHA-256 hash, a random `token_id`, the project/channel/role, `kind` (`run` or `automation`), the bound `run_id`, the scopes, and the issued/expiry/last-used/revoked times.

A token is valid for at most seven days, but normally it is revoked and its file deleted as soon as the corresponding run ends; the next run writes a new capability to the same path. The server derives `run_id` directly from the token and verifies that the turn belongs to the channel and role in the token and that its status is still `queued`, `running` or `waiting_user`. A `run_id` submitted by older clients is only a compatibility check field and cannot change the run the token is bound to. Therefore, obtaining an old token is not enough to keep writing in a turn that has already ended.

The current permission policy is:

| Action | Regular role | Project orchestrator |
| --- | --- | --- |
| `task.create` / `task.update` / `task.brief` | Allowed | Allowed |
| `task.delete` / `task.archive` / `task.restore` | Denied | Allowed |
| `document.publish` | Allowed | Allowed |
| `document.rename` | Allowed | Allowed |
| `document.history` | Allowed | Allowed |
| `document.delete` | Denied | Allowed |
| `message.publish` | Denied | Allowed |
| `channel.runs.list` / `channel.run.stop` / `channel.history` | Denied | Allowed |
| `channel.create` / `channel.update` / `channel.archive` / `channel.restore` | Denied | Allowed |
| `dashboard.list` / `dashboard.save` / `dashboard.delete` | Denied | Allowed |
| `board_source.list` / `board_source.save` / `board_source.delete` | Denied | Allowed |
| `guideline.list` / `guideline.history` / `guideline.save` / `guideline.delete` | Denied | Allowed |
| `skill.list` / `skill.history` / `skill.save` / `skill.delete` | Denied | Allowed |
| `automation.list` / `automation.save` / `automation.run` / `automation.delete` | Denied | Allowed |
| `recycle.list` / `recycle.restore` / `recycle.purge` | Denied | Allowed |

When the project has no orchestrator selected (no-orchestrator mode), there is no "Regular role" column: every role is authorized according to the "Project orchestrator" column, but roles cannot dispatch to each other. `message.publish` can only post messages and returns `permission_denied` when given a non-empty `mentions` (the description of this action listed by `actions` also omits the `mentions` parameter); roles are started only when a human names them, and results stay only in the channel. Dispatch permission is determined by `Project.can_dispatch`, which is true only for the orchestrator.

`guideline.save` shares the guideline Git repository with the Web guideline editor. `skill.save` likewise commits the current complete skill directory to the project skill repository, so existing auxiliary files such as `scripts/`, `references/` and `assets/` form a version together with the updated `SKILL.md`; both actions return `revision` on success and record the current role. Renaming a guideline continues the original file's history chain. Deletion requires orchestrator permission: documents, guidelines, skills and dashboards all go into the project's unified recycle bin, and deleting a document, guideline or skill also creates a new Git commit, so history is not erased. `recycle.restore` returns `already_exists` and keeps the recycled item when the original identifier is already taken; `recycle.purge` is an irreversible permanent deletion and should be called only when the user explicitly asks for it. Tool responses do not expose the local path of the recycle directory.

The `mentions` of `message.publish` is a separate array of role IDs, and it is also the **only** dispatch channel: only valid roles in the array are scheduled, and roles with "manual mention only" (仅人工点名) enabled are treated as nonexistent for agents (the call returns `role_not_found`; the automation script identity is not subject to this restriction); text such as `@reviewer` or `@[reviewer]` in the body is always just ordinary content. The orchestrator's final Runtime reply is published to the current channel automatically by the platform; ordinary answers, conclusions and status summaries should not be published a second time through `message.publish` with empty `mentions`. Assigning work still uses `message.publish` with the target `mentions` passed explicitly. Regular roles neither have the scope for this action nor can see the roster of the other executor roles. When at least one role actually starts, the result also returns `handoff: "end_turn"` and follow-up resume instructions; the orchestrator should immediately state briefly in its final Runtime reply that the work has been dispatched and end the current turn, without calling `message.publish` again for that statement. It should also not wait by using `sleep` or by polling the channel, the worktree or the run status, and it should not dispatch another message such as "report intermediate status" to the same role while that role is executing. A persistent session of the same role in the same channel cannot have a second turn inserted while it is executing; such requests are only queued behind the original task and cannot provide real-time progress. After the role completes or fails, the platform automatically starts a new orchestrator turn and hands back the complete result. When the collaboration chain budget results in no role being started, `dispatched` is empty and this handoff is not returned.

The status of active runs in the current channel is not injected directly into the orchestrator's context. When a human asks what is currently running or asks to stop a role, the orchestrator can call the read-only action `channel.runs.list` on demand to get a one-time snapshot of `queued`, `running` and `waiting_user` runs; each item contains `run_id`, the role, the execution combination, whether it is the current orchestrator run, and whether it can be stopped. It can then use `channel.run.stop` with the chosen `run_id` to stop the target within the same channel. The server rejects cross-channel requests, requests for runs that have already ended, and requests by the current orchestrator to stop itself; stopping a queued run only cancels the queue entry and does not terminate the same role's other Runtimes. Queries do not produce a conversation receipt, and the stop action itself generates an `agent_stop` platform message, so it does not generate a duplicate tool receipt either. The orchestrator should not use on-demand queries for wait loops.

Both `channel.runs.list` and the read-only action `channel.history` accept an optional `channel` (a channel short id within the project; defaults to the current channel) for cross-channel checks: when the orchestrator has dispatched a task from #general to a dedicated channel, then been stopped, and is later woken up again, the message sent by `message.publish` exists only in the target channel and the source channel has only a dispatch receipt. In that case, using `channel.runs.list` to see whether the target channel has active runs and `channel.history` to read the target channel's recent messages is enough to determine how far execution has progressed and to avoid starting the work twice. `channel.history` returns the latest `limit` records (default 20, at most 200), pages backward with `before_id`, uses the same record format as the workspace `channel-history.json` (including `agent_tool` receipts and platform messages such as `agent_stop`), and also includes the channel's total message count `message_count`. Runs found by a cross-channel query always have `stoppable` set to false, and `channel.run.stop` still accepts only a `run_id` from the current channel.

After a write operation succeeds, the platform appends a platform receipt of type `agent_tool` to the conversation of the channel that made the call, showing the calling role, the action name and an action summary. Failed calls and read-only calls such as `channel.list`, `recycle.list` and the other `*.list` actions do not generate receipts. When `message.publish` posts to the current channel, the message itself is the trace, so no receipt is inserted; when it posts to another channel, a receipt is inserted in the calling channel containing a link to the target channel and the list of dispatched roles (the same as the `summary` returned by the tool), and `context.agent_tool` additionally records the target `channel` and `message_id`, so that an orchestrator that was stopped and then woken up again can still tell from its own channel's history that the work has been dispatched.

### Automation script identity (`kind=automation`)

Besides the per-run role tokens, there is another kind, the **automation script token**: when a project script defined by `automation.save` is triggered by cron, a webhook delivery or manually, the platform issues a one-time token for that run (`kind=automation`, not bound to a run), writes it to `.agent-tool-token` in the script's working directory, and revokes it immediately after the run ends. The script's own `actions` allowlist is the source of truth for the actions available to the script identity (default `task.create`, `task.update`, `task.brief`, `message.publish`, `document.publish`, `dashboard.save`, `board_source.save`), independent of the orchestrator permission bit; the allowlist may contain only actions other than `channel.runs.list`, `channel.run.stop`, `channel.history` and `automation.run` (so scripts cannot trigger each other), and passing these actions to `automation.save` returns `invalid_arguments`. The audit actor is recorded as `automation:<project-id>:<script-short-id>`, which distinguishes it from role-triggered calls.

`message.publish` from the script identity is published with `author_type=automation`: without `mentions` it only posts the message and does not trigger any role; explicitly mentioning a single role dispatches that role directly (the result is not handed back to the orchestrator automatically); mentioning multiple roles converges, as with human messages, to starting only the orchestrator. When any identity calls `task.create` and one of the project's configured automatic task-handling rules matches the labels, the task is dispatched immediately according to the rule's handling instructions and handling role (except tasks whose status is already `已完成` (done)), and the result includes `auto_dispatch` (`rule_query`, `sent`); scripts syncing external tasks are the most common case. Rules can only be configured by a human through the ⚡ in a task board column header, and there is no corresponding Agent Tool action; see [automation.md](automation.md) for details.

## Actions and concurrency rules

`GET /api/agent/v1/actions` returns the token identity and the action descriptions. The request format of `POST /api/agent/v1/actions` is:

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

- A document must provide exactly one of UTF-8 `content` or `content_base64`; the per-file limit is 50 MB. Existing files are not overwritten by default; only passing `overwrite: true` explicitly overwrites the file and creates a new version.
- `document.rename` takes `source` and `target` relative paths within the document library; the source file must exist and the target path must not exist. Moving a file and only changing its name use the same action, and a single Git commit preserves the original file's history chain. If the source document already has a config-page chat bound to it, that channel, its messages and the Runtime session move to the new path; if the target path is already bound to another config-page chat, `channel_conflict` is returned.
- `task.delete`, `document.delete`, `dashboard.delete`, `guideline.delete` and `skill.delete` all require the project orchestrator identity. When the target does not exist they return `task_not_found` or `not_found`, rather than falsely reporting the deletion of a nonexistent resource as a success; a successful result contains `recycle_item`.
- `recycle.list` returns recycled items of all types in the current project; `recycle.restore` and `recycle.purge` take a recycled item `id`, and both require the project orchestrator identity.
- `channel.runs.list` and `channel.history` query the current channel bound to the token by default, and passing `channel` queries another channel in the project; `channel.run.stop` accepts only a positive integer `run_id` returned by a query of the current channel, and cannot stop runs in other channels or the current orchestrator's own run.
- On success, `task.create`, `task.update` and `task.brief` all include `task` in the result, which is the complete task after the write (including the new `updated_at`). `updated_at` is MissionCrew's system update time: the platform refreshes it on every write, it cannot be set, and it serves as the version described below; they also immediately refresh the `tasks/` snapshot in the calling role's workspace, so there is no need to wait for the next turn.
- `task.update` must include `snapshot_updated_at`, set to the task's current version: the `snapshot_updated_at` field of its `tasks/` snapshot, or `updated_at` of `task` in the result of the previous `task.*` call. If the task has already been updated by another execution, `version_conflict` is returned to prevent a stale snapshot from overwriting newer state; by then the platform has already refreshed the snapshot, and `details.task` in the error is the current task, so check what changed and retry with its `updated_at`. `task.brief` does not need a version number, and it is simpler to use when you only record progress or only change the status.
- `task.create` and `task.update` use `title`, `summary`, `body`, `status`, `labels`, `channel_ids`, `url`, `meta`, `external_id` and `external_updated_at`; `status` is the status text (stored as a `status: <text>` label), and labels support the advanced `property: value` form. Channel binding is optional: `task.create` without `channel_ids` binds the current channel, and `task.update` with `[]` removes all bindings; at least one channel is required only when the task is dispatched, and the project's `general` channel is used when there is no binding. The `labels` of `task.update` replace the entire label list; to change only one property, use `properties` (e.g. `{"owner": "张三", "priority": null}`), where values can be strings or arrays, `null` clears that property, and the other labels are left untouched; when `status` is also given, `status` takes precedence.
- `source` of `task.create` sets the data source the task belongs to: the built-in source `built-in` by default, or the short id of one of the project's custom data sources (`invalid_arguments` when it does not exist). The task then shows on taskboard dashboards bound to that source; without a status it gets the source's first status value (`待处理` for the built-in source). Tasks of the built-in source and of custom sources are operated on in exactly the same way: `task.update`, `task.brief`, `task.archive`, `task.restore` and `task.delete` each act on a single task by id, without carrying the other tasks of the data source.
- `url` (link to the external item), `meta` (display notes on the board card, an array of strings) and `external_id` (the external item's id, such as an issue number) can be written for tasks of both kinds of data source, with the same rules as the card fields of `board_source.save`. `external_updated_at` is the item's update time in the external system (such as an issue's last update), given as Unix seconds or an ISO 8601 string with a time zone (such as `2026-10-19T08:00:00Z`); values without a time zone or that look like milliseconds are rejected. Boards show and sort by it first, and `task.update` with `null` clears it. `external_id` is unique within a data source: when it is already taken, `already_exists` (409) is returned with the existing task in `error.details.task`, which should be updated with `task.update` instead; `task.update` with an empty string removes it. The whole sync of a custom source matches by `external_id`: when `task.create` records an external item, pass it (the id the cards will use), so the next card with that id in `cards` updates this task instead of creating another one, and a later `cards` list without it deletes the task; tasks without `external_id` take no part in the whole sync. The built-in source has no whole sync, and `external_id` there only identifies the external item and prevents recording it twice.
- `task.brief` appends a status brief and can update the status text at the same time through `status` (e.g. `待处理` (to do), `处理中` (in progress), `已阻塞` (blocked), `已完成`). Briefs are appended records and do not overwrite the body.
- `task.delete` moves the task body together with all of its status briefs into the project recycle bin; after it is restored, the original task id, fields, and the briefs' authors, content and times are preserved. Restoring is refused when the custom data source the task belonged to has been deleted, or when the data source already has a task with the same `external_id` (recreated by a later sync, or recorded again).
- `board_source.save` creates or updates a custom task data source (`columns` declares the status values, and `cards` are synced by id as a whole into this source's tasks: adding new ones, updating existing ones, and deleting those not present this time), which a taskboard dashboard from `dashboard.save` binds through `source`; new tasks go through the project's automatic handling rules. Existing tasks only have the fields and properties carried by the card updated: without `status` the local status is kept, the plain labels in `labels` are replaced as a whole group, and `property: value` labels and `properties` replace only properties with the same name (`null` clears); see [automation.md](automation.md) for the merge rules. The whole sync only manages tasks with an `external_id` (created from a card `id`, or recorded by `task.create` with `external_id`); tasks without one take no part in matching and are never deleted for being absent from `cards`. Never save a partial `cards` list to add or remove a single task. `board_source.delete` returns `in_use` while the data source is still referenced by a dashboard, and deleting a data source also deletes all of its tasks (including those added with `task.create`) without the recycle bin.
- `automation.save` creates a new script by short id or updates an existing one by merging fields: `script` is the full script text (run as an executable if it has a shebang, otherwise with bash), `cron` is a five-field crontab expression (empty string = no schedule), `enabled` controls scheduled triggering only, `webhook: true` accepts deliveries on the dedicated webhook port (can be combined with cron; the result carries `webhook_path`, while the signing secret is shown only on the human automation page and is never given to agents, so ask a human to take it from there and configure the external system; see [automation.md](automation.md)), `actions` is the action allowlist of the script token, and `timeout_seconds` is the timeout of a single run. `automation.delete` deletes the script, revokes its tokens and clears its run records. `automation.run` triggers one manual run in the background right away (also for a script whose schedule is disabled) and returns `run_id`; while the previous run of the same script is still going it returns `in_use`.
- Configuration that is not in the agent workspace is read on demand with the read-only `*.list` actions; read it before changing anything: `dashboard.list` (with `id` it returns the full layout/filters/group_by), `board_source.list` (status values, task counts and bound dashboards), `automation.list` (with `id` it returns the full script and the latest `runs` runs, 5 by default and at most 20, where each run's stdout/stderr is cut to its last 4000 characters and marked when truncated; it also includes `run_count`, the number of run records kept for the script, and `before_id` returns only runs with a smaller id, so paging back reaches every kept record), and `guideline.list` / `skill.list` (including disabled entries; with `name` / `id` they return the full Markdown, plus the package's file list for a skill, which can be passed back unchanged to `guideline.save` / `skill.save` to edit or re-enable it).
- The Git history of documents, guidelines and skills lives in separate version repositories outside the agent workspace and is read with the read-only `*.history` actions: `document.history` (`path`), `guideline.history` (`name`) and `skill.history` (`id`). Without `revision` they list the versions newest first (`revision`, `created_at`, `actor`, `message`; 20 by default, `limit` at most 100; document history follows renames and also works for deleted documents); with `revision` they return that version's content: `content` for a document (`content_base64` for non-UTF-8 files), the full `markdown` for a guideline, and `SKILL.md` plus the file list for a skill, where `file` additionally reads one file of the package. A single version larger than 5 MB returns `file_too_large`. There is no separate restore action: rolling back means reading the old version and saving it again with the matching `*.publish` / `*.save`, which creates a new version and keeps the history intact.
- `channel.update` changes a channel's name, purpose or working directory (`id` defaults to the current channel); the working directory follows the same rule as `channel.create`, an empty string switches back to the platform's built-in workspace, and a new directory takes effect from that channel's next run. `channel.archive` makes a channel read-only and stops its persistent runtimes; it is refused for `general`, page-conversation channels and channels with active runs, including the current channel (`general_channel`, `content_channel`, `in_use`); `channel.restore` reverses it. Archived and page-conversation channels cannot be changed (`channel_archived`, `content_channel`). The "编辑频道" (Edit channel) item in the web sidebar's channel menu and `PATCH /api/chat/channels/{id}` share the same rules.
- `task.archive` takes a task off the boards and out of the `tasks/` snapshot while keeping its body and briefs; `task.restore` restores an archived task.
- The IDs, project ownership, working directories and Markdown properties of channels, dashboards, guidelines and skills are all validated in the unified action implementation.
- For actions involving channels, documents, tasks, guidelines, skills, automations and the recycle bin, the success result contains a canonical `/resources/...` URL (`resource_url`); the agent should put that URL in its channel reply and should not publish the real `.missioncrew` path.

Success response:

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

A failure response always contains a machine-readable `code`, a `message` for the Runtime to read, and `retryable`, which indicates whether a retry is appropriate; when it can help the caller recover on the spot, it also includes `details` (such as the current task for `version_conflict`):

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

Common errors include `missing_token`, `invalid_token`, `expired_token`, `run_mismatch`, `run_inactive`, `permission_denied`, `invalid_request`, `invalid_arguments`, `already_exists`, `version_conflict` and `internal_error`. Resource errors also include `not_found`, `task_not_found`, `task_archived`, `channel_not_found`, `channel_archived`, `content_channel`, `general_channel`, `unavailable`, `role_not_found`, `run_not_found`, `cannot_stop_self`, `file_too_large`, `channel_conflict`, `in_use` and `unsupported_action`; the CLI generates `connection_failed` / `client_error` itself when it cannot connect or the local arguments are invalid.

## Audit and security boundary

Every action call writes an `agent_tool_called` audit entry recording the project, channel, role, `token_id`, `run_id`, `request_id`, action, result status and error code. Audit details do not store the Bearer token, message bodies, document bodies or full arguments; the creation, modification and publishing of resources still write their own domain audit entries.

Role tokens provide a MissionCrew application-level boundary for identity, least privilege and traceability; they are not an operating-system sandbox against malicious local processes. The service's regular Web API currently has no authentication for public-network access, and YOLO Runtimes may also have host network and file permissions; therefore the service should still run only on a trusted network, and untrusted execution should be restricted through Runtime filesystem/network policies, a host firewall or a reverse proxy. Bearer scopes must not be described as strong isolation that no process on the same host can bypass.

## Compatibility migration

Legacy `missioncrew-action` text blocks can still be read, so that old persistent Runtime sessions and historical tests do not break immediately; after parsing they are only forwarded to the same action registry and no longer have their own write logic. Among them, `post_message` has the same contract as `message.publish`: it dispatches only when the block explicitly carries a `mentions` array, and `@[role-id]` in the body no longer triggers any execution. New contexts do not ask the Runtime to produce this format, and the old entry point cannot return errors to the same agent turn the way tool calls do, so it exists only for migration compatibility, and no new dependencies on it should be added.

Post-run sync after a chat role edits `.missioncrew/documents/` directly is still a compatibility path; new implementations should call `document.publish`. `.missioncrew/tasks/` is only a read-only snapshot, and a task changes only when an agent explicitly calls `task.create`, `task.update`, `task.brief` or `task.delete`. Task handling always goes through channel collaboration; there is no separate task-stage Runtime.

The document library has no separate directory resources or directory actions. The directory tree is formed implicitly by each file's relative path: after `a/b/c.md` is published, the Web UI shows `c.md` under the directory `a/b`. Creating the target's parent directories is done automatically by `document.publish` / `document.rename`; empty directories do not enter the document list or Git history, and there are no separate interfaces to create, move, rename or delete them.
