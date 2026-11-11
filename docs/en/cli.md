# Command-line tool `mc`

Day-to-day MissionCrew operations happen in the web UI, and resources such as channels, tasks, documents and guidelines are usually created and maintained by the orchestrator directly in the conversation; `mc` is the terminal form of the same business logic and applies the same validation rules as the API, which makes it suitable for scripting, headless environments and troubleshooting. After installing the dependencies, invoke it with `uv run mc <subcommand>`, or run `mc` directly after activating `.venv`.

All subcommands read and write the same data directory: by default `~/.missioncrew/` under the user's home directory, which can be overridden with the environment variable `MISSIONCREW_HOME`. If the data directory does not exist, it is created silently; if the service instance uses a different directory through `MISSIONCREW_HOME`, set the same value before running the CLI, otherwise you operate on an empty database. `mc --help` and the `--help` of each subcommand list the full options; this document groups them by purpose.

## Service and audit

| Command | Description |
|---|---|
| `mc serve [--host H] [--port P] [--chat-workers N] [--webhook-host H] [--webhook-port P]` | Starts the web service (REST API + pages); by default it listens only on the local `127.0.0.1:8321`. `--host 0.0.0.0` or the environment variable `MISSIONCREW_HOST` opens it to LAN access (the platform has no authentication; use it only on a trusted network). `--chat-workers` sets the global limit on turns executing at the same time (default 16; the environment variable `MISSIONCREW_CHAT_MAX_WORKERS` also works): only turns that are executing count, queued ones do not; runs of the same role in the same channel are still serialized per session, and a single role's cross-channel limit is set with the role's "concurrency limit", see [resources.md](resources.md). It also opens a separate port that only receives webhook deliveries for automation scripts (default `8322`; change it with `--webhook-port` or `MISSIONCREW_WEBHOOK_PORT`, `0` turns it off; it listens on the `--host` address unless `--webhook-host` or `MISSIONCREW_WEBHOOK_HOST` is set); see [automation.md](automation.md). For a long-running service use pm2; see "Running under pm2" in README.en.md. |
| `mc audit [TASK_ID] [--limit N]` | Shows the audit log; every platform decision is traceable. Can be filtered by task; defaults to the latest 50 entries. |
| `mc demo [--no-run]` | Writes a mock runtime and the sample project `webshop` into the current data directory, and demonstrates one flow in which a task is handed to the orchestrator through a channel. For development and debugging only: it writes demo data into the real data directory, so point `MISSIONCREW_HOME` at a temporary directory when using it. |

## Runtime (backend)

| Command | Description |
|---|---|
| `mc backend detect [--no-register]` | Scans the Agent CLIs installed on this machine and registers them; same as "Re-detect" (重新检测) in the web UI. For tools that are already registered, only the path, version and built-in model list are refreshed, and the user's enable/disable and quota adjustments are kept; it also ensures that the global role templates exist, and automatically creates the `default` project when the platform has no project yet. Exits with a non-zero status when no tool is detected. |
| `mc backend list` | Lists the runtimes in the registry with their adapter, tier, cost, quota and capabilities. |
| `mc backend add --file <yaml>` | Imports/updates runtime records (single or list) from YAML, for manual customization; see `examples/backends.real.yaml` for an example. |

## Projects

| Command | Description |
|---|---|
| `mc project add --file <yaml>` | Imports/updates a project from YAML (including skills and controlled resources). Guidelines are imported together with the YAML only when the project is newly created; when an existing project is updated, the project guideline file library is authoritative, so edit guidelines on the web Guidelines page. A new project copies the global role templates as they are at that time and initializes the `general` channel and the document library; when `orchestrator_role_id` is not specified, the first template entry becomes the orchestrator, and an explicit empty string means no-orchestrator mode (all roles have equal rights). See `examples/project.template.yaml` for an example. |
| `mc project list` | Lists projects and their guideline counts. |
| `mc project show <project_id>` | Prints the complete project definition as JSON. |

## Roles

| Command | Description |
|---|---|
| `mc role list [-p project]` | Lists roles with their enabled state, runtime/model/effort, preferences and capabilities. |
| `mc role add --file <yaml> [-p project]` | Imports/updates roles (single or list) from YAML; `-p` overrides `project_id` in the YAML. Validation is largely the same as in the web UI: the project must exist, the runtime must be registered, the effort must be a level supported by that model, the current orchestrator cannot be disabled, and the current orchestrator cannot be set to manual mention only (`manual_only`). Model validation is stricter than in the web UI: when the runtime has a registered built-in model list, the model must be in that list (when the list does not include the CLI default entry, leaving the model empty is also rejected), and models detected live in the web UI are not accepted; in addition, the CLI does not check whether the runtime is disabled. See `examples/roles.yaml` for an example. |

## Channels and chat

| Command | Description |
|---|---|
| `mc chat channels [-p project]` | Lists channels, including their project and working directory. |
| `mc chat channels --create <name> -p <project> [--workdir dir]` | Creates a channel whose id is `<project>:<name>`; a `/` in the name (sidebar grouping) becomes a hyphen in the id (`backend/api` → `backend-api`). |
| `mc chat send "<content>" [-c channel] [-p project] [--author name] [--no-wait]` | Sends a message to a channel; the default channel is `general`. `@[role]` (with square brackets) in the content explicitly triggers that role to run, while a plain `@role` is just body text; if no role is mentioned, the message goes to the orchestrator. By default it waits until all runs triggered by this message (including cascades) finish, and prints the new messages produced in the meantime. |
| `mc chat log [channel] [-p project] [--limit N]` | Shows the recent chat history of a channel. |

The channel argument can be either the full id (such as `default:general`) or just the name; when the name is used in multiple projects, add `-p` to qualify it.

## Tasks

| Command | Description |
|---|---|
| `mc task create -p project --title title [-s summary] [-b body] [-l label]... [-c channel-id]... [--status S] [--source data-source] [--url link] [--meta note]... [--external-id external-id] [--process]` | Creates a task. The status is free text (built-in values `待处理`, `处理中`, `已阻塞`, `已完成`) and is stored as a `status: <text>` label; `--source` takes a custom data source's short id (the built-in source by default), and without a status the task gets that source's first status value; `--url`, `--meta` and `--external-id` record the external item's link, board notes and id (`external_id` is unique within the source, and a custom source's whole sync matches by it); `--process` sends the task to the orchestrator of the first bound channel right after it is created. |
| `mc task process <task_id> [-m note] [-c channel-id]` | Notifies the orchestrator in one channel to process the task: `-c` picks the receiving channel (an unbound one is bound as well), defaulting to the first bound channel. |
| `mc task brief <task_id> -m <content> [--status S]` | Appends a status brief, and can update the task status at the same time. |
| `mc task list` / `mc task show <task_id>` | Lists tasks; shows a single task and its status briefs. |
| `mc task delete <task_id> [-y]` | Moves the task and its briefs into the project recycle bin; it can be restored from the project recycle bin in the web UI. |

## Agent-side command

Within a run turn, agents operate on platform resources with a different command that is installed with the package, `missioncrew-tool`: it calls the server's Agent Tool API with the role token, and permission checks are done on the server. See [Agent Tool API](agent-tool-api.md) for the contract and the list of actions.
