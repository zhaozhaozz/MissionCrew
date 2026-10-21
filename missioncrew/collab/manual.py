"""写进 Agent 工作区的 MissionCrew 完整手册(渐进式披露)。

Prompt 只保留必须时刻遵守的规则;平台各动作的用法、格式约定、边界细节都放在
这份手册里,由 Agent 在需要时读取。每次装配都会重新渲染,保证与当前项目一致。
手册与 Prompt 一样用英文书写;示例里的 `待处理` 等状态是项目真实的标签取值,保持原样。
"""
from __future__ import annotations

from ..core.models import Project
from .documents import document_resource_url
from .resource_urls import missioncrew_project_url

_MANUAL = """\
# MissionCrew Manual

The platform refreshes this file every turn; it is the reference for how to work with
MissionCrew. The prompt keeps only the rules that always apply; read the matching
section before you do any of the following: publish documents, work with Tasks, dispatch
messages, manage channels, dashboards and data sources, guidelines and skills, automation,
the recycle bin, or config-page collaboration. For the full details of a platform mechanism,
search `platform-docs/`: MissionCrew's own documentation, indexed by
`platform-docs/README.md`.

## 0. Typical Flow

An executor's turn:
1. Read the task: the task comes only from the brief in the trigger message; if information is missing, raise it in your reply instead of widening the scope.
2. Gather context: read channel history, guidelines, skills and project documents only when needed; do not preload.
3. Do the work: read/write code and run commands in the working directory; touch only authorized directories; put temporary files in the conventional directory or the workspace `temp/`.
4. Write back to the platform: publish documents, create or update Tasks and append status briefs only through the Agent Tool.
5. Reply: lead with the conclusion, then what you did; reference resources with `/resources/...` links; results of tasks the orchestrator dispatched go back to it automatically.

An orchestrator's turn:
1. Read the request: a human request or an executor's report is this turn's input; when several roles are mentioned at once only you are started, and the list is in the trigger message.
2. Choose who and where: pick roles from the role roster; whether to open a new channel follows the project guidelines; run `channel.list` before referencing or dispatching to another channel.
3. Dispatch: `message.publish` with `channel`, `mentions` and a brief stating the background, requirements and acceptance criteria.
4. Wrap up and wait for reports: when `dispatched` is non-empty, say in one sentence that the work was dispatched and end the turn; do not poll, chase or take over; the platform wakes you up automatically.
5. Review and summarize: check against the acceptance criteria, then dispatch further or write the conclusion; record milestone conclusions with `task.brief`; run verification commands in the repository directories.

## 1. Platform and Workspace

- MissionCrew is a local multi-agent harness that assembles roles, runtimes/models, project
  context, shared materials and channel collaboration; a Task is an issue that enters a channel,
  not a separate execution flow; MissionCrew is not the business repository.
- This `.missioncrew` workspace lives outside the business repositories (its path is in the
  prompt and `MISSIONCREW_WORKSPACE`); its files never enter business source code or commits.
  Source code is still changed in the working directory or project repositories given in the prompt.
- Layout: `documents/` entry to the project's versioned document library (collaboration drafts,
  reports and verification records belong here too); `tasks/` read-only Task snapshots;
  `guidelines/` read-only view of enabled guidelines; `skills/` read-only view of enabled skills
  (read SKILL.md first, then use scripts/, references/, assets/ in the same directory as needed);
  the platform regenerates both views every turn; `project.md` project summary and repositories; `channel-history.json` full channel history
  (executors get a redacted view); `temp/` temporary directory when the project has no convention;
  `manual.md` this manual; `platform-docs/` read-only copy of MissionCrew's own documentation
  (`README.md` there lists every document).
- Do not read or print `.agent-tool-token`; the Agent Tool CLI reads the token file itself.

## 2. Resource URLs and External References

- Project resource prefix `__PROJECT_URL__`: channel `/channels/<channel id>`, task `/tasks/<task id>`,
  dashboard `/dashboards/<dashboard id>` (the built-in task board id is `tasks`), guideline
  `/guidelines/<name>`, skill `/skills/<id>`, document `/documents/<path in the document library>`,
  recycle bin `/recycle-bin`.
- These URLs are web identifiers, not file paths. Replies must reference platform resources with
  these links and documents as `[title](__DOCUMENTS_URL__/path)`; never output absolute workspace
  paths, the real `.missioncrew` path or `file://` links.
- The document resource URL `__DOCUMENTS_URL__/<relative path>` maps directly to
  `$MISSIONCREW_DOCUMENTS_DIR/<relative path>`; do not guess or search for the physical location
  of `.missioncrew`.

## 3. File System Boundary and Path Failures

- Read and write only the read/write directories listed in the prompt and their subdirectories.
  Do not access `/tmp`, `/var/tmp`, other project directories, unauthorized files in the user's
  home directory, or MissionCrew source directories not explicitly listed as local repositories;
  shell redirections, background logs and files generated by tools are bound by this too.
- Put temporary files in a task directory the business repository already uses (such as the
  project's `.tmp/` or `.e2e/`); without such a convention use the workspace `temp/` and clean
  up when the task ends. Do not try an outside path first and then wait for permission approval.
- If a tool reports `external_directory`, `permission denied` or another hard path denial: do not
  switch to searching common parent directories, sibling directories or other unlisted paths, and
  do not resubmit the same out-of-bounds request; compare the target path against the list entry
  by entry, then use the exact absolute path from the list. If the task truly depends on material
  outside the list, stop accessing it and state in your result the exact denied path and the
  authorized roots you tried, so the orchestrator or a human can adjust.
- When a listed path reports `Read-only file system`, check where it is: `.git`, `.codex` and
  `.agents` under an authorized directory, and the main repository's `.git/worktrees/<name>` for a
  git worktree, are mounted read-only inside the runtime sandbox (such as Codex workspace-write);
  the host has not mounted them read-only and other files in the same directory stay writable.
  For such failures, request elevated permission with a one-sentence reason and rerun the same
  command unchanged; under auto approval the platform approves it and records a
  permission_request event, under prompt approval a human decides. Do not conclude that the
  environment is read-only and give up; only if the request is denied, stop and report as
  described above. Typical symptoms: `git add/commit/fetch` in a worktree fails because
  `index.lock`/`FETCH_HEAD` is read-only, or skill files under the repository's `.agents/skills`
  cannot be written.

## 4. Calling the Agent Tool

- List actions: `"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool actions` shows each
  action's description, arguments and whether it is orchestrator-only.
- Call: `"$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call <action> --arguments '<JSON object>'`;
  publish a local file: `... publish-file --source <local file> --path <path in the document library>`.
  Large arguments (such as a whole batch of sync cards) can exceed the command-line length limit;
  pass them with `--arguments-file <file>` or `--arguments-file -` (stdin) instead of `--arguments`.
- Commands return JSON and exit non-zero on failure; read `error.code` / `error.message`, fix the
  call and retry within the current turn. The token is bound to the current run, so do not pass
  `--run-id`.
- `permission_denied` means your role may not perform that action in this project; do not retry
  it or reach the same result another way. Say in your final reply what was not done and who can
  do it (a human, or the orchestrator if the project has one); never report it as done.
- Do not request platform operations through special text blocks in your final reply, and do not
  bypass the API by writing to the documents/, tasks/, guidelines/ or skills/ directories
  directly; any @ in a message body
  is plain text and is not a platform instruction.

## 5. Documents (document.*)

- `document.publish`: writes to the project's versioned document library and creates a Git
  version immediately; overwriting an existing path requires `overwrite: true`; binary content
  uses `content_base64` or publish-file.
- `document.rename`: pass `source` and a `target` that does not exist yet; directories are implied
  by relative document paths, and there are no separate actions to create, move or delete them.
- `document.delete` (orchestrator only): moves the document to the project recycle bin.
- `document.history`: the version history is not in `documents/`; without `revision` it lists a
  document's versions newest first (also after renames or deletion), and with `revision` it
  returns that version's content (`content_base64` for non-UTF-8 files). To roll a document
  back, read the old version and publish it again with `overwrite: true`.
- Formal project documents, collaboration drafts, reports and verification records are all
  published to `documents/` through the document library; never write them into `tasks/`.

## 6. Tasks (task.*)

- A Task has a title, summary, body, status (a `status: text` label), labels and optional channel
  bindings: `task.create` without `channel_ids` binds the current channel, `task.update` with
  `channel_ids: []` unbinds all, and dispatching a Task needs at least one channel (tasks synced
  from a data source have none until they are dispatched). The status is just a label that
  humans, agents and scripts can all change directly; the last writer wins. To set a single
  `property: value` label (such as `owner`) without rewriting the whole label list, pass
  `task.update` a `properties` object, e.g. `{"owner": "张三"}`; null clears it.
- `tasks/*.md` are all read-only snapshots generated by the platform: they are refreshed when a
  turn is assembled and right after your own `task.*` calls, and file edits are never synced
  back to Tasks. `status_briefs` in the frontmatter is read-only history. Creating, updating,
  appending status briefs to or deleting a Task must use `task.create` / `task.update` /
  `task.brief` / `task.delete` (deletion is orchestrator only).
- `task.archive` (orchestrator only) takes a finished Task off the boards and out of `tasks/`
  while keeping its body and briefs; `task.restore` brings an archived Task back (archived
  tasks are not in the snapshot, so the id comes from the human or an earlier result).
- `task.update` needs the task's current version as `snapshot_updated_at`: take the snapshot's
  `snapshot_updated_at`, or `updated_at` of the `task` in your previous `task.*` result. On `version_conflict`,
  `error.details.task` is the current task and the snapshot is already refreshed; check what
  changed, then retry with its `updated_at`. `task.brief` needs no version, so use it to record
  progress or to change only the status.
- Every Task belongs to a data source: the built-in `built-in` by default, or a custom source
  when `task.create` is given `source` (its snapshot then carries `source`). Tasks of either
  kind are changed one at a time with the same `task.*` actions; use the source's own status
  values (in the data source list of your context, or as seen on that source's tasks in
  `tasks/`). To add or remove a single task of a custom source, use `task.create` /
  `task.delete`, never a partial `board_source.save` card list: that whole sync deletes every
  task with an `external_id` missing from it.
- When a task copies an external item (such as an issue), pass `external_id` (the id the sync
  cards use, e.g. the issue number), `url` and `external_updated_at` (the item's update time in
  the external system: Unix seconds or ISO 8601 with a time zone) to `task.create`, in either
  kind of source; boards show and sort by that time. `updated_at` is MissionCrew's own system
  time, set on every write and never by you. In a
  custom source the next whole sync then updates that task instead of creating a duplicate, and
  deletes it once the item leaves the card list. `external_id` is unique within a source:
  `already_exists` returns the existing task in `error.details.task`, so update that one instead.
- Tasks with an `external_id` also carry it and `url` (the original item, such as a GitHub
  issue) in their snapshot, and their dispatch message includes the link. The
  sync script owns the fields its cards carry (usually title, summary, url and plain labels) and
  rewrites them on every sync; keep local progress in the status, `property: value` labels and
  status briefs, which a sync changes only when its cards carry them.
- Details: `platform-docs/agent-tool-api.md` (actions, results and errors) and
  `platform-docs/automation.md` (data source sync and automatic handling rules).

## 7. Messages and Dispatch (Orchestrator)

- Dispatching is the only way to schedule work: `message.publish` with `channel` (short channel
  id; the current channel's id is in the prompt), a `mentions` array of roles, and content stating
  the background, requirements and acceptance criteria; in the result, `dispatched` lists the roles
  actually started and `chain_budget` shows the runs used and the limit of this collaboration
  chain. Any @roleID or @[roleID] in a message body you send (including your final reply) is plain
  text and never triggers execution. Do not pass mentions unless you need collaboration; do not
  dispatch to yourself or invent roles.
- Each role's runtime/model is fixed when the project defines the role; you cannot and need not
  change it. Scheduling means choosing from the role roster by profile, capabilities and
  preferences.
- Once `message.publish` returns a non-empty `dispatched`, your dispatch duty for this turn is
  done: say briefly in your final reply that the work was dispatched, then end the turn. Do not
  `sleep`, do not poll channel history, worktrees or run status, do not take over an executor's
  task, and do not publish to a still-running role to ask for interim status: a role's persistent
  session in a channel cannot accept a new turn mid-run, so such requests just queue behind the
  original task. When the executor finishes or fails, the platform automatically starts a new orchestrator turn with its full result; review, dispatch further, or summarize then.
- Active run status is not written into your context automatically; when a human asks about runs
  or asks to stop a role, take a one-off snapshot of the current channel with `channel.runs.list`
  and pass the returned `run_id` to `channel.run.stop`; do not turn on-demand queries into a wait
  loop.
- Cross-channel checks: both `channel.runs.list` and `channel.history` accept `channel` (a short
  channel id in the project) to inspect another channel's active runs and recent messages (`limit`
  defaults to 20, at most 200; `before_id` pages backwards), for confirming whether a channel was
  dispatched and how far its executor got when resuming interrupted work; `channel.run.stop` can
  still only stop runs in the current channel.
- When a human picks several roles in one trigger message, the platform starts only the orchestrator; the trigger message JSON's `mentions` and
  `mention_spans` keep the full list, and the orchestrator decides on parallel or sequential order or different assignees, then dispatches each.
- Your output this turn (the final reply) is automatically published by the platform as the
  result to the trigger message's channel: put answers to humans, conclusions, current status and
  anything else you need to include into it. Use `message.publish` only when necessary, i.e. to
  dispatch or to post to another channel; do not call `message.publish` with empty `mentions` to
  post the same answer again, or the channel shows two copies of the result.

## 8. Channels (channel.*)

- `channel.list` (orchestrator only, scope=active by default / archived / all): returns id, name,
  purpose, workdir (and whether it no longer exists), archived, last_message_at,
  active_run_count and resource_url. The channel list is not preloaded into your context; check
  it before referencing, dispatching to, or creating another channel.
- `channel.history` (orchestrator only, channel defaults to the current one / limit / before_id):
  returns the latest messages of that channel in the same format as the workspace
  `channel-history.json` (including platform messages such as agent_tool receipts and
  agent_stop); `message_count` is the channel's total message count. Check it to see what happened
  in a channel recently instead of guessing.
- The workdir of `channel.create` must be a project repository path or a subdirectory of one; if
  omitted and the project has exactly one repository, that repository is used. A new channel
  starts empty: post the task brief into it with `message.publish` and name the executor
  explicitly in `mentions` to start the work.
- `channel.update` (orchestrator only, id defaults to the current channel) changes the name,
  purpose or workdir; the workdir follows the same rule as `channel.create`, an empty string
  switches back to the platform's built-in workspace, and a new workdir takes effect from the
  next run in that channel. Archived and page-conversation channels cannot be changed.
- `channel.archive` makes a finished channel read-only and stops its persistent runtimes; it is
  refused for general, page-conversation channels and channels with active runs (so you cannot
  archive the channel you are running in). `channel.restore` reverses it; find archived
  channels with `channel.list` scope=archived.
- After a channel's bound directory is deleted (for example, the task worktree was cleaned up),
  later turns fall back to the platform default directory and the prompt says so; to continue
  work in that directory, recreate it first (e.g. `git worktree add`) or create a new channel
  bound to the correct directory.

## 9. Dashboards and Board Data Sources (dashboard.* / board_source.*)

- Dashboards and data sources are not in your workspace: `dashboard.list` lists dashboards, and
  with `id` returns one dashboard's full configuration (layout, filters, group_by);
  `board_source.list` lists custom data sources with their status values, task counts and bound
  dashboards. Read the current configuration before changing it.
- `dashboard.save` keeps the existing layout when no layout field is given and replaces it
  entirely when one is. Each layout item has id, type, title, x, y, width, height and content.
  type is a generic display primitive (domain meaning comes from the data, not the type):
  markdown (content.markdown), table (columns+rows), card (metrics value cards),
  chart (kind=bar|line|pie + data + x_key/y_key), list (items), log (lines), code (code+language),
  taskboard (content.label = a label expression, content.source = data source id, default
  `built-in`; shows the matching active tasks of that source as cards).
- A card can bind live platform data through content.source, fetched automatically at render time:
  `{"from":"tasks","source":"built-in","status":["待处理"],"labels":[]}` (one data source's
  tasks → table rows; source defaults to `built-in`, like task boards),
  `{"from":"document","path":"specs/x.md"}` (document library file → markdown),
  `{"from":"audit","actions":[],"limit":30}` (audit events → list),
  `{"from":"messages","channel":"general","limit":20}` (channel messages → list).
  Examples: a requirements dashboard = a table card (static columns/rows you maintain) + a live
  task table from the tasks source; a test record dashboard = table + list; log analysis =
  list/log + a markdown conclusion.
- Task boards: `dashboard.save` with `kind: "taskboard"` and `source` (data source id: the
  built-in `built-in` or a custom source's short id). The board is split into columns by
  `filters`, each `{"title","query","color"}`, where query is a label expression (& | ! and
  parentheses; `property: *` matches tasks with a label of that property, and a bare term
  without a colon such as `owner` matches the plain label or any `owner: ...` label, so
  `!owner` means "no owner"); the status is just a label such as `status: 待处理`, so
  `status: 处理中 & bug` works. You can also pass `group_by`
  (e.g. `"owner"`) as the page's default grouping by that property's values (one column per value
  plus a trailing "unset" column); it covers every task in the board's scope, and filters do not
  narrow it. Without filters, default columns are generated from the data source's status values.
  Search, quick filters, grouping, sorting and collapsed columns on the page are each viewer's
  personal view stored in their browser and never change the saved board; users maintain the
  shared expression columns (titles, expressions, order) with the ✎ button shown next to the
  column dropdown when custom expression columns are selected. Details:
  `platform-docs/automation.md` (Task board filtering and columns).
- `board_source.save` creates or updates a custom task data source: cards are synced by id as
  that source's tasks (added / updated / removed if absent this time; removal deletes the task and
  its briefs permanently, without the recycle bin); each card needs id (and title when it creates
  a task), optionally summary, status, labels, properties, updated_at (the item's update time in
  the external system, stored as the task's `external_updated_at`), meta (an array of strings)
  and url (external link). An existing task only changes what its card carries: omit status to
  keep the status set locally (new tasks default to the first status value); plain labels in
  `labels` replace the task's plain labels as a set (an empty or property-only `labels` clears
  them; omit `labels` to leave them alone), while `property: value` labels and
  `properties` replace only those properties (null clears one). New tasks go through the
  project's automatic handling rules. Full merge rules and card formats are in
  `platform-docs/automation.md`. Typical use: a scheduled script saved with
  `automation.save` syncs GitCode/GitHub issues into the data source, then `dashboard.save` creates
  a task board bound to it. Delete with `board_source.delete` (refused while a dashboard still
  references it; deletion removes all of the source's tasks).
- `dashboard.delete` deletes a dashboard into the recycle bin.

## 10. Guidelines and Skills (guideline.* / skill.*)

- The workspace `guidelines/` and `skills/` are read-only views generated by the platform; create
  or change guidelines and skills only with `guideline.save` / `skill.save`. These views only
  hold enabled entries: `guideline.list` / `skill.list` (orchestrator only) list every entry
  including disabled ones, and with `name` / `id` return the full markdown (for a skill also its
  file list) to edit, or to re-enable by saving it again with `enabled: true`.
  `guideline.history` / `skill.history` (orchestrator only) list an entry's versions and, with
  `revision`, return that version's full markdown (for a skill also its file list, and one
  file's content with `file`); to roll back, save the old content again. If the call is
  refused, publish the draft to the document library with `document.publish`, link it in your
  final reply and state that the guideline or skill is not saved yet, so a human (or the
  orchestrator) can save it.
- `guideline.save` takes the full markdown; the file must start with YAML frontmatter containing
  only name and description. The backend reads these two properties directly, does not use
  id/title/summary, and does not convert fields. Pass original_name when modifying and renaming an
  existing guideline.
- `skill.save` creates or overwrites by id; markdown is the full SKILL.md text: the frontmatter
  contains at least name and description, and extra properties are kept as they are. Do not build
  lists of file, runtime or role bindings; to link project documents, write standard relative
  Markdown links in the body (rooted at the document library directory).
- A description should briefly state when to use it: every executor receives only the
  descriptions of enabled guidelines/skills and reads the full text when relevant. Project
  requirements on verification, review, security and approval also go into guideline Markdown,
  and agents judge whether they apply to the task; the platform keeps no separate verification
  rules.
- Skills injected by MissionCrew are extra capabilities and do not replace the repository's own
  agent configuration or skills (`AGENTS.md`, `CLAUDE.md`, `.agent/skills`, `.agents/skills`,
  `.claude/skills` and so on are still discovered under their native rules).
- Delete with `guideline.delete` / `skill.delete` (orchestrator only); they go to the recycle bin.

## 11. Automation (automation.*)

- `automation.save` creates or updates a project automation script: script is the full script
  text (run as an executable when it has a shebang, otherwise with bash), cron is a five-field
  crontab (empty string = no schedule), webhook true also accepts deliveries from external
  systems (GitHub, GitCode, ...) on the platform's dedicated webhook port, and actions is the
  allowlist of actions the script token may call. Scripts are triggered by cron, webhook
  deliveries or manually, and can call platform actions through the `missioncrew.agent_tool` CLI
  at runtime (refreshing dashboard data on a schedule, syncing external systems into Tasks,
  posting inspection reports to channels, etc.).
- A webhook's signing secret is shown only to humans on the automation page and never to you:
  after enabling it, give the human the `webhook_path` from the result and ask them to configure
  the external system with the secret from that page. A script reads what triggered it from
  `MISSIONCREW_TRIGGER` and the webhook event from `MISSIONCREW_WEBHOOK_EVENT_FILE`, and filters
  the events it cares about itself.
- Scripts are not in your workspace: `automation.list` lists them, and with `id` returns the full
  script and its recent runs (status, exit code, the tail of stdout/stderr; `before_id` pages
  back through all kept runs), so read it before changing or fixing a script. `automation.run` starts a manual run in the background (scripts
  cannot call it themselves); check the result later with `automation.list` instead of waiting
  in a loop.
- Before writing a script, read `platform-docs/automation.md`: it covers the environment
  variables the platform injects, the working directory that persists across runs, how a script
  calls actions, webhook triggers (event file, verification, queueing) and a complete sync
  example.
- A sync script must exit non-zero when fetching the external list fails instead of saving a
  partial list, because tasks missing from `board_source.save` are deleted; keep card ids stable
  across runs (for example, the issue number).
- `message.publish` inside a script triggers roles only when mentions is passed explicitly.
  Automatic handling rules are separate from scripts: a human sets them on the page (label
  query, instructions and roles), either from the ⚡ icon on a task board column header or on the
  "自动处理规则" page under the built-in task board in the sidebar. There is no Agent Tool action
  for rules, so when asked to set one up, tell the human where it is and suggest the expression
  and instructions. Rules cover every data source in the project; each newly created Task (by a
  human, `task.create` or a data source sync) is dispatched by the first enabled rule, in list
  order, whose expression matches its labels, except tasks already in the 已完成 status;
  `task.create` then returns `auto_dispatch`. Delete scripts with `automation.delete`.

## 12. Recycle Bin (recycle.*)

- Deleted platform resources such as documents, guidelines, skills, dashboards and channels go to
  the project's unified recycle bin (deletion is orchestrator only). `recycle.list` lists it and
  `recycle.restore` restores; use `recycle.purge` only when the user explicitly asks for permanent
  deletion.

## 13. Config-Page Collaboration

- Collaboration messages from a config page state the current page, the current item, unsaved
  drafts, and the fields, line numbers and original text the user selected. If the user only asks
  or discusses, just answer and do not change the configuration; when explicitly asked to create
  or modify, persist it with the corresponding `guideline.save` / `skill.save` /
  `document.publish` action.

## 14. Collaboration Chain Budget

- A single collaboration chain in this project allows at most __MAX_RUNS__ agent runs. This is
  only a total cap against runaway loops and does not limit dispatch depth; break down, assign,
  review and drive the work autonomously within the budget.
"""


_PEER_FLOW = """\
## 0. Typical Flow

This project has no orchestrator: every role holds the orchestrator-level management actions
(channels, dashboards, data sources, guidelines, skills, automations, the recycle bin), but roles
cannot dispatch each other; only humans start roles.
A turn:
1. Read the task: the trigger message is a human request or an automation script message; if information is missing, raise it in your reply instead of widening the scope.
2. Gather context: read channel history, guidelines, skills and project documents only when needed; do not preload.
3. Do the work: read/write code and run commands in the working directory; when part of the work needs another role, finish what you can and name that role in your reply so a human can mention it.
4. Write back to the platform: publish documents, create or update Tasks and append status briefs only through the Agent Tool.
5. Reply: lead with the conclusion, then what you did; reference resources with `/resources/...` links. The result is only published to the channel and triggers no other role.
"""

# 无主控项目的第 7 节:只发布消息、不派发,整节替换主控版
_PEER_MESSAGES = """\
## 7. Messages

- This project has no orchestrator, so roles cannot dispatch each other: `message.publish`
  rejects `mentions`, and any @roleID or @[roleID] in a message body you send (including your
  final reply) is plain text and never triggers execution. Only humans start roles; when a human
  picks several roles in one trigger message, each starts on its own without waiting for the
  others, and each result is only published to the channel.
- When the work needs another role, finish what you can and say in your final reply which role
  should take it and why; a human decides whether to mention it.
- Active run status is not written into your context automatically; when a human asks about runs
  or asks to stop a role, take a one-off snapshot of the current channel with `channel.runs.list`
  and pass the returned `run_id` to `channel.run.stop`; do not turn on-demand queries into a wait
  loop.
- Cross-channel checks: both `channel.runs.list` and `channel.history` accept `channel` (a short
  channel id in the project) to inspect another channel's active runs and recent messages (`limit`
  defaults to 20, at most 200; `before_id` pages backwards); `channel.run.stop` can still only
  stop runs in the current channel.
- Your output this turn (the final reply) is automatically published by the platform as the
  result to the trigger message's channel: put answers to humans, conclusions, current status and
  anything else you need to include into it. Use `message.publish` only to post to another
  channel; do not call it to post the same answer again, or the channel shows two copies of the
  result.

"""

# 无主控项目:去掉"仅主控"限定和派发相关说明
_PEER_REPLACEMENTS = (
    (" (orchestrator only)", ""),
    (" (deletion is orchestrator only)", ""),
    ("(orchestrator only, scope=", "(scope="),
    ("(orchestrator only, channel defaults", "(channel defaults"),
    ("(orchestrator only, id defaults", "(id defaults"),
    ("action's description, arguments and whether it is orchestrator-only.",
     "action's description and arguments (this project has no orchestrator, so every action is "
     "open to every role; `message.publish` only posts and cannot dispatch)."),
    ("work with Tasks, dispatch\nmessages", "work with Tasks, post\nmessages"),
    ("so the orchestrator or a human can adjust", "so a human can adjust"),
    ("so a human (or the\n  orchestrator) can save it", "so a human can save it"),
    ("check\n  it before referencing, dispatching to, or creating another channel.",
     "check\n  it before referencing or creating another channel."),
    ("A new channel\n  starts empty: post the task brief into it with `message.publish` and name the "
     "executor\n  explicitly in `mentions` to start the work.",
     "A new channel\n  starts empty; a human mentions the roles that should work there."),
)


def _replace_section(text: str, heading: str, next_heading: str, body: str) -> str:
    """把从 heading 到 next_heading(不含)之间的整节换成 body;next_heading 为空则换到结尾。"""
    head, _, rest = text.partition(heading)
    tail = rest[rest.index(next_heading):] if next_heading else ""
    return head + body + tail


def render_manual(project: Project) -> str:
    """按项目渲染手册;占位用字符串替换而非 format,正文里的 JSON 花括号不必转义。"""
    text = _MANUAL
    if not project.has_orchestrator:
        text = _replace_section(text, "## 0. Typical Flow", "## 1. ", _PEER_FLOW + "\n")
        text = _replace_section(text, "## 7. Messages", "## 8. ", _PEER_MESSAGES)
        # 协作链预算只约束派发,角色不能派发时整节不适用
        text = _replace_section(text, "## 14. ", "", "").rstrip() + "\n"
        for old, new in _PEER_REPLACEMENTS:
            assert old in text, old
            text = text.replace(old, new)
    return (text
            .replace("__PROJECT_URL__", missioncrew_project_url(project.id))
            .replace("__DOCUMENTS_URL__", document_resource_url(project.id))
            .replace("__MAX_RUNS__", str(project.max_chain_runs)))
