# Scheduled automation and automatic task handling

Two mechanisms combine into one automation pipeline:

1. **Automation scripts (Automation)**: a script is triggered on a crontab schedule, by a webhook delivery from an external system, or manually, the platform issues a one-time Agent Tool token for that run, and the script uses it to call platform actions such as creating tasks, syncing data sources, posting messages and publishing documents; this is the "scripts update MissionCrew" capability.
2. **Automatic task-handling rules (task auto rules)**: when the labels of a newly created task match a rule's label expression, the platform automatically dispatches the task into a channel for a role to handle, exactly the same way as the manual "Hand off to role" (交给角色处理) action.

Combined, items from external systems can flow through fully automatically: **GitHub/GitCode issues, PRs, alerts → synced into tasks by a script triggered on a schedule or by a webhook → a rule matches → automatically dispatched to a role → the agent handles it in the channel → results written back through `task.brief`/`task.update`**. Humans only follow progress in the channel and step in when needed.

## Automation scripts

- **Creation**: the ＋ in the "Automation" (自动化) section of the Web sidebar, or ask the orchestrator in chat to create one with `automation.save`; before changing a script the orchestrator reads its current text with `automation.list` (with `id`), and afterwards it can test-run it right away with `automation.run`. Fields: `id`, `name`, `description`, `script` (run as an executable when it has a shebang, otherwise passed to bash), `cron` (five-field crontab, empty = no schedule), `enabled` (controls scheduled triggering only), `webhook` (whether webhook deliveries are accepted; can be on together with cron, see "Webhook triggers" below), `actions`, `timeout_seconds`. Manual triggering is always available.
- **Scheduling**: all cron triggers go through the unified scheduling entry point; a script runs only once at a time: while its previous run is still in progress, scheduled and manual triggers are skipped and webhook deliveries are queued; scheduled triggers missed while the service is down are not run afterwards.
- **Permissions**: the script's own `actions` allowlist is the source of truth for the actions available to the script identity; the default is `task.create`, `task.update`, `task.brief`, `message.publish`, `document.publish`, `dashboard.save`, `board_source.save`; the audit actor is recorded as `automation:<project>:<script>`.
- **Run records**: the automation detail page shows each run's trigger type (scheduled / Webhook / manual; webhook runs also record the source, event name and delivery id), exit code and stdout/stderr. A non-zero exit code is recorded as a failure. Only the latest 200 run records are kept per script. The orchestrator reads the latest runs with `automation.list` and `id` (only the tail of the output is returned), paging back with `before_id` for older records, to investigate a failing script.

### Execution environment

- **Working directory**: `<data-directory>/automations/<project-id>/<script-short-id>/`; it is kept across runs and can hold state files (for example, the time of the last sync). Before each run, the platform writes the script body to `script` in this directory, so do not use this file name for anything else.
- **Environment variables**: the script inherits the environment of the MissionCrew service process, and `PATH` as well as the login state of CLIs such as `gh` and `gitcode` all come from the user who started the service; the platform additionally injects:

  | Variable | Meaning |
  | --- | --- |
  | `MISSIONCREW_AGENT_TOOL_PYTHON` | The platform's own Python interpreter; use it to run `-m missioncrew.agent_tool` |
  | `MISSIONCREW_AGENT_TOOL_URL` | Root URL of the Agent Tool API |
  | `MISSIONCREW_AGENT_TOKEN_FILE` | The one-time token file of this run (`.agent-tool-token` in the working directory); the CLI reads it itself, so the script does not need to read it and must not print it |
  | `MISSIONCREW_PROJECT_ID` | Project id |
  | `MISSIONCREW_AUTOMATION_ID` | Script id, in the format `<project-id>:<script-short-id>` |
  | `MISSIONCREW_TRIGGER` | What triggered this run: `cron`, `webhook` or `manual` (a human clicking "Run now" (立即运行) on the page, or the orchestrator's `automation.run`) |
  | `MISSIONCREW_WEBHOOK_EVENT_FILE` | Webhook runs only: path of this run's event file; the format is described under "Webhook triggers" below |
  | `MISSIONCREW_WEBHOOK_PROVIDER` / `MISSIONCREW_WEBHOOK_EVENT` / `MISSIONCREW_WEBHOOK_DELIVERY` | Webhook runs only: the source (`github`, `gitcode`, `gitlab` or empty), the event name (such as `issues` or `Issue Hook`) and the delivery id, so the script can branch on the event directly |

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

A script with `webhook` turned on can be triggered by events pushed from an external system (GitHub, GitCode, GitLab or your own service): it runs as soon as an event arrives instead of waiting for the next scheduled run, and the same script can keep a cron schedule as a periodic fallback.

- **Dedicated port**: `mc serve` opens a separate port that only receives webhooks, `8322` by default (change it with `--webhook-port` or the environment variable `MISSIONCREW_WEBHOOK_PORT`; `0` turns it off). It listens on the same address as `--host` unless `--webhook-host` or `MISSIONCREW_WEBHOOK_HOST` says otherwise. The port has exactly one route, `POST /hooks/<project-id>/<script-short-id>`: every other path returns 404 and every other method 405, and it serves no pages, API or static files. The main service has no authentication, so when an external system needs to reach MissionCrew, forward only the webhook port (port forwarding, a tunnel or a reverse proxy) and never expose the main service port. If the port is already in use, the main service still starts, and the "Webhook" section of the automation detail page shows why it is not listening.
- **URL and secret**: once turned on, the "Webhook" section of the automation detail page shows the delivery URL and the secret, which can be copied or regenerated (regenerating invalidates the old secret immediately, so the external system must be updated). The secret is generated the first time the webhook is turned on and kept when it is turned off. It is shown only on that page and is absent from overview data and Agent Tool results: after the orchestrator turns on the webhook with `automation.save`, it should tell a human the delivery path, and the human takes the secret from the page to configure the external system.
- **Verification**: every delivery must carry at least one of the credentials below that verifies against this script's secret, otherwise it gets 401. When several are present, any one that verifies is enough: requests sent through a webhook relay often keep the upstream platform's signature header unchanged (for example GitHub's `X-Hub-Signature-256`, which was signed with the upstream platform's secret), and such a non-matching signature does not cause a rejection as long as another credential in the same request verifies.

  | Method | Request header | Used by |
  | --- | --- | --- |
  | HMAC-SHA256 signature (over the raw body) | `X-Hub-Signature-256: sha256=<hex>` | GitHub (set Secret to the secret), Gitea and your own services |
  | HMAC-SHA256 signature | `X-GitCode-Signature-256: sha256=<hex or base64>` | GitCode signing-secret mode |
  | Standard Webhooks signature | `webhook-id`, `webhook-timestamp`, `webhook-signature: v1,<base64>` (`svix-*` are equivalent) | Senders, signing libraries and relays that follow the [Standard Webhooks](https://www.standardwebhooks.com/) specification |
  | Token | `X-GitCode-Token`, `X-Gitlab-Token` or `Authorization: Bearer <secret>` | GitCode password mode, GitLab's Secret token, relays that can add request headers, and your own services |

  A Standard Webhooks signature covers `<webhook-id>.<webhook-timestamp>.<raw body>`, and a timestamp more than 5 minutes away from the platform's clock is rejected (replay protection). Newly generated secrets use the specification's `whsec_<base64>` format, so standard signing libraries can use them as they are; signing uses the base64-decoded bytes. Older secrets without the `whsec_` prefix sign with their raw bytes (the libraries' raw format). GitHub-style signatures and tokens always treat the whole secret string as raw text.

- **Requests and responses**: JSON bodies and GitHub's form format (`payload=<json>`) are supported, up to 25 MiB (larger bodies get 413); a body declared as JSON that fails to parse gets 400. An accepted delivery immediately gets `202 {"ok": true, "run_id": <run record id>}` and the script runs in the background; GitHub's `ping` event is answered by the platform itself with `200 {"ok": true, "pong": true}` without running the script. If the script does not exist, belongs to another project or has no webhook turned on, the answer is always 404 without saying why.
- **Queueing**: deliveries that arrive while the script is running are queued in arrival order and get `202 {"ok": true, "queued": true}`; at most 20 can wait per script, after which 503 is returned. When the current run ends the next one starts, using the script's latest definition: if the webhook is turned off or the script is deleted meanwhile, the queued events are dropped and audited. The queue lives only in memory and is lost when the service restarts, so a sync that must not miss events needs a periodic full sync on cron as a fallback.
- **Event file**: in a webhook run the event is written to `.webhook-event.json` in the working directory (the path is `MISSIONCREW_WEBHOOK_EVENT_FILE`) and deleted when the run ends:

  ```json
  {
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

  `provider` is recognized from the event-name header (`X-GitHub-Event`, `X-GitCode-Event`, `X-Gitlab-Event`) and is an empty string when none is present, in which case `delivery` takes the Standard Webhooks `webhook-id`; `headers` holds the request headers with lowercase keys, minus the secret-bearing `Authorization`, `X-GitCode-Token` and `X-Gitlab-Token`; `payload` is the parsed JSON, or `null` when the body is not JSON, in which case the raw text is in `body`.
- **Filtering is up to the script**: the platform does not filter by event type, repository or action; the script reads the event and simply `exit 0`s for deliveries it does not care about. Such runs still leave a record, which helps to see which events arrived.

Example: sync right away when an issue is opened, closed or reopened, ignore other events, and fall back to cron every 30 minutes. Note that `board_source.save` is a whole-set sync, so a webhook run must also fetch the full list before saving; saving only the single issue from the event would delete all the other tasks.

```bash
#!/usr/bin/env bash
set -euo pipefail

if [ "$MISSIONCREW_TRIGGER" = webhook ]; then
  [ "$MISSIONCREW_WEBHOOK_EVENT" = issues ] || { echo "ignoring event $MISSIONCREW_WEBHOOK_EVENT"; exit 0; }
  action=$(jq -r '.payload.action' "$MISSIONCREW_WEBHOOK_EVENT_FILE")
  case "$action" in
    opened|closed|reopened) ;;
    *) echo "ignoring issues.$action"; exit 0 ;;
  esac
fi

# Scheduled, manual and matching webhook runs share the same full sync; see the end-to-end example below
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

1. **Sync script**: create an automation script with cron `*/30 * * * *` (to sync as soon as an issue changes, also turn on the webhook and filter events as described above) that uses the `gitcode`/`gh` CLI to fetch the repository's issue list, converts it into cards, and calls `board_source.save`. Use the issue number as the card id, write the issue's labels and assignees as `labels` (e.g. `bug`, `owner: xxx`), and write the link as `url`. Include `status: 已完成` only for closed issues and no `status` for open issues, so that the handling progress written back by agents is always kept.

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
