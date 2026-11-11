# MissionCrew resources

MissionCrew resources come in two related layers with different purposes:

1. **Public resource URLs** are used in chat Markdown, Web navigation, and API return values, in the format `/resources/<project>/<resource-type>/<id-or-path>`.
2. **Agent harness file entry points** live in the `.missioncrew/` workspace the runtime receives, and let agents read documents, tasks, guidelines, and skills; chat roles modify platform resources explicitly through the Agent Tool.

A public URL is not a file path and does not grant the runtime any new file permissions; real paths in the harness are only for tool access and must not be published in chat messages.

Each resource type below has corresponding Agent Tool actions (see [Agent Tool API](agent-tool-api.md)). In daily use, humans do not need to enter items one by one on the pages: the orchestrator can create channels, tasks, documents, guidelines, skills, dashboards, and automation scripts in the conversation as needed and paste the canonical URLs back into the channel; the Web pages are for viewing, reviewing, and manual intervention.

## Public resource URLs

### Specification

```text
/resources/<project-id>/<resource-type>/<resource-id-or-relative-path>
```

- `project-id` may contain only letters, digits, underscores, and hyphens.
- Each path segment of the URL is percent-encoded separately; a complete file system path cannot be used as a single resource identifier.
- The database ID of a channel or custom dashboard is `<project-id>:<item-id>`; public URLs use only the short ID within the project.
- The canonical public type for dashboards is `dashboards`. The frontend can read the old-style `boards`, `dashboard`, and `panels`, and then normalizes them to `dashboards`.
- Resource addresses are site-root-relative URLs, so the same Markdown message can be opened under localhost, a LAN address, or a reverse-proxy domain.

### Resource types

| Resource | Canonical URL | Stable identifier | What the Web opens |
| --- | --- | --- | --- |
| Channel | `/resources/<project>/channels/<channel-id>` | Short channel ID within the project | The corresponding chat channel |
| Task | `/resources/<project>/tasks/<task-id>` | Global task ID | Opened directly: the built-in task board with the task's details; clicking a task link of the current project inside a page: a details pop-up over that page |
| Custom dashboard | `/resources/<project>/dashboards/<board-id>` | Short dashboard ID within the project | The corresponding custom dashboard |
| Built-in task board | `/resources/<project>/dashboards/tasks` | Reserved identifier `tasks` (a frontend routing convention; the backend currently does not prevent creating a custom dashboard with the same ID, so avoid using that ID) | The project task board |
| Guideline | `/resources/<project>/guidelines/<name>` | `name` in the Markdown frontmatter | The corresponding guideline edit/preview page |
| Skill | `/resources/<project>/skills/<skill-id>` | Project skill ID | The skill's `SKILL.md` page |
| File inside a skill | `/resources/<project>/skills/<skill-id>/<relative-path>` | Relative path within the skill directory | The corresponding file in the file tree |
| Project document | `/resources/<project>/documents/<relative-path>` | Relative path within the document library | The corresponding versioned document page |
| Project recycle bin | `/resources/<project>/recycle-bin` | Project ID | A unified list of all restorable project resources |
| Project settings | `/resources/<project>/settings[/<section>]` | Project ID; optional section `info`, `notify`, `res`, `roles`, `credentials` | The project settings page, scrolled to the section when one is given (`settings/credentials` is the credentials form, used by the `credential.request` receipt) |
| Automation | `/resources/<project>/automations/<automation-id>` | Short automation ID within the project | The corresponding automation detail page |

For example:

```markdown
[Architecture document](/resources/acme-shop/documents/architecture/current.md)
[End-to-end testing guideline](/resources/acme-shop/guidelines/web-e2e-testing)
[Task board](/resources/acme-shop/dashboards/tasks)
[Architecture discussion channel](/resources/acme-shop/channels/architecture-doc)
[Project recycle bin](/resources/acme-shop/recycle-bin)
[Project settings → Credentials](/resources/acme-shop/settings/credentials)
```

## Storage and behavior of each resource type

### Channels

The source of truth for channels and messages is the platform database. A channel stores its purpose, working directory, and archive state; its latest activity time is computed live by the platform from the message table. A channel URL opens the project chat page and selects the corresponding channel. The name, purpose and working directory can be changed with "编辑频道" (Edit channel) in the "•••" menu of the channel's sidebar entry, or by the orchestrator with `channel.update`; a new working directory takes effect from that channel's next run. Archived and page-conversation channels cannot be changed.

A channel name can use `/` for grouping: `backend/api` appears in the sidebar as `api` under a collapsible `backend` group. Only the first `/` splits, giving a single level, and the expanded/collapsed state is kept in the browser; page-conversation channels are not grouped. Grouping only affects the sidebar, and a channel id still contains only letters, digits, underscores and hyphens: when a channel is created from the web UI or the CLI, a `/` in the name becomes a hyphen (`backend/api` gets the id `backend-api`); the orchestrator supplies its own id to `channel.create`, and renaming a channel never changes its id.

Archived channels are outside agent visibility by default. Humans can still view the history when they open an archived channel; before an agent posts a new message to an archived channel, the platform reactivates that channel. The project's internal `general` channel is the reserved default channel, and its resource URL uses the short ID `general`.

Within one channel a role executes one turn at a time. Trigger messages that arrive for it while it is busy (a human mention, an orchestrator dispatch, an executor's report, a fired callback, and so on) each queue; when its turn comes, the platform merges all messages queued at that moment into one turn: the role sees every message in chronological order with its trigger type in the same turn input, and it runs once and replies once. The merged turn is carried by the run of one of those messages; the reply is attached to that message and continues its collaboration chain: when an executor's queued messages include tasks dispatched by the orchestrator, the latest of those is used and the result goes back to the orchestrator as usual; otherwise the latest message is used. The runs of the other messages end with status `merged` and still count toward the run budget of their own collaboration chains; the chat page shows "排队时已合并" (merged while queued) under those messages with a jump to the turn that actually ran, and the carrying run's card states how many messages it handled together. Merging happens only at the moment the role's turn comes: messages that arrive afterwards keep queueing for the next turn, and a queued run that was stopped before the merge is not included. Stopping the carrying run stops this turn's handling of all merged messages. The "task finished" notification follows the collaboration chain of the carrying run; a chain that ends with `merged` is not notified separately.

Queued runs do not occupy execution slots; slots go only to turns that are actually executing. There are two limits on slots: the global limit is set by `mc serve --chat-workers` (or the environment variable `MISSIONCREW_CHAT_MAX_WORKERS`, default 16); in addition, each role can set a "concurrency limit" in the role editor (field `max_concurrency`) that caps how many channels it executes in at the same time, counted for the same role across the project's channels; empty or 0 means no limit. Once a role reaches its limit, messages that mention it in other channels queue even if global slots are free; other roles are not affected. When a slot frees up, waiting channels are dispatched in the order of each channel's earliest queued message. A queued run's process card carries a status line saying what it is waiting for: the previous turn in this channel has not finished, the role has reached its concurrency limit, or all global slots are taken. Reports from Runtime self-wakes (a background command finishing, a built-in timer, and so on) are started by the CLI itself and are not subject to either limit.

A channel-level stop ends all `queued`, `running`, and `waiting_user` chat runs in that channel at once, releases pending interactions, terminates the corresponding runtime processes, and leaves a visible platform message and an audit record. The platform keeps the persisted native session/thread IDs, so later runs can restart the process and try to resume the context; file writes that have already completed are not rolled back. Even when the database has no active run, calling the stop API again cleans up orphan processes based on the runtime session saved for the channel.

Main APIs:

- `GET /api/chat/channels`
- `POST /api/chat/channels`
- `GET /api/chat/<channel-id>/messages`
- `POST /api/chat/<channel-id>/messages`
- `POST /api/chat/<channel-id>/stop`

### Tasks

A task is an issue-like collaboration entry point, and its source of truth is the platform database. It contains a title, a summary, a body, a status, labels, one or more channel bindings, and multiple append-only status briefs. Visiting a task URL directly opens the built-in task board and expands the task's details; clicking a task link of the current project in chat, documents, or other pages only pops up the details dialog over the current page without switching pages (Ctrl/⌘-click still opens the full board in a new tab), while task links of other projects navigate as usual. On the chat page, a "Task N" button on the right of the channel title row shows how many unfinished tasks are bound to the current channel; clicking it opens a floating vertical list of those unarchived tasks with their status and title (only unfinished ones by default; it can switch to all), and clicking one also pops up its details; the button is hidden when the channel has no bound tasks.

The agent harness also provides Markdown snapshots in `.missioncrew/tasks/`. Agents modify the source of truth with `task.create`, `task.update`, and `task.brief`, and the project orchestrator deletes with `task.delete`; humans use the same set of Web/API fields. On deletion, the task and its status briefs go into the project recycle bin together, and after a restore it keeps the original task id. When a task is processed, the platform does not start a separate execution engine; instead it sends an ordinary message in a single channel: to the project orchestrator by default, or to the specified role when the message carries a structured mention or matches an automatic handling rule, and the existing channel collaboration flow takes over. The receiving channel defaults to the task's first bound channel (`general` when it has no binding); "交给角色处理" (hand off to a role) in the task details can pick any active channel of the project instead, which the API exposes as the `channel_id` field of the `process` request, and picking an unbound channel also adds it to the task's bindings.

Main APIs:

- `POST /api/tasks`
- `GET /api/projects/<project-id>/tasks?scope=active|archived|all[&channel=<channel-id>]` (the list omits bodies; `channel` keeps only the tasks bound to that channel)
- `GET /api/tasks/<task-id>`
- `PATCH /api/tasks/<task-id>`
- `DELETE /api/tasks/<task-id>` (moves the task to the project recycle bin)
- `POST /api/tasks/<task-id>/briefs`
- `POST /api/tasks/<task-id>/process`

### Dashboards

The source of truth for a custom dashboard is a `Board` in the platform database. A dashboard has a 12-column layout and widgets such as `markdown`, `table`, `card`, `chart`, `list`, `log`, `code`, and `taskboard`; widgets can also read live data from tasks, documents, audit records, or channel messages. Both task-reading widgets work like task boards: they read a single data source, chosen with `source` (default: the built-in `built-in`). A table bound to `{"from":"tasks","source":"pr-dev",...}` lists task rows, and a `taskboard` widget lists active task cards by `content.source` plus `content.label` (a label expression). Task status pills take the status color declared by the task's data source, with the same rule for the four built-in statuses and custom ones.

The built-in task board is not a deletable `Board` record but a special dashboard that the platform generates live from the project's tasks, so it uses the reserved address `/dashboards/tasks`. The code still uses the `boards` name internally for the model and API, but public URLs consistently use the user-facing `dashboards`.

Standalone task board pages support search, multi-select filters, property grouping, sorting within columns and column collapsing. Personal views are stored in the current browser separately for each project, board and data source, without writing back to shared configuration. Custom expression columns and automatic handling rules remain shared configuration. See [Task board filtering and columns](automation.md#task-board-filtering-and-columns) for interactions, label matching and read-only query parameters.

Main APIs:

- `GET /api/projects/<project>/boards`
- `GET /api/projects/<project>/builtin_board/data`
- `GET /api/projects/<project>/boards/<board-id>/data`
- `GET /api/projects/<project>/task_rules/preview?query=<condition>&rule=<original condition>`
- `POST /api/projects/<project>/boards`
- `DELETE /api/projects/<project>/boards/<board-id>`
- `POST /api/projects/<project>/widget_data`

### Guidelines

Guidelines use the same versioned file store as project documents: `projects/<project>/guidelines/` is an ordinary Markdown worktree, and the sibling `guideline-history.git` bare Git repository keeps the complete history. The Markdown worktree is the content source of truth; the guideline data in the project configuration is an index cache kept for compatibility with the existing project model and for fast assembly, and it is re-parsed and refreshed from the Markdown at startup and on read; the enabled state is not part of the Markdown content version. The file header uses only `name` and `description`, consistent with the backend:

```markdown
---
name: code-review
description: Run an independent review after changing business code or interfaces
---

# Code review guideline
```

Edits and renames each create a Git revision; restoring an old version writes one more new revision and does not rewrite the old history. Enabled guidelines are materialized into the agent harness at `.missioncrew/guidelines/<name>.md`. The shared context only provides `description`, the content version, the internal on-demand read path, and the Web URL; the agent reads the full text when it judges the guideline relevant. Guideline URLs use `name`, not absolute file system paths.

Main APIs:

- `GET /api/projects/<project>/guidelines`
- `POST /api/projects/<project>/guidelines`
- `GET /api/projects/<project>/guidelines/<name>/history`
- `GET /api/projects/<project>/guidelines/<name>/history/<revision>`
- `POST /api/projects/<project>/guidelines/<name>/restore`
- `DELETE /api/projects/<project>/guidelines/<name>`

### Skills

The source of truth for skills is the project-managed skill directory; each skill contains at least `SKILL.md` and can contain files such as `scripts/`, `references/`, and `assets/`. The sibling `skill-history.git` bare Git repository keeps the complete directory history, including the files' executable permissions; the enabled state is not part of the content version. A project supports ZIP upload, local directory import, and direct copying into the project's skill drop directory; for the complete format, history behavior, and import boundaries, see [Project skills as complete directories](skills.md).

Enabled skills are mapped into the agent harness at `.missioncrew/skills/<skill-id>/`. The skill root URL opens `SKILL.md`, and appending a relative path opens a file inside the skill directly. This URL is only for reading on the Web; when an agent runs scripts or reads reference material, it still uses the authorized real directory in the harness.

Main APIs:

- `GET /api/projects/<project>/skills`
- `GET /api/projects/<project>/skills/library`
- `POST /api/projects/<project>/skills`
- `GET /api/projects/<project>/skills/<skill-id>/history`
- `GET /api/projects/<project>/skills/<skill-id>/history/<revision>`
- `GET /api/projects/<project>/skills/<skill-id>/file?path=<relative-path>&revision=<revision>`
- `POST /api/projects/<project>/skills/<skill-id>/compare`
- `POST /api/projects/<project>/skills/<skill-id>/restore`

### Project documents

The source of truth for documents is the project document worktree, and a separate bare Git repository keeps the revision history. Chat roles publish files through the `document.publish` Agent Tool action and move or rename files through `document.rename`; end-of-run snapshots taken after direct edits exist only for compatibility with old sessions. Humans can also select several files at once to upload on the Web documents page: when no document is selected, they are saved to the document library root; when a document is selected, they are saved to that document's directory; a file with the same name is overwritten only after confirmation, and each file creates its own revision. The limit per file is 50 MB.

The Web directory tree is built on the fly from the relative paths in the file list; there is no standalone directory resource. For example, `a/b/c.md` appears as `c.md` under `a/b`; empty directories are not shown, and there are no separate APIs to create, move, rename, or delete a directory.

After upload, text files can still be edited and previewed online. The revision history table sits above the current document content, and its first row is clearly marked as the latest revision; users can select any two plain-text revisions in A → B order to view a unified diff. Binary files, non-UTF-8 files, and files containing binary control characters are saved as raw bytes, are not converted as text, and cannot be compared across revisions; the document page still offers downloads of the current and historical revisions. When a user asks the orchestrator from the floating chat bar on the document page, plain-text content is read on demand only through a temporary snapshot path in the role workspace; non-text files such as `.xlsx` get no text snapshot, line numbers, or selection; instead, the file name, the relative path in the document library, and the resource URL are handed to the orchestrator, and the runtime reads the file with tools suited to its format, using `MISSIONCREW_DOCUMENTS_DIR` as the root.

Document URLs use the relative path within the document library. Before publishing an agent reply, the platform converts known real document library paths into resource URLs, and the frontend also remains compatible with MissionCrew document paths saved in historical messages. Ordinary absolute paths to business source code are not automatically converted into resource links.

Main APIs:

- `GET /api/projects/<project>/documents`
- `POST /api/projects/<project>/documents/upload?path=<relative-path>&overwrite=<bool>` (the request body is the raw bytes of a single file)
- `GET /api/projects/<project>/documents/download/<relative-path>?revision=<revision>`
- `GET /api/projects/<project>/documents/file/<relative-path>`
- `PUT /api/projects/<project>/documents/file/<relative-path>`
- `GET /api/projects/<project>/documents/history?path=<relative-path>`
- `POST /api/projects/<project>/documents/compare` (compares two historical revisions of the same plain-text file)

### Unified project recycle bin

After deletion, documents, guidelines, complete skill packages, custom dashboards, tasks, channels (except content channels bound to a document/guideline/skill page, which are permanently purged when their content is deleted or when they themselves are deleted), non-orchestrator roles, and project resource references all go into the project's own unified recycle bin. Physical entries are stored in `projects/<project>/recycle-bin/<item-id>/`, which contains a platform-private manifest and the content or a database snapshot; this internal directory is never added to the runtime's allowed directories and is not exposed through the API. Complete skill packages in the legacy `.skill-trash/` are migrated into the unified recycle bin when the service starts.

The recycle bin page is a single project-level view that can be filtered by resource type. Each item shows its original name, stable identifier, deletion time, operator, and size. A restore rebuilds the resource's source of truth and syncs the Project index and the guideline/skill live views; the recycled item is removed only after the whole chain completes. If the original path or ID is already taken by a new resource, the restore returns `409`, and both the existing resource and the recycled item remain unchanged. Permanently deleting a single item and emptying the recycle bin cannot be undone, and the Web asks for confirmation first.

The recycle bin stores "the copy needed to restore the current resource"; it is not the sole source of truth for all history. After a recycled item is permanently deleted, the Git history of documents, guidelines, and skills, channel messages, and audit records still exist according to their own retention policies. The built-in task board, the project itself, global runtimes, and global role templates do not go into the project recycle bin.

Main APIs:

- `GET /api/projects/<project>/recycle-bin`
- `POST /api/projects/<project>/recycle-bin/<item-id>/restore`
- `DELETE /api/projects/<project>/recycle-bin/<item-id>` (permanently deletes a single item)
- `DELETE /api/projects/<project>/recycle-bin` (empties the recycle bin)

## File resources visible to the runtime

Each channel×role combination has its own `MISSIONCREW_WORKSPACE`, which the platform refreshes before each run. A typical layout:

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
└── runtime/
```

| Variable | Purpose |
| --- | --- |
| `MISSIONCREW_PROJECT_URL` | The current project's public resource URL prefix `/resources/<project>` |
| `MISSIONCREW_DOCUMENTS_URL` | The current project's document URL prefix |
| `MISSIONCREW_WORKSPACE` | The harness root directory of the current channel role |
| `MISSIONCREW_DOCUMENTS_DIR` | Read entry point for project documents; chat writes go through the Agent Tool |
| `MISSIONCREW_TASKS_DIR` | Markdown snapshots of project tasks; chat writes go through the Agent Tool |
| `MISSIONCREW_GUIDELINES_DIR` | Markdown directory of enabled guidelines |
| `MISSIONCREW_SKILLS_DIR` | Complete directory view of enabled skills |
| `MISSIONCREW_CHANNEL_HISTORY` | The channel history JSON visible to the current role; chat runs only |
| `MISSIONCREW_AGENT_TOOL_URL` | The API root address chat roles use to call unified platform actions |
| `MISSIONCREW_AGENT_TOKEN_FILE` | Agent Tool capability file whose path is stable and whose content is atomically rotated per run |

For the complete directory permissions, sync, and isolation boundaries, see [Agent harness workspace and project data boundaries](agent-harness-workspace.md).

## Content without a public project resource URL

The following may live in the MissionCrew workspace or settings but has no project resource URL:

- `channel-history.json` is role-isolated history for the current agent to read on demand; it is not a public download address for the channel's source of truth.
- `runtime/` is a directory for diagnostics and recent output, and must not appear in final replies or business commits.
- Project code repositories and ordinary local paths are resources authorized to the runtime and are not exposed through `/resources/...`.
- Runtimes, models, global settings, and project settings are system pages, not project content resources.

This distinction keeps "allowing an agent to read a directory" from being misread as "the directory can be accessed publicly through the Web".

## Unified contract for API, chat, and Web

### API

Resource lists and write results return `resource_url`. This field is only for navigation; it is a read-only derived value and does not enter the persisted model of projects, guidelines, or skills. When a client sends back a project object that contains `resource_url`, the backend ignores this read-only field.

### Agent and orchestrator actions

The project shared context provides `MISSIONCREW_PROJECT_URL`, the resource formats for project content and the recycle bin, and the Web URLs of the current guidelines and skills. Chat roles use the Agent Tool API with their role token to explicitly create channels; save or delete dashboards, guidelines, skills, tasks, or documents; manage recycled items; and post messages; the platform returns a structured result or error within the same turn, and success receipts use clickable Markdown links. For the complete call and permission contract, see [MissionCrew Agent Tool API](agent-tool-api.md).

An agent's final reply should use:

```markdown
[Readable title](/resources/<project>/<resource-type>/<id-or-path>)
```

Do not output absolute `.missioncrew` paths, platform data directory paths, or `file://` links.

### Web

The unified Markdown component recognizes same-origin resource URLs and hands them to the app router. The router restores the project, the resource type, and the specific item; paths of documents and of files inside skills are decoded segment by segment. When the user switches between channels, tasks, dashboards, guidelines, skills, documents, the recycle bin, or project settings within the app, the address bar updates to the canonical resource URL in sync.

Pages without a resource URL (Global settings `/<project>/settings`, Runtime status `/<project>/runtime-status`) only have a page address. Such an address written in Markdown, or the old project settings address `/<project>/proj`, also opens as a page when clicked; it is treated as a document library path only when its first segment is not an existing project.

The server returns the single-page app entry for resource addresses, and the frontend verifies whether the resource exists using overview/API data. When a resource has been deleted or its identifier is invalid, the frontend does not try to read a local path with the same name; instead it returns to a valid page of that resource type or shows that the resource was not found.

## Adding a new resource type

When adding a new project content resource, complete all of the following:

1. Register the canonical type in `missioncrew/collab/resource_urls.py` and provide a URL builder function.
2. Return the derived `resource_url` in the resource API and `/api/overview`, but do not write it into the persisted model.
3. Give canonical Markdown links in the agent shared context and in related platform action receipts.
4. Register the type or compatible aliases in `missioncrew/web/js/markdown.js`.
5. Implement the two-way mapping from URL to page state and from page state to canonical URL in `missioncrew/web/js/router.js`.
6. Verify that resource links can be clicked, refreshed, and opened across projects, and support browser back/forward.
7. Add tests for URL encoding, the API `resource_url`, history compatibility, and smoke tests with real data.

Resource URLs are a public navigation contract. Internal database table names, file layout, or UI tabs can be refactored, but canonical URLs already published into chat history must remain resolvable.
