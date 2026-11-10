# Scheduled automation and automatic task handling

Two mechanisms combine into one automation pipeline:

1. **Automation scripts (Automation)**: a script is triggered on a crontab schedule, by a webhook delivery from an external system, or manually, the platform issues a one-time Agent Tool token for that run, and the script uses it to call platform actions such as creating tasks, syncing data sources, posting messages and publishing documents; this is the "scripts update MissionCrew" capability.
2. **Automatic task-handling rules (task auto rules)**: when the labels of a newly created task match a rule's label expression, the platform automatically dispatches the task into a channel for a role to handle, exactly the same way as the manual "Hand off to role" (交给角色处理) action.

Combined, items from external systems can flow through fully automatically: **GitHub/GitCode issues, PRs, alerts → synced into tasks by a script triggered on a schedule or by a webhook → a rule matches → automatically dispatched to a role → the agent handles it in the channel → results written back through `task.brief`/`task.update`**. Humans only follow progress in the channel and step in when needed.

## Automation scripts

- **Creation**: the ＋ in the "Automation" (自动化) section of the Web sidebar, or ask the orchestrator in chat to create one with `automation.save`; before changing a script the orchestrator reads its current text with `automation.list` (with `id`), and afterwards it can test-run it right away with `automation.run`. Fields: `id`, `name`, `description`, `script` (run as an executable when it has a shebang, otherwise passed to bash), `cron` (five-field crontab, empty = no schedule), `enabled` (controls scheduled triggering only), `webhook_source` (id of the global webhook source it subscribes to, empty = no webhook trigger; can be used together with cron, see "Webhook triggers" below), `github_repos` and `github_events` (only when the subscribed source is a GitHub polling source: which events of which repositories it wants; see "GitHub polling sources" below), `actions`, `timeout_seconds`. Manual triggering is always available.
- **Scheduling**: all cron triggers go through the unified scheduling entry point; a script runs only once at a time: while its previous run is still in progress, scheduled and manual triggers are skipped and webhook deliveries are queued; scheduled triggers missed while the service is down are not run afterwards.
- **Permissions**: the script's own `actions` allowlist is the source of truth for the actions available to the script identity; the default is `task.create`, `task.update`, `task.brief`, `message.publish`, `document.publish`, `dashboard.save`, `board_source.save`; the audit actor is recorded as `automation:<project>:<script>`.
- **Run records**: the automation detail page shows each run's trigger type (scheduled / Webhook / manual; webhook runs also record the webhook source, the sending platform, the event name and the delivery id), exit code and stdout/stderr. A non-zero exit code is recorded as a failure. Run records are kept for 7 days with no limit on their number; the latest run of each script is always kept. The orchestrator reads the latest runs with `automation.list` and `id` (only the tail of the output is returned), paging back with `before_id` for older records, to investigate a failing script.

### Execution environment

- **Working directory**: `<data-directory>/automations/<project-id>/<script-short-id>/`; it is kept across runs and can hold state files (for example, the time of the last sync). Before each run, the platform writes the script body to `script` in this directory, so do not use this file name for anything else.
- **Environment variables**: the script inherits the environment of the MissionCrew service process, and `PATH` as well as the login state of CLIs such as `gh` and `gitcode` all come from the user who started the service; when the project has a GitHub identity (the `TOKEN` of the `GITHUB` environment), `gh` and git in the script use that identity instead, without the script passing the token (the mechanism is in "GitHub identity" of [harness workspace boundaries](agent-harness-workspace.md)). The platform additionally injects:

  | Variable | Meaning |
  | --- | --- |
  | `MISSIONCREW_AGENT_TOOL_PYTHON` | The platform's own Python interpreter; use it to run `-m missioncrew.agent_tool` |
  | `MISSIONCREW_AGENT_TOOL_URL` | Root URL of the Agent Tool API |
  | `MISSIONCREW_AGENT_TOKEN_FILE` | The one-time token file of this run (`.agent-tool-token` in the working directory); the CLI reads it itself, so the script does not need to read it and must not print it |
  | `MISSIONCREW_PROJECT_ID` | Project id |
  | `MISSIONCREW_AUTOMATION_ID` | Script id, in the format `<project-id>:<script-short-id>` |
  | `MISSIONCREW_CREDENTIALS_DIR` | Directory of the credentials available to the project (global environments plus the project's own, a project environment replacing a global one of the same name as a whole), one subdirectory per environment and one file per variable; read one inline in the command that needs it, such as `NPM_TOKEN="$(cat "$MISSIONCREW_CREDENTIALS_DIR/NPM/TOKEN")" npm publish`, and never print a secret. stdout/stderr also have secret values replaced with `[credential:<environment>.<variable>]` before they are stored |
  | `MISSIONCREW_TRIGGER` | What triggered this run: `cron`, `webhook` or `manual` (a human clicking "Run now" (立即运行) on the page, or the orchestrator's `automation.run`) |
  | `MISSIONCREW_WEBHOOK_EVENT_FILE` | Webhook runs only: path of this run's event file; the format is described under "Webhook triggers" below |
  | `MISSIONCREW_WEBHOOK_SOURCE` | Webhook runs only: id of the webhook source that received this delivery |
  | `MISSIONCREW_WEBHOOK_PROVIDER` / `MISSIONCREW_WEBHOOK_EVENT` / `MISSIONCREW_WEBHOOK_DELIVERY` | Webhook runs only: the sending platform (`github`, `gitcode`, `gitlab` or empty), the event name (such as `issues` or `Issue Hook`) and the delivery id, so the script can branch on the event directly |

- **Token and timeout**: the token is revoked as soon as the run ends, and also when the run is killed for exceeding its timeout; a script that runs longer than `timeout_seconds` is terminated and recorded as timed out.
- **Calling platform actions**:

  ```bash
  "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call <action> --arguments '<JSON>'

  # For large arguments (e.g. syncing a whole batch of cards), read them from a file or stdin to avoid exceeding the length limit of a single command-line argument
  jq -c '{id: "github", cards: .}' cards.json \
    | "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call board_source.save --arguments-file -
  ```

  The CLI exits with code 0 on success; when the platform returns an error, the connection fails or the arguments are invalid, it exits with a non-zero code and prints the error JSON to stdout. Put `set -euo pipefail` at the top of bash scripts, so that a failure at any step makes the run fail and leaves a record.

## Webhook triggers

Events pushed by an external system (GitHub, GitCode, GitLab or your own service) trigger scripts through a **webhook source**: a script runs as soon as an event arrives instead of waiting for the next scheduled run, and the same script can keep a cron schedule as a periodic fallback. When there is no public URL GitHub can reach, or no permission to configure a webhook on the repository, see "GitHub polling sources" below: the platform queries events with a token instead, and scripts are written the same way.

A webhook source is a platform-wide delivery endpoint that defines only the delivery URL, the secret and whether it is enabled; it does not belong to any project. The external system is configured once per source; scripts in any project subscribe to a source by selecting it, and one source can be shared by several scripts (including scripts of different projects), for example one GitHub organization webhook driving both an issue-sync script and a PR-review script.

- **Dedicated port**: `mc serve` opens a separate port that only receives webhooks, `8322` by default (change it with `--webhook-port` or the environment variable `MISSIONCREW_WEBHOOK_PORT`; `0` turns it off). It listens on the same address as `--host` unless `--webhook-host` or `MISSIONCREW_WEBHOOK_HOST` says otherwise. The port has exactly one route, `POST /hooks/<source-delivery-path>`: every other path returns 404 and every other method 405, and it serves no pages, API or static files. The main service has no authentication, so when an external system needs to reach MissionCrew, forward only the webhook port (port forwarding, a tunnel or a reverse proxy) and never expose the main service port. If the port is already in use, the main service still starts, and both the "Webhook sources" (Webhook 来源) section of Global settings and the "Webhook" section of the automation detail page show why it is not listening.
- **Creating a source**: create one under Global settings → Webhook sources (全局设置 → Webhook 来源) with a source id, a name, a description, a delivery path and whether it accepts deliveries. The delivery path is the part of the delivery URL after `/hooks/`; left empty it equals the source id, and it can also have several segments (at most 4, each made of letters, digits, underscores and hyphens). It is unique across the platform, and after changing it the external system must be updated to the new URL. A disabled source answers every delivery with 404, while the scripts that subscribe to it keep their subscription. A source that scripts still subscribe to cannot be deleted; first pick another source in those scripts or remove their webhook trigger. Sources are maintained only by humans on that page, and the Agent Tool has no action for them.
- **Secret**: generated when the source is created, and viewed, copied or regenerated in the source's edit dialog (regenerating invalidates the old secret immediately, so every external system that delivers to this source must be updated). The secret is shown only in that dialog and is absent from overview data, automation lists and Agent Tool results.
- **Subscribing**: pick the source in the "Webhook source" (Webhook 来源) drop-down of the script editor, or let the orchestrator pass `webhook_source` to `automation.save` (the source id; an empty string removes the webhook trigger). The orchestrator lists the available sources with `automation.list`; when none fits, it should ask a human to create one in Global settings and then subscribe with the source id the human gives. Every delivery to a source triggers all scripts that subscribe to it, and each script gets the same event.
- **Migration of older setups**: webhooks that were turned on directly on a script before have been migrated automatically into sources named after the script (with the project id as a prefix when the source id was already taken); they keep the old delivery URL `/hooks/<project-id>/<script-short-id>` and the old secret, so the external systems need no change.
- **Verification**: every delivery must carry at least one of the credentials below that verifies against the source's secret, otherwise it gets 401. When several are present, any one that verifies is enough: requests sent through a webhook relay often keep the upstream platform's signature header unchanged (for example GitHub's `X-Hub-Signature-256`, which was signed with the upstream platform's secret), and such a non-matching signature does not cause a rejection as long as another credential in the same request verifies.

  | Method | Request header | Used by |
  | --- | --- | --- |
  | HMAC-SHA256 signature (over the raw body) | `X-Hub-Signature-256: sha256=<hex>` | GitHub (set Secret to the secret), Gitea and your own services |
  | HMAC-SHA256 signature | `X-GitCode-Signature-256: sha256=<hex or base64>` | GitCode signing-secret mode |
  | Standard Webhooks signature | `webhook-id`, `webhook-timestamp`, `webhook-signature: v1,<base64>` (`svix-*` are equivalent) | Senders, signing libraries and relays that follow the [Standard Webhooks](https://www.standardwebhooks.com/) specification |
  | Token | `X-GitCode-Token`, `X-Gitlab-Token` or `Authorization: Bearer <secret>` | GitCode password mode, GitLab's Secret token, relays that can add request headers, and your own services |

  A Standard Webhooks signature covers `<webhook-id>.<webhook-timestamp>.<raw body>`, and a timestamp more than 5 minutes away from the platform's clock is rejected (replay protection). Newly generated secrets use the specification's `whsec_<base64>` format, so standard signing libraries can use them as they are; signing uses the base64-decoded bytes. Older secrets without the `whsec_` prefix sign with their raw bytes (the libraries' raw format). GitHub-style signatures and tokens always treat the whole secret string as raw text.

- **Requests and responses**: JSON bodies and GitHub's form format (`payload=<json>`) are supported, up to 25 MiB (larger bodies get 413); a body declared as JSON that fails to parse gets 400. An accepted delivery immediately gets `202 {"ok": true, "run_ids": [<run record id>...], "queued": <number queued>}` and the scripts run in the background; while no script subscribes to the source yet, `run_ids` is empty and nothing runs. GitHub's `ping` event is answered by the platform itself with `200 {"ok": true, "pong": true}` without running any script. If the source does not exist or is disabled, the answer is always 404 without saying why.
- **Queueing**: each script has its own queue. A script that is running when a delivery arrives queues its copy of the event in arrival order, counted in the response's `queued`; at most 20 can wait per script. When one script's queue is full, only its copy is dropped (and audited), the response carries a `dropped` count and the other scripts run as usual; when the queues of all subscribing scripts are full, 503 is returned so the sender retries later. When the current run ends the next one starts, using the latest definitions: if meanwhile the script is deleted, switches to another source or drops its webhook trigger, or the source is disabled or deleted, the queued events are dropped and audited. The queue lives only in memory and is lost when the service restarts, so a sync that must not miss events needs a periodic full sync on cron as a fallback.
- **Event file**: in a webhook run the event is written to `.webhook-event.json` in the working directory (the path is `MISSIONCREW_WEBHOOK_EVENT_FILE`) and deleted when the run ends:

  ```json
  {
    "source": "github-org",
    "provider": "github",
    "event": "issues",
    "delivery": "<delivery id>",
    "received_at": 1760000000.0,
    "content_type": "application/json",
    "headers": {"user-agent": "GitHub-Hookshot/...", "x-github-event": "issues"},
    "payload": {"action": "opened", "issue": {"number": 42}},
    "body": null
  }
  ```

  `source` is the id of the webhook source that received the delivery; `provider` is the sending platform, recognized from the event-name header (`X-GitHub-Event`, `X-GitCode-Event`, `X-Gitlab-Event`) and is an empty string when none is present, in which case `delivery` takes the Standard Webhooks `webhook-id`; `headers` holds the request headers with lowercase keys, minus the secret-bearing `Authorization`, `X-GitCode-Token` and `X-Gitlab-Token`; `payload` is the parsed JSON, or `null` when the body is not JSON, in which case the raw text is in `body`.
- **Delivery history**: every event a source receives is recorded, kept for 7 days and at most 1000 per source. Every delivery that passes verification is recorded, including those received before any script subscribed and GitHub's `ping`; rejected deliveries (failed verification) are not recorded, and their reason is in the audit log. Each record has the time it was received, the sending platform, the event name, the action (`payload.action`, or `object_attributes.action` for GitLab), the repository (`payload.repository.full_name`, or `project.path_with_namespace` for GitLab), the delivery id, how it was verified, and which scripts it was handed to with their results (the run record id, queued, or dropped because the queue was full; empty means no script received it). The full event is stored too and is identical to the event file a script reads; for events over 256 KB only the request headers and the summary are stored, and saved credentials that appear in the full text are likewise replaced with `[credential:<name>]`. Humans read it through "Records" (记录) on each row of Global settings → Webhook sources (全局设置 → Webhook 来源), where they can filter by event, action and repository and click a row for the full text; the "Webhook" section of the automation detail page has the same entry. The orchestrator queries the same records with `webhook.history` (parameter `source`, optionally `event`, `action`, `repository`, `limit`, `before_id`; with `id` it returns the full event of that delivery as `event_file`), to see which events a source has actually delivered before writing a script's filter, and to investigate when a script did not run as expected.
- **One-shot subscriptions**: besides automation scripts subscribing to a source for the long term, a chat role can wait for a single event with `webhook_source` of `callback.create`: the first matching event the source receives after registration (judged by event name, action, repository and an optional filter script) brings that role back to continue in the channel where it registered, and it also comes back when the timeout passes without one. This suits waits such as "continue with the next step once my PR is merged", without creating a long-running script for it. The parameters and the filter script contract are in [Agent Tool API](agent-tool-api.md).
- **Filtering is up to the script**: the platform does not filter by event type, repository or action, and every script on a shared source receives all of that source's deliveries; the script reads the event and simply `exit 0`s for deliveries it does not care about. Such runs still leave a record, which helps to see which events arrived.

Example: sync right away when an issue of `acme/webshop` is opened, closed or reopened, ignore other events (including events for other repositories delivered to the same source), and fall back to cron every 30 minutes. Note that `board_source.save` is a whole-set sync, so a webhook run must also fetch the full list before saving; saving only the single issue from the event would delete all the other tasks.

```bash
#!/usr/bin/env bash
set -euo pipefail

if [ "$MISSIONCREW_TRIGGER" = webhook ]; then
  [ "$MISSIONCREW_WEBHOOK_EVENT" = issues ] || { echo "ignoring event $MISSIONCREW_WEBHOOK_EVENT"; exit 0; }
  repo=$(jq -r '.payload.repository.full_name // ""' "$MISSIONCREW_WEBHOOK_EVENT_FILE")
  [ "$repo" = acme/webshop ] || { echo "ignoring repository $repo"; exit 0; }
  action=$(jq -r '.payload.action' "$MISSIONCREW_WEBHOOK_EVENT_FILE")
  case "$action" in
    opened|closed|reopened) ;;
    *) echo "ignoring issues.$action"; exit 0 ;;
  esac
fi

# Scheduled, manual and matching webhook runs share the same full sync; see the end-to-end example below
```

## GitHub polling sources

An external webhook needs a public URL that GitHub can reach, and admin rights on the repository or organization to configure it. When all you have is a personal access token (PAT), use a **GitHub polling source** instead: it is another type of webhook source (`kind: github`) whose deliveries are not sent by an external system but generated by the platform, which queries GitHub for new events on a schedule with the GitHub identity (the `TOKEN` of the `GITHUB` environment). Subscribing, fan-out, queueing and the shape of the event a script receives are the same as for external webhooks, so one script can handle both types of source without telling them apart.

The platform is **read-only** towards GitHub: it only calls query APIs, never registers webhooks on repositories or organizations, never marks notifications as read, and changes no setting on GitHub.

The division of work is "a human creates the entry, scripts subscribe to repositories":

- **A human creates the source, and it is not tied to repositories.** Save the GitHub token first, that is the `TOKEN` of the `GITHUB` environment (see "GitHub identity" in [harness workspace boundaries](agent-harness-workspace.md)), then fill in Global settings → Webhook sources → "New GitHub polling source" (全局设置 → Webhook 来源 → ＋ 新建 GitHub 轮询来源):

  | Field | Meaning |
  | --- | --- |
  | Token to use (`project_id`) | The `TOKEN` of the global `GITHUB` environment, or the one in effect for a project (the project's own `GITHUB` environment when it has one, otherwise the global one) |
  | Polling interval (`interval_seconds`) | How often GitHub is queried, 60 to 86400 seconds, 60 by default; it applies to every repository and to the notifications of this source |
  | Account notifications (`notifications`) | When on, adds a notification stream: the notification inbox of the account the token belongs to |
  | Participating only (`participating`) | The notification stream only contains notifications the account directly participates in or is mentioned in |

  The type of a source cannot be changed after it is created. Like other sources it is maintained only by humans on the page, and one source per identity is enough.
- **Each subscribing script declares its repositories.** Besides choosing this source with `webhook_source`, a script lists the repositories it wants in `github_repos` (`owner/repo`, at most 50 per script) and can use `github_events` to receive only some event names (empty means all, including `notification` when the source has notifications on). Both fields appear on the script edit page once a GitHub polling source is selected, and the orchestrator passes the parameters of the same names to `automation.save`, so **an agent can subscribe to new repositories by itself** without a human changing the source. The platform polls the union of the repositories of all scripts on the source (at most 200 in total) and hands each repository's events only to the scripts subscribed to it; repositories no script subscribes to are not queried (except a repository a role is waiting on once with `callback.create`, which is queried while the wait lasts).
- **A subscription takes effect immediately**: a repository a script adds gets its baseline within ten seconds of saving, and only events from that moment on are delivered, without waiting for the next polling time; this extra query covers only the new repository and does not make the other repositories exceed the polling interval. When a repository name is wrong or the token cannot access it, saving still succeeds; the reason is shown in the source status, and the orchestrator also sees it in `github_errors` of that script in `automation.list`.
- **The two kinds of event stream**:

  | | Repository event stream | Notification stream |
  | --- | --- | --- |
  | GitHub API | `GET /repos/<owner>/<repo>/events` | `GET /notifications` |
  | What it sees | Whatever happens in the repositories scripts subscribed to, whether or not the account is involved | The account's inbox: being mentioned, assigned or asked for a review, new activity on issues/PRs it created or takes part in, activity in repositories it watches |
  | Who receives it | Scripts subscribed to that repository whose `github_events` does not exclude the event name | Every script on the source whose `github_events` does not exclude `notification` |
  | Usable tokens | Both classic and fine-grained PATs; a private repository needs a token with read access to it | Classic PATs only, with the `notifications` or `repo` scope; fine-grained PATs cannot be used |
  | Event name (`MISSIONCREW_WEBHOOK_EVENT`) | Same as webhooks: `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`, `push`, `create`, `delete`, `release`, `commit_comment`, `discussion`, `fork`, `watch`, `member`, `public`, `gollum` | `notification` |
  | What happened | `payload.action` is the action | `payload.reason` is why the notification was received; the details have to be fetched separately |

- **Payload of a repository event**: `payload` is the event body given by GitHub's Events API, to which the platform adds the fields webhook payloads are commonly filtered by, `repository` (`full_name`, `name`, `html_url` and so on), `sender` (who triggered it, with `login`) and `organization`; the id, type and time of the original event are in `payload.github_event`. It is close to a webhook payload but not identical:
  - A new issue is `issues` with `action: opened`; closing, reopening, labeling and assigning are `closed`, `reopened`, `labeled` and `assigned`. `payload.issue` is the full issue object.
  - A new PR is `pull_request` with `action: opened`. **A merge is `action: merged`** (in a webhook it is `closed` with `pull_request.merged: true`), and closing without merging is `closed`. `payload.pull_request` only has `number`, `url`, `head` and `base`, without title or body; fetch them with `gh api "$(jq -r .payload.pull_request.url "$MISSIONCREW_WEBHOOK_EVENT_FILE")"` when needed.
  - `push` only has `ref`, `head` and `before`, without the list of commits.
- **Payload of a notification**: `payload.reason` is the reason (`mention`, `team_mention`, `assign`, `review_requested`, `author`, `comment`, `state_change`, `subscribed`, `ci_activity` and so on); `payload.subject` is what the notification points at, with `title`, `type` (`Issue`, `PullRequest`, `Release` and so on), the object's API address `url` and the API address of its latest comment `latest_comment_url`; `payload.repository` is the repository it is in; `payload.notification` is the notification's own `id`, `updated_at` and `last_read_at`. Each new activity on the same object is delivered again. The platform's query includes read notifications, so nothing is missed when a human reads one on the web first; the platform never marks notifications as read.
- **Differences from external webhooks**:
  - The first poll of an event stream only records a baseline, and events that already existed are not delivered.
  - Polling progress is stored in the database, so after a service restart the platform fetches the events that happened while it was down. The repository events API keeps only the latest 300 events of each repository, so earlier events cannot be recovered after a long downtime on a very active repository.
  - When the queue of a subscribed script has no room, the remaining events of that stream are left for the next poll instead of being dropped; the source status then shows "events pending" (有事件待投). When several scripts subscribe to the same repository, one of them being full makes the later events of that repository wait together, to keep their order.
  - Repositories no script subscribes to are not queried, and events from that time are not kept for later subscribers; subscribing again after unsubscribing starts from a new baseline.
  - In the event file `transport` is `poll` (`webhook` for external deliveries); `delivery` is `poll-<GitHub event id>` or `poll-notification-<notification id>-<updated time>`, and the same event is never delivered twice. In the script environment `MISSIONCREW_TRIGGER` is still `webhook` and `MISSIONCREW_WEBHOOK_PROVIDER` is `github`.
- **Latency and quota**: the polling interval is configured on the source and defaults to 60 seconds, which is also the minimum GitHub requires (the `X-Poll-Interval` response header; when GitHub asks for longer the platform follows it). The repository events API itself is not real-time: in our measurements events usually became visible within a minute of happening, and GitHub's documentation says the delay can reach several hours at busy times; the notifications API has no such delay. Use an external webhook when you need a response within seconds. Each subscribed repository costs one request per round; when there are no new events GitHub answers 304, which does not consume the call quota (5000 per hour per token); when rate limiting is triggered the platform postpones the next query until the time GitHub gives.
- **Status and troubleshooting**: the source list and the edit dialog show the polling interval, the subscribed repositories, the time of the last query, the total number of deliveries and the reason for a failure, and "Check now" (立即检查) queries once immediately. Common causes: the `GITHUB` environment has no `TOKEN`; the token is no longer valid (401); the repository does not exist or the token cannot access it (404, which GitHub also returns for private repositories the token cannot access); the notification stream is used with a fine-grained PAT. A failing stream does not affect the other streams; one audit entry (`github_poll_failed`) is written whenever the failure reason changes.
- **What the platform does not do**: it does not register webhooks on GitHub for you, and it does not do finer monitoring such as comparing lists item by item. What the polling source cannot give (fields the Events API omits, a more authoritative issue/PR state, a shorter check cycle) is queried and compared by the script or agent itself with `gh api`: write a cron script that stores what it saw last time in its working directory and compares after each query. `gh` in a script is already the project's GitHub identity.

Example: hand new issues of `acme/webshop` to dev in a channel, and tell the orchestrator when a PR is merged. The script subscribes to the source `github` with `github_repos` set to `["acme/webshop"]` and `github_events` set to `["issues", "pull_request"]`, and then filters by action (the script's `actions` must include `message.publish`, which is there by default):

```bash
#!/usr/bin/env bash
set -euo pipefail

[ "$MISSIONCREW_TRIGGER" = webhook ] || exit 0
action=$(jq -r '.payload.action // ""' "$MISSIONCREW_WEBHOOK_EVENT_FILE")
case "$MISSIONCREW_WEBHOOK_EVENT.$action" in
  issues.opened)
    jq -c '{channel: "general", mentions: ["dev"],
            content: ("新 Issue #\(.payload.issue.number) \(.payload.issue.title)\n\(.payload.issue.html_url)\n请分析并给出处理建议")}' \
      "$MISSIONCREW_WEBHOOK_EVENT_FILE" \
      | "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call message.publish --arguments-file -
    ;;
  pull_request.merged)
    # The PR in the event only has its number and URL; get the title with gh (gh in the script is already the project's GitHub identity)
    title=$(gh api "$(jq -r '.payload.pull_request.url' "$MISSIONCREW_WEBHOOK_EVENT_FILE")" --jq .title)
    jq -c --arg title "$title" '{channel: "general", mentions: ["lead"],
            content: ("PR #\(.payload.number) 已合并：\($title)")}' "$MISSIONCREW_WEBHOOK_EVENT_FILE" \
      | "$MISSIONCREW_AGENT_TOOL_PYTHON" -m missioncrew.agent_tool call message.publish --arguments-file -
    ;;
  *) echo "ignoring $MISSIONCREW_WEBHOOK_EVENT.$action" ;;
esac
```

## Syncing external lists into tasks (data sources)

`board_source.save` creates or updates a custom task data source: `columns` declares the status values (order and color), and `cards` are **synced as a whole** by id into this source's tasks, which adds new entries, updates existing ones, and deletes entries not present this time.

> **Tasks that are not in this call's `cards` are permanently deleted together with all of their status briefs, and they do not go to the recycle bin.** So when fetching the external list fails, the script must exit with a non-zero code and must not save an empty or partial list; paginated fetches must fetch every page; and the rule that generates card `id`s must stay the same across runs, otherwise every run deletes the old tasks and creates a new copy.

The whole sync only manages tasks with an `external_id`: those created from a card `id`, and those recorded with `task.create` carrying `external_id`. To add, change or remove a single task of a data source, use `task.create` (with `source`), `task.update` and `task.delete` just as for built-in tasks. When you record an item of the external list on its own (for example, adding an issue without waiting for the next sync), pass `external_id` (the id the cards will use) and `url`: at the next whole sync the card with that id updates this task instead of creating another copy, and once a later sync's `cards` no longer contain that id, the task is deleted. `external_id` is unique within a data source, and recording it twice returns `already_exists`. A task without `external_id` is neither updated by the whole sync nor deleted for being absent from `cards`.

### Card fields

| Field | Format | Description |
| --- | --- | --- |
| `id` | String, required | Stable identifier of the external item, e.g. `#231`; sync matches existing tasks by it |
| `title` | String | Required when creating a task; may be omitted when updating |
| `summary` | String | Short description |
| `status` | String | Status text, stored as a `status: <text>` label; see the merge rules below |
| `labels` | Array of strings | Plain labels (e.g. `bug`) or `property: value` property labels (e.g. `owner: 张三`) |
| `properties` | Object | `{property: value \| [value...] \| null}`; sets an individual property, and `null` clears it |
| `url` | String | Original link; agents see it in the `url` field of the task snapshot and in the automatic dispatch message |
| `meta` | Array of strings | Note text shown on the card, e.g. `["@alice", "3 条评论"]` |
| `updated_at` | Unix seconds or an ISO 8601 string with a time zone | The item's update time in the external system, stored as the task's `external_updated_at`; boards show and sort by it first. Omitted keeps the existing value; it is separate from the system update time MissionCrew records itself |

`columns` is an array of `[{value, color}]`: if it is omitted when a data source is created, the defaults are the four statuses `待处理` (to do), `处理中` (in progress), `已阻塞` (blocked) and `已完成` (done); if it is omitted on update, the existing status values are kept, and if it is passed, it replaces them entirely. The status text itself is not validated, so you can write values outside `columns`. The page's independent status-column view keeps these undeclared statuses and puts tasks without a status in an "Unset" column; custom expression columns still show only tasks matching each column's expression.

### Merge rules

Existing tasks are merged on the principle of "**change only what the card carries, keep what it omits**", so status that agents or humans have advanced locally and properties they have added are not wiped out by the next sync:

- `title`, `summary`, `url`, `meta`: overwritten only when present; a `null` value is the same as absent.
- `status`: set only when present; when absent, the local status is kept, and newly created tasks land in the first status value. Scripts should include it only when the external source has a definite conclusion, for example `已完成` when an issue is closed.
- Plain labels in `labels` (such as `bug`): as long as the card carries the `labels` field, the task's plain labels are replaced as a whole group by the card's, and labels removed in the external source are removed as well; when `labels` is an empty array or contains only `property: value` entries, all plain labels are cleared. If you do not want the sync to manage plain labels, do not include `labels`; for markers that must persist locally in the long term, use the `property: value` form and avoid the property names the script sends.
- `property: value` entries in `labels` and `properties` (`{property: value | [value...] | null}`): only properties with the same name are replaced, `null` clears, and properties not mentioned are left untouched. When the same property appears in more than one of them, the `status` field takes precedence over `properties`, and `properties` takes precedence over `labels`.
- Tasks whose content (including the external update time) has not changed are not written to the database, so the system `updated_at` stays the same and does not invalidate the `task.update` snapshot an agent holds.

In other words, the fields the script includes are owned by the external source and are set back to the external values on every sync; the agent handling a task should record progress in the status, `property: value` labels and status briefs, and as long as the script does not include those fields, they are kept.

Synced items are ordinary tasks: they can be opened in the detail view, receive status briefs, be bound to channels and be dispatched; **newly synced tasks immediately go through the project's automatic handling rules**. Synced tasks are not bound to any channel before they are dispatched. A synced task removed with `task.delete` is created again by the next sync as long as the external item still exists; restoring the old task from the recycle bin then returns a conflict. To view them, create a taskboard dashboard bound to the source with `dashboard.save`. Shared configuration still uses `filters` to define custom expression columns and `group_by` to define property grouping; personal filters and display preferences on the page work independently as described below.

### Task board filtering and columns

Built-in and custom task boards use the same interactions, with a choice of unarchived tasks, archived tasks or all tasks:

- **Search**: matches text in titles, summaries, task IDs, external IDs, labels and card notes (`meta`) without case sensitivity; task bodies are not searched.
- **Multi-select filters**: multiple values of the same property use OR, while different properties use AND; a property can also match "Unset". Multiple plain labels use OR and combine with property filters. For example, selecting `owner: alice`, `owner: bob` and `priority: high` means the owner is alice or bob, and the priority is high. Active conditions stay visible on the board and can be removed individually or cleared together.
- **Advanced label expression**: use an expression for combinations involving `!`, parentheses and other operators. It is a separate AND condition alongside search and multi-select filters, and does not rewrite shared expression columns.
- **Columns**: a dropdown offers status, properties present in the data (such as `owner` or `priority`) and custom expression columns. On first opening, boards with an existing `group_by` inherit that property; boards with extra or custom expression columns keep their expression view; standard new boards group by status. Personal preferences saved in this browser take precedence after that. Property grouping only changes how tasks are organized; it does not silently limit them to the union of shared expression columns. A task with multiple values for the grouping property can appear in each corresponding column.
- **Editing expression columns**: when custom expression columns are selected, a ✎ button appears next to the dropdown and opens the editor for the shared expression columns. In the editor you can change column titles and expressions, add or remove columns, and reorder them by dragging ⠿ or with ↑ ↓. The right side previews, live, which tasks the column being edited matches within the current display scope (personal filters excluded) and counts how many tasks fall into no column, with a reference of the expression syntax below. Changes are submitted together with Save (保存). On the built-in board the four status columns cannot be removed or rewritten but can be reordered; columns bound to a ⚡ automatic handling rule cannot change their expression or be removed.
- **Display**: cards within columns default to most recently updated first; other sorting options are least recently updated, most recent system update (系统最近更新), most recently created and title. "Updated" uses the external system's update time (a sync card's `updated_at` or `external_updated_at` set by an agent) and falls back to MissionCrew's system update time, which the card then labels "system updated" (系统更新); "most recent system update" uses only the time MissionCrew records itself. The task detail page lists the update time, the system update time and the creation time; the system times are read-only. Hide empty columns, collapse individual columns or collapse/expand all. A collapsed column keeps its name and count so it is easy to reopen.

Archive scope, search, filters, grouping, sorting, empty-column visibility and collapsed columns are **personal view preferences**, stored in the current browser separately for each project, board and data source and restored when the page is reopened. They do not write back to shared project or board configuration. Editing expression columns through the ✎ next to the column dropdown and configuring automatic handling rules through the column's ⚡ still update shared configuration; these serve a different purpose from temporary filters.

These interactions draw on [Linear's display options](https://linear.app/docs/display-options), [GitHub Projects' filtering semantics](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/filtering-projects) and [Trello's list collapsing](https://support.atlassian.com/trello/docs/collapse-or-expand-a-list/).

#### Read-only view parameters

The built-in board's `GET /api/projects/<project>/builtin_board/data` and custom task boards' `GET /api/projects/<project>/boards/<board-id>/data` accept the same parameters:

| Parameter | Behavior |
| --- | --- |
| `scope` | `active` (default), `archived` or `all`; selects the current archive scope |
| `view_group_by` | An explicitly nonempty value groups independently by that property, such as `status` or `owner`; candidate tasks come from the data source's current `scope` without first taking the union of shared `filters`. An explicit empty value (`view_group_by=`) selects the saved expression columns. Omitting this parameter preserves the old API behavior: use the shared `group_by`, and when it is nonempty, first take the union of `filters` before grouping |
| `filter_query` | An additional advanced label expression, combined with the following quick filters using AND |
| `search` | The plain text search described above, combined with the other conditions using AND |
| `filter_label` (repeatable) | An exact label value such as `owner: alice`; values of the same property use OR, different properties use AND, and all plain labels form a separate dimension whose selected values use OR |
| `missing_property` (repeatable) | Matches tasks missing a property, such as `owner`; combines with selected values of that property using OR, then with other filter dimensions using AND |

These parameters do not persist changes to the project or dashboard. Label matching uses the platform's label normalization and ignores case; label values are not parsed as expressions, and complex expressions belong in `filter_query`. Columns and selectable labels come from the current archive scope before quick filters are applied, so filtering does not remove other available choices. The response's `counts` always cover the entire data source for each archive scope. `matched_count` counts distinct tasks matching the quick filters, including tasks that match no column expression in expression mode. Expression columns can overlap or leave tasks uncovered, so adding their counts does not give the total number of tasks.

## Automatic task-handling rules

Rules are stored on the project; each one = **label expression + handling instructions + optional @role mentions + enable/disable switch**. A rule applies to tasks from every data source in the project and does not belong to any particular board.

- **Where to configure**: the sidebar's "面板 → 自动处理规则" (automatic handling rules) page, placed under the built-in task board and shown only once the project has rules, lists every rule numbered in order; it only creates and edits rules and offers no filtering or grouping. The ⚡ icon in the column header of any task board (built-in or custom), the board's "显示选项 → 新建自动处理规则" (display options → new rule) entry and the ⚡ on a dashboard's taskboard widget open the same rule editor; opened from a column header, the condition is prefilled with that column's expression, and an existing rule with the same condition is edited directly. Rules can only be configured by a human on the page, and the Agent Tool has no corresponding action; when an agent is asked to set up a rule, it should tell the human to use one of these entries, and give the suggested expression and handling instructions.
- **Rule editor**: the condition switches between a visual mode and an expression mode. The visual mode edits one condition per row, all of which must match ("全部") or any of which may match ("任一"); each row picks a plain label (contains / does not contain), the status or a property (is / is not / set / unset), with suggestions taken from labels that already exist in the project's data sources. Conditions with nested parentheses or a mix of AND and OR can only be edited in expression mode. The preview on the right lists the currently unarchived tasks from all of the project's data sources that match the condition and marks where each would go: handled by this rule, not dispatched because it is already done, or handled first by an earlier rule. The preview only illustrates the matching scope; the rule itself applies only to tasks created afterwards. Each condition can have only one rule; conditions are saved in a canonical form, and board columns and rules are matched by that form, so differences in spacing or `&&` versus `&` do not matter.
- **When it fires**: when a task is created (by a human on the page, by an agent with `task.create`, or by a data source sync creating it), it is dispatched by the first enabled rule in list order whose expression matches its labels; new tasks whose status is already `已完成` are not dispatched. If the labels of an existing task are changed later, no dispatch is triggered even if they match a rule. When `task.create` triggers a dispatch, the result includes `auto_dispatch` (`rule_query`, `sent`).
- **Dispatch behavior**: a message containing the task details (including the original link) is posted in the task's first bound channel only, or in the project's `general` channel if the task has no bound channel, and after dispatch the task status changes to `处理中` (a custom data source that does not declare the status value `处理中` keeps the current status, respecting that source's declared workflow); if a single role is mentioned, that role executes directly, and if multiple roles or no role is mentioned, the task is handed to the project orchestrator to schedule; from then on, chat collaboration is reused entirely. The agent that receives this message can see that it comes from an automatic task-handling rule.
- **Expression syntax**: whole-label matching (`bug`, `owner: 张三`) and `property: *` existence matching, combined with `&`, `|`, `!` and parentheses. A term without a colon matches both a plain label of that name and any label using it as the property name: `owner` is equivalent to `owner | owner: *`. Examples: `bug & !status: 已完成` (unfinished bugs), `!owner` or `!owner: *` (no owner yet; the former also excludes tasks with a plain `owner` label).

## End-to-end example: automatically handling GitCode/GitHub issues

1. **Sync script**: create an automation script with cron `*/30 * * * *` (to sync as soon as an issue changes, also subscribe it to a webhook source that receives the repository's events and filter events as described above) that uses the `gitcode`/`gh` CLI to fetch the repository's issue list, converts it into cards, and calls `board_source.save`. Use the issue number as the card id, write the issue's labels and assignees as `labels` (e.g. `bug`, `owner: xxx`), and write the link as `url`. Include `status: 已完成` only for closed issues and no `status` for open issues, so that the handling progress written back by agents is always kept.

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   # If gh fails, set -e makes the script exit non-zero, so a partial list is never synced
   LIMIT=1000
   gh issue list --repo acme/webshop --state all --limit "$LIMIT" \
     --json number,title,body,state,labels,url > issues.json
   # Reaching the limit means the list may be incomplete, so fail and exit as well
   [ "$(jq length issues.json)" -lt "$LIMIT" ] || { echo "More than $LIMIT issues, increase LIMIT" >&2; exit 1; }

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

2. **Task board**: use `dashboard.save` to create a taskboard bound to this source, with columns by status or by owner.
3. **Rule**: a human configures it through the ⚡ in the task board column header, for example with the expression `bug & status: 待处理` and the handling instructions "@dev please analyze and fix this, then append a status brief when done". It is recommended to run the script manually once to sync in the existing issues before enabling the rule, so that the whole backlog is not dispatched at once.
4. **Automatic flow**: a new issue is synced into a task → it matches the rule → it is automatically dispatched to dev → dev analyzes and fixes it in the channel (and can also write the conclusion back to the external system with the `gh`/`gitcode` CLI) → dev updates the task status with `task.brief`/`task.update`; as long as the sync script does not include `status`, this status is kept across later syncs until the issue is closed and synced as `已完成`. With this approach, a reopened issue does not automatically go back to `待处理`; when needed, a human or an agent changes the status, or the script includes a status for reopened issues.

PR reviews and alert on-call work the same way: any data that can be expressed as "external list → cards" can enter this pipeline, and the handling instructions and the choice of roles determine how it is processed.

## Related documents

- Action list, arguments and permission model: [agent-tool-api.md](agent-tool-api.md)
- Agent workspace and task snapshot boundaries: [agent-harness-workspace.md](agent-harness-workspace.md)
