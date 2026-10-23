# Agent harness workspace and project data boundaries

MissionCrew is a local multi-agent harness: it assembles roles, runtimes/models, project context, shared materials, and channel collaboration. A task is an issue that enters channel collaboration, not a separate execution flow. MissionCrew is not a business repository, and platform runtime files must not be written into business code directories.

## The two kinds of `docs` / `documents` are not the same layer

- The source repository's `docs/` holds MissionCrew's own development and usage documentation and is committed with the source code; the English version lives in `docs/en/`, which is where this file is. Running agents read a read-only copy of the English docs (`docs/en/`) through the workspace's `platform-docs/`; the entry point is the `README.md` index.
- The agent harness's `.missioncrew/documents/` is the versioned document entry point of a MissionCrew project, read and written by running agents. It lives in the platform data directory, not in the business source repository.

Therefore, the fact that the source repository uses `docs/` does not mean the runtime entry point should also be called `docs/`. The stable name of the runtime read/write entry point is `documents/`, and its environment variable is `MISSIONCREW_DOCUMENTS_DIR`; the prefix for project resources exposed to channels or the Web is `MISSIONCREW_PROJECT_URL`, and the document entry point also has the convenience variable `MISSIONCREW_DOCUMENTS_URL`.

## Working directory and harness directory

Each run involves two locations at the same time:

1. `workdir` is the actual working directory. When the channel is bound to a code repository, it is usually that repository, and business source code and deliverables should be modified here.
2. `MISSIONCREW_WORKSPACE` points to a separate `.missioncrew/` in the platform data directory. It holds the project materials, task views, history, and run records provided by MissionCrew, and never enters the business source code or business Git commits.

Chat workspaces are isolated and reused per `project × channel × role`. Different roles do not share the same `channel-history.json` or Agent Tool token, but `.missioncrew/documents/` links to the same project document library, so project documents are shared among the roles permitted to run.

## File system boundaries

The prompt requires the agent to read and write only the paths listed as "Read/write directories" for this run and their subdirectories, and not to proactively use `/tmp`, `/var/tmp`, other project directories, unauthorized user files, or the MissionCrew source directory; shell redirections, pipes, background logs, and files generated automatically by tools must respect the same boundary. Note that this is a prompt-level constraint: the default `workspace-write` policy adds the system temp directory and runtime-owned directories (such as `~/.codex` and the platform's pi home directory) to the sandbox's writable roots (`_prepare` in `runtime/manager.py`), so that toolchains that implicitly use `/tmp` do not fail inside the sandbox; `read-only` / `full-access` add no such directories.

When a command would normally use an external temp path, rewrite it before running to a task directory the business repository already uses (for example the project's conventional
`.tmp/` or `.e2e/`); when the project has no such convention, use `$MISSIONCREW_WORKSPACE/temp`, and clean it up when the
task ends. Do not access the external path first and then wait for permission approval.

A typical layout:

```text
.missioncrew/
├── README.md
├── manual.md
├── project.md
├── platform-docs/          # MissionCrew's own docs (read-only copy of the repository's docs/en/)
├── documents/              # Entry point to the project's versioned document library
├── tasks/                  # Markdown views of project tasks
├── guidelines/             # Project-level shared live view of enabled guidelines
├── skills/                 # Project-level shared live view of enabled skills
├── channel-history.json    # Chat workspaces only; isolated per role
├── .agent-tool-token       # 0600 role token
└── runtime/                # Diagnostic files such as recent runtime output
```

Besides the chat workspaces, each channel also has two channel-level shared directories, at the same level as each role's `.missioncrew`: `uploads/` stores attachments that humans upload through the input box, and `page-context/<kind>/` stores snapshots of the page content when a config-page chat on the Web document or guideline page is sent to the channel. They do not belong to any role; when they exist, they are authorized for every role that runs in that channel, so the same page message can read the paths given in it whether it is handed to the orchestrator, a single role, or multiple roles.

## Read/write semantics of each directory

### `documents/`

`documents/` is a symbolic link to the project's document worktree. Agents can read Markdown or other project materials; when a chat role publishes or replaces a file it should use the `document.publish` Agent Tool action, and to move or rename a file it should use `document.rename`, so that path, conflict, and permission errors come back within the current turn and an audit record carrying the role's identity is produced. The orchestrator can delete files with `document.delete`, and the deletion itself also creates a Git revision. Scanning for direct edits after a run ends exists only for compatibility with old sessions. The Git history of this document library does not belong to the business repository and is not in the workspace either; agents list versions and read old content with `document.history` (`guideline.history` / `skill.history` for guidelines and skills). Text or binary files that humans upload from the Web documents page also go into the same worktree and revision history, and are not copied into the business source directory.

Document directories are not standalone resources. A relative path `a/b/c.md` appears on the Web as an `a/b` directory containing `c.md`; publishing or renaming a file creates the target's parent directories automatically. Empty directories do not appear in the file list or the Git history, so there are no separate actions to create, move, rename, or delete a directory.

When guidelines and skills need to reference project documents, use ordinary Markdown links. Agents read the link targets on demand according to the task; the platform does not need to maintain an extra reference list.

Internal paths are only for runtime tool access and are not Web addresses. Chat replies and the API use root-relative resource URLs:

```text
/resources/<project-id>/documents/<path-relative-to-document-library>
```

For example `[Architecture overview](/resources/acme-shop/documents/architecture/current.md)`. This URL can be clicked, refreshed, and copied, and does not contain the platform data directory; the Web resolves it to the corresponding project's document page. Before publishing an agent reply, the platform also normalizes real document library paths that were output by mistake into resource URLs, and the frontend remains compatible with path links already saved in history.

## Unified resource URLs

Channels, tasks, dashboards, guidelines, skills, and documents use the same set of project-level resource URLs. Chat Markdown, platform action receipts, and the API's `resource_url` all use these addresses; clicking, refreshing, copying, and browser back/forward restore the corresponding project and item. For the complete resource lifecycle, API, and agent and Web contracts, see [MissionCrew resources](resources.md).

| Resource | URL |
| --- | --- |
| Channel | `/resources/<project>/channels/<channel-id>` |
| Task | `/resources/<project>/tasks/<task-id>` |
| Custom dashboard | `/resources/<project>/dashboards/<board-id>` |
| Built-in task board | `/resources/<project>/dashboards/tasks` |
| Guideline | `/resources/<project>/guidelines/<name>` |
| Skill / file inside a skill | `/resources/<project>/skills/<skill-id>[/<relative-path>]` |
| Project document | `/resources/<project>/documents/<relative-path>` |
| Project recycle bin | `/resources/<project>/recycle-bin` |

`channel-id` and `board-id` use short IDs within the project; the URL does not expose the `<project>:<id>` namespace used by the database. `dashboards` is the public resource type for dashboards; when the Web reads an old-style `boards`, `dashboard`, or `panels` address, it normalizes it to `dashboards`. Real directories are only for runtime tool calls and cannot be used in place of these Web URLs.

### `platform-docs/`

`platform-docs/` links to `platform-docs/` in the data directory; its content is a copy of the source repository's English docs, `docs/en/*.md`, shared by all projects and all roles. Each time the platform assembles a workspace, it compares the copy with the source, rewrites only the files whose content differs, and deletes extra files, so any changes an agent makes to it are reverted at the next assembly. `README.md` is the index and describes each document's content and when to use it; `manual.md` only contains the agent's behavior rules, while the complete description of platform mechanisms is here, for agents to look up as needed.

### `tasks/`

`tasks/` only holds platform-generated snapshots of project tasks; it is not a directory for collaboration drafts or reports. Snapshots are read-only; when an agent creates, edits, appends a brief to, or deletes a task, it must call the Agent Tool explicitly, and file changes are not synced back to the task. A task contains `title`, `summary`, a body, `labels` (the status is a `status: <text>` label), and optional `channel_ids`; tasks of a custom data source additionally have `source` in the snapshot, and tasks that have `external_id`, `url` (the link to the original item) or `meta` (synced ones, or ones created with these fields) carry those fields too. Snapshots are refreshed each time the workspace is assembled, and also immediately after one of the calling role's own `task.*` actions succeeds. Chat roles create a task with `task.create` and modify a task with `task.update` carrying `snapshot_updated_at`; the tool reports version conflicts immediately. Append a status brief with `task.brief`; the project orchestrator deletes a task with `task.delete`, and the task and its briefs go into the project recycle bin. `status_briefs` in the snapshot frontmatter is read-only history. Collaboration drafts and reports should be put in a suitable directory of the document library through `document.publish`.

The status is free text; the built-in board uses `待处理` (to do), `处理中` (in progress), `已阻塞` (blocked), and `已完成` (done) by default, and humans, agents, and scripts can all change it directly. The Agent Tool uses `snapshot_updated_at` to keep an old version from overwriting newer content, and on a conflict the error carries back the current task. Channel binding is optional; "processing a task" simply means sending a dispatch message in one channel: the first bound channel by default, or the project's `general` channel when there is no binding, and a human dispatching it can pick another channel.

### `guidelines/` and `skills/`

`guidelines/` points to a project-level shared live view that holds the complete Markdown of all enabled guidelines, with `name` and `description` in the frontmatter. Guideline source files are managed by an ordinary worktree plus a separate bare Git history; on save, restore, enable/disable, or delete, the platform first updates the source of truth and the project index, then refreshes the live view with atomic file replacement, and only then returns success. All channel role workspaces link to the same view, so within the same agent turn, re-reading after `guideline.save` returns gets the new version without waiting for the next assembly; a deleted or disabled guideline also disappears from the view immediately; disabled guidelines are not in the view, and the orchestrator reads them with `guideline.list` (likewise `skill.list` for skills). The shared context only provides guideline summaries, content versions, and file paths; the agent first judges relevance from `description` and reads the full text when needed.

`skills/` likewise points to a project-level shared live view, in which each enabled skill links to the project's complete skill directory; besides `SKILL.md`, a skill can contain relative files such as `scripts/`, `references/`, and `assets/`. The skill source of truth has its own separate bare Git history, which stores the whole package and the files' executable permissions; `skill.save`, `skill.delete`, Web editing, import, and scanning all commit content changes and refresh the shared view before they complete, so once an action returns success, creation, enabling/disabling, content updates, and deletion are all visible to every existing workspace. Guidelines and skills are not bound to roles or runtimes; the agent judges whether they apply to the current task. A new package copied directly into the project's skill drop directory enters the project index and revision history at the next scan or run assembly. For the import, direct drop, and history restore rules, see [Project skills as complete directories](skills.md).

## Consistency boundaries

| Resource | Source of truth | Agent entry point | Guarantee when the call returns success |
| --- | --- | --- | --- |
| Document | Project document worktree + separate Git history | Link to the worktree directory | Publishing, overwriting, or moving to the recycle bin is reflected directly in all workspaces |
| Guideline | Guideline worktree + Git history + Project index | Project-level live view of enabled guidelines | The index and the atomic mirror have both been refreshed; all workspaces read the same version |
| Skill | Project skill package directory + Git history + Project index | Project-level live view of enabled skills | The package version, content, and list of enabled members have all been refreshed |
| Task | SQLite task + status brief records | Role workspace Markdown generated at each assembly | Tool changes refresh the calling role's snapshot; content edits use `snapshot_updated_at`, and appending a brief does not overwrite history |

"Returns success" here means the Agent Tool or Web API has completed the entire sync chain. If any step among Git, the index, or the live view fails, the call returns a structured error instead of success; the guideline view is replaced using a temp file in the same directory, so readers only ever see the complete old version or the complete new version, never a half-written body. A prompt already sent to the runtime is not modified retroactively during the turn, but the file paths given in the prompt point to the new version in real time; the next assembly recomputes the shared context version and sends the new summary to the reused session.

Project-level deletions are all written to `projects/<project>/recycle-bin/`, but this internal directory is not authorized to the runtime. Agents use `recycle.list`, `recycle.restore`, and `recycle.purge`, and humans use the project recycle bin page; documents, guidelines, skills, dashboards, channels, roles, tasks, and local code repositories share the same list. A restore returns success only after the content, the project index, and the shared live views have all been refreshed; when the target identifier conflicts, the recycled item is kept so that the user can rename it, remove the conflict, or permanently delete it, and then handle it again.

### `channel-history.json`

The history file in a chat workspace is structured JSON that the agent reads proactively when the recent conversation excerpt is not enough. The orchestrator role can see full role information; executors read a separate redacted view, in which other executors are anonymized and their runtime, model, and effort are not exposed. Only the orchestrator can dispatch other roles; through `message.publish`, executors can mention only the orchestrator.

### `runtime/`

`runtime/` stores diagnostic information such as the runtime's recent output, used for live display, troubleshooting, and recovery. It is platform runtime state and must not be committed to the business repository. Runs without a harness workspace (channels that belong to no project) write diagnostic logs to `runtime/` under the platform data directory root, and likewise do not create `.missioncrew/` in the business repository.

## Injected environment variables

| Variable | Meaning |
| --- | --- |
| `MISSIONCREW_WORKSPACE` | The `.missioncrew/` root directory of the current channel role |
| `MISSIONCREW_PROJECT_URL` | The Web resource URL prefix of the current project, i.e. `/resources/<project>` |
| `MISSIONCREW_DOCUMENTS_DIR` | The current project's document entry point, i.e. `.missioncrew/documents/` |
| `MISSIONCREW_DOCUMENTS_URL` | The Web resource URL root of the current project's documents; the final reply uses it to build links |
| `MISSIONCREW_GUIDELINES_DIR` | Directory of enabled guideline Markdown files |
| `MISSIONCREW_SKILLS_DIR` | Directory of enabled skills |
| `MISSIONCREW_TASKS_DIR` | Directory of project task Markdown files |
| `MISSIONCREW_MANUAL` | The agent manual `manual.md` |
| `MISSIONCREW_PLATFORM_DOCS_DIR` | The copy of MissionCrew's own docs, `platform-docs/` |
| `MISSIONCREW_CHANNEL_HISTORY` | The channel history file visible to the current role; chat runs only |
| `MISSIONCREW_CHANNEL_ID` | The current channel id |
| `MISSIONCREW_SESSION_KEY` | The runtime session key of the current channel×role |
| `MISSIONCREW_ALLOWED_DIRS` | All authorized directories after unified policy resolution (JSON array): local code repositories, the project document library, the harness workspace, the guideline/skill views, the skill drop directory, the platform docs copy, the channel upload directory, plus the system temp directory and runtime-owned directories added by the default policy |
| `MISSIONCREW_READABLE_DIRS` / `MISSIONCREW_WRITABLE_DIRS` | JSON arrays of readable / writable directories after unified policy resolution |
| `MISSIONCREW_SKILL_DIRS` | JSON array of the skill directories assembled for this run |
| `MISSIONCREW_RUNTIME_PERMISSIONS` | JSON of the approval / filesystem / network permission intent |
| `MISSIONCREW_AGENT_TOOL_URL` | Root address of the unified Agent Tool API |
| `MISSIONCREW_AGENT_TOKEN_FILE` | Bearer capability file whose path is stable and whose content is atomically rotated per run |
| `MISSIONCREW_AGENT_TOOL_PYTHON` | Python interpreter that can run the Agent Tool CLI module |

The three Agent Tool variables above are injected both in chat runs and in automation scripts; an automation script's token file is at `automations/<project>/<id>/.agent-tool-token`, and the script additionally gets `MISSIONCREW_PROJECT_ID` and `MISSIONCREW_AUTOMATION_ID`.

The runtime's actual `PWD` is still `workdir`. Adapters that support native multi-directory authorization convert the allowed directories into the corresponding command-line arguments; other runtimes can also learn these paths from the environment variables and the prompt context. For the Agent Tool's identity, scope, action, and error contracts, see [MissionCrew Agent Tool API](agent-tool-api.md).

## Platform-internal data boundary

Project databases, secrets, document Git metadata, the raw channel history stored by the platform, and other roles' history views are outside the agent's authorized scope. The agent only gets the business directories the current run needs and its own harness workspace.

## Development constraints

- Do not automatically create `.missioncrew/` in a business repository bound to a channel.
- Do not rename the runtime `.missioncrew/documents/` just because the source docs live in `docs/`.
- When migrating runtime directories, automatically handle only symbolic links that the platform created and whose targets are unambiguous; ordinary files or directories must be kept and the conflict reported.
- When adding a new writable file, make clear whether it belongs to shared project materials or to role-isolated state, and choose its directory accordingly.
- After project settings change, refresh the materialized files and the persistent shared context; reused sessions must not keep using the old configuration.
- Business code and deliverables are written to `workdir` or a local code repository; project documents enter the document library through `document.publish`, and tasks are written to the platform through task actions.
- Agent replies must not publish real `.missioncrew` paths or `file://` links; reference platform resources with `/resources/<project>/<resource-type>/<id-or-path>`.
