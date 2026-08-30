# MissionCrew

[中文](README.md) | English

MissionCrew is a local multi-agent collaboration platform. It turns the Agent CLIs already installed on your machine (Claude Code, Codex, Grok, …) into a set of roles that work together like a team inside chat channels. You state what you need in a chat box and either address a specific role directly or let the orchestrator coordinate several roles to get it done.

The point is to combine several local Agent CLIs so you can play to the strengths of different tools and models — like a group chat where different tasks go to different models. Add your own guidelines and skills to build the agent team you want.

![One channel per issue or feature; a one-line request from a human, dispatched by the orchestrator to dev and tester and summarised back](docs/images/chat.jpg)

## Core concepts

MissionCrew exposes a set of tool commands to agents, so an agent operates platform resources by running commands. The main concepts are:

- **Project**: configuration and resources are isolated per project, so several efforts can run side by side.
- **Runtime**: an Agent CLI installed on this machine (Claude Code, Codex, …). Auto-detected and shared by all projects.
- **Role**: a runtime configuration plus a persona — for example one model acting as the developer and another as the tester.
- **Orchestrator**: one role chosen as the lead. It has more authority than other roles (it can create channels, for instance). A message that mentions nobody goes to the orchestrator by default, and replies from other roles automatically trigger it.
- **Channel**: a chat room. Every agent can read the conversation history and collaborate in it.
- **Guidelines**: a special kind of document whose list is injected into context. Use them to define workflows such as development or testing requirements.
- **Docs**: every project gets a versioned document library, a good home for material that does not belong in the code repository.
- **Automation**: a script triggered on a schedule or by hand that calls the MissionCrew API to perform actions.

**None of these resources need to be managed by hand.** Channels, tasks, documents, guidelines, skills, dashboards and automation scripts can all be created and maintained by the orchestrator through the Agent Tool: say "split the login rework into tasks and open a channel to track it", "write up what we just concluded as a document" or "put these testing requirements into a guideline" in the chat, and the orchestrator calls the matching actions and posts the results back as links; other roles can create and update tasks and publish documents too. The web UI is mainly for viewing, reviewing and the occasional manual tweak, not the everyday entry point for data entry.

Detailed docs (Chinese): [Runtimes](docs/runtimes.md), [Agent Tool API](docs/agent-tool-api.md), [Resources and URLs](docs/resources.md), [Harness workspace boundaries](docs/agent-harness-workspace.md), [Project skills](docs/skills.md), [CLI](docs/cli.md).

## Quick start

Requirements: Python 3.10+, [uv](https://docs.astral.sh/uv/), Node.js 18+ (npm is needed for pm2 and pi), and at least one logged-in Agent CLI on this machine (such as `claude` or `codex`).

```bash
uv sync            # create .venv and install dependencies
uv run mc serve    # start the web service on 127.0.0.1:8321
```

Open `http://127.0.0.1:8321`; everything else happens in the browser:

1. Go to **Global settings** and click **Re-detect** in the runtime list. The platform scans the installed Agent CLIs, registers them, and creates a `default` project with a set of default roles (`@lead` orchestrator, `@dev`, `@reviewer`, …) and a `general` channel.
2. Back on the chat page, type your request into the `general` channel. Mention nobody and the orchestrator takes over; type `@` to pick a role and it executes directly.
3. When needed, adjust each role's runtime/model and persona in **Project settings**. Guidelines, skills, documents, channels and tasks can be maintained on the pages by hand — or simply delegated to the orchestrator in the chat.

What each page does:

- **Chat**: the project switcher at the top is the entry point for every page. Type a request in a channel and it goes to the orchestrator by default. Typing `@` opens a role picker: pick one role and it executes directly, with the result staying in the channel; pick several and only the orchestrator starts, holding the full list and deciding whether to run them in parallel, in sequence, or to re-assign. A hand-typed `@name` is plain text and never triggers anyone.
- **Channels**: create, archive and delete channels from the sidebar. A channel records its purpose and working directory and can be bound to a real code repository. While agents are queued or running, the composer offers *Stop agent / Stop all*.
- **Task board**: tasks are issue-like — title, body, status, labels, channel bindings and append-only status briefs. *Hand to Lead* simply posts a message to the orchestrator in the bound channel; everything after that is ordinary chat collaboration, there is no separate task execution loop.
- **Project settings**: roles (fixed runtime/model + positioning + capabilities + preferences), multiple guideline Markdown files, complete skill packages, the versioned document library and custom dashboards. Documents, guidelines and skill packages are versioned in their own Git repositories with history, diff and restore; deleted items go to the project recycle bin.
- **Global settings**: role templates for new projects, the runtime list (install state, version, on/off switch, model list) and custom model providers.
- **Runtime status**: global runtime instances, call history, and the quota windows / reset times of the Codex, Claude, Kimi and Grok accounts logged in on this machine. Roles can opt into usage linkage individually: when a quota is exhausted only opted-in roles are disabled automatically, and they come back once the window resets.

**There is no authentication of any kind.** The web UI and API can browse local directories and dispatch agents that run commands, so `mc serve` listens on `127.0.0.1` only by default. To reach it from other devices on your LAN, pass `--host 0.0.0.0` explicitly (or set `MISSIONCREW_HOST`), do so only on a trusted network, and never expose it to the internet — see [SECURITY.md](SECURITY.md). The chat concurrency limit defaults to 16 and can be changed with `--chat-workers <N>` or `MISSIONCREW_CHAT_MAX_WORKERS`.

Platform data (SQLite, document libraries, agent workspaces, logs) lives in `.missioncrew/` under the current directory; override it with `MISSIONCREW_HOME`. Nothing is ever written into your code repositories.

## Running under pm2

For day-to-day use let [pm2](https://pm2.keymetrics.io/) manage the service. The repository ships a single entry point, `scripts/serve.sh` (configured in `ecosystem.config.cjs`, process name `missioncrew`, started as `.venv/bin/python -m missioncrew.cli serve`). Do not start the service by hand with `nohup`/`setsid`:

```bash
npm install -g pm2          # once

scripts/serve.sh start      # start (loopback only; for LAN access: MISSIONCREW_HOST=0.0.0.0 scripts/serve.sh start)
scripts/serve.sh status     # pm2 status missioncrew
scripts/serve.sh logs 100   # last 100 log lines
scripts/serve.sh restart    # restart
scripts/serve.sh stop       # stop
```

Conventions:

- The service inherits the caller's environment as usual — in particular the full `PATH`, which runtime detection relies on to find the installed Agent CLIs. After installing a new CLI run `scripts/serve.sh restart` (it passes `--update-env`) so the new tool is detected. Variables injected by host terminals (`VSCODE_*`, `CLAUDE_*`, `GIT_ASKPASS`, `SSH_AUTH_SOCK`, …) are stripped by the dispatch layer before agent subprocesses start, regardless of how the service was launched.
- pm2 captures stdout/stderr into `.missioncrew/server.log`.
- Health-check an endpoint that reads the database, such as `/api/overview`; the home page is static HTML and a 200 there proves nothing.
- Before restarting or stopping, make sure no agent is running: no active instance on the *Runtime status* page and no queued or running task in any channel. pm2 gives the service up to 30 seconds to shut down gracefully and end the resident CLI sessions one by one.

## Supported local agents

*Re-detect* probes `PATH` for each tool in the table below (except pi, see below) and keeps one registry record per tool; roles are bound to a fixed runtime and model. Three integration styles:

- **Native protocol**: claude, codex and pi each have a dedicated provider that speaks the tool's own structured protocol; session resume, permission replies and reasoning effort are all handled inside the protocol.
- **ACP over stdio**: the CLI runs as a long-lived JSON-RPC server on stdio and the platform answers its permission requests automatically.
- **Print mode**: the prompt is passed through a built-in command template and sessions are resumed with each tool's session/resume flags.

| CLI | adapter | Integration |
|---|---|---|
| `claude` (Claude Code) | `claude_code` | Native bidirectional stream-json; built-in haiku / sonnet / opus / fable aliases |
| `codex` (OpenAI Codex) | `codex` | Native app-server; model list read from the current account |
| `pi` | `pi` | Native RPC (vendored install); executes custom API models |
| `grok` (Grok Build) | `grok_build` | ACP stdio |
| `copilot` (GitHub Copilot CLI) | `copilot` | ACP stdio |
| `kimi` (Kimi CLI) | `kimi` | ACP stdio |
| `kiro-cli` (Kiro) | `kiro` | ACP stdio |
| `qodercli` (Qoder) | `qoder` | ACP stdio |
| `traecli` (Trae) | `trae` | ACP stdio |
| `opencode` | `opencode` | Print mode |
| `cursor-agent` (Cursor) | `cursor` | Print mode |
| `codebuddy` | `codebuddy` | Print mode |

Protocol flows, detection and upgrade mechanics, where model lists come from, and how to add a new tool are described in [docs/runtimes.md](docs/runtimes.md).

### Using API models (via pi)

Besides local CLIs, MissionCrew can talk directly to **OpenAI / Anthropic compatible APIs** — self-hosted inference servers, gateways, third-party hosting — over the OpenAI Chat Completions, OpenAI Responses, Anthropic Messages and Google Generative AI protocols. These models are executed by [pi](https://www.npmjs.com/package/@mariozechner/pi-coding-agent): the platform does not reimplement an agent loop; it reuses pi's tool execution and session management through pi's RPC mode. Setup:

1. **Install pi.** pi is vendored inside the platform data directory; detection only recognises that copy and never a system-wide pi. Node.js/npm is required. Install it once by hand (adjust the prefix if you changed `MISSIONCREW_HOME`):

   ```bash
   npm install --prefix .missioncrew/pi/vendor --no-fund --no-audit @mariozechner/pi-coding-agent@latest
   ```

   Then click **Re-detect** in *Global settings* to register and enable `pi`. Later upgrades use the *Update* button on that page and only touch the vendor directory — never a global `-g` install.
2. **Add a provider.** Under *Global settings → Custom model providers* add an entry: name, protocol, base URL, API key (a literal or a `$ENV_VAR` reference; keys are never sent back to the browser) and the list of model ids.
3. **Bind a role.** After saving, the models appear in the role editor's model list as `provider/model-id`; give the role runtime `pi` and that model.

Provider configuration is stored in `.missioncrew/pi/agent/models.json` (mode 0600) and sessions in `.missioncrew/pi/sessions`; `~/.pi` is never read or written. See the "pi RPC" and "Custom model providers" sections of [docs/runtimes.md](docs/runtimes.md) for details.

## License

Released under the [MIT License](LICENSE).

## Acknowledgements

Inspired by [multica](https://github.com/multica-ai/multica), [hapi](https://github.com/tiann/hapi) and other happy-family apps.

Thanks to the [linux.do](https://linux.do/) community.
