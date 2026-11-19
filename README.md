# LabOps

[中文说明](README.zh-CN.md)

LabOps is a local control plane for running AI coding agents (Codex and Claude) on your own Git repositories. Each change goes through a task, a round in its own worktree, an independent verification run, a review and a gate, and lands on `main` only after you approve a fast-forward in a terminal. Around that loop it tracks training runs, queues GPU jobs, pushes alerts to your phone, and passes messages between agent sessions that are already open in the Claude and Codex desktop apps.

State lives in one SQLite database. Git keeps every submitted commit under `refs/labops/*`, and an operation journal repairs Git and SQLite after a crash.

## Limitations

- Windows only. LabOps uses PowerShell 7 (`pwsh`), Windows named pipes and Windows process queries. It has not been run on macOS or Linux.
- Single user. Approvals are typed in a terminal (TTY) to prevent mistakes, not attacks. Any agent running as the same Windows user can read the database and the repositories.
- Desktop messaging to Codex uses the Codex desktop app's private IPC (`\\.\pipe\codex-ipc`). It was tested with Codex desktop 26.1111 and may break when the app updates. Messaging to Claude uses the documented [cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging) inbox.
- Phone notifications ship with one sender, which calls the OpenClaw CLI. Other channels need a sender of your own (see [Notifications](#notifications)).

## Install

Requirements: Python 3.11 or later, Git on `PATH`, PowerShell 7 for queue launches.

```powershell
git clone <this-repo> labops
cd labops
python -m pip install -e ".[mcp]"   # the core CLI has no dependencies; [mcp] adds the MCP server
labops init
```

State goes to `%USERPROFILE%\.labops` unless `LABOPS_HOME` points elsewhere. Every command prints JSON; errors carry `error`, `message` and `detail`.

To run from a checkout without installing, set `$env:PYTHONPATH = "<labops>\src"` and call `python -m labops.cli` instead of `labops`.

## The task loop

A repository is registered once. A task freezes an acceptance profile (the exact commands to run, a timeout, the paths a change may touch) and a policy of frozen files. Implementers work in rounds; each round gets its own worktree and branch.

```powershell
labops repo add --repo-id toy --path C:\work\toy --trusted-for-exec
labops policy set --repo-id toy --file examples\policy.toy.json
labops task create --repo-id toy --task-id T20260101-demo --goal "Fix X" --profile examples\profile.toy.json
labops round open --task-id T20260101-demo --actor codex --role implement
labops wt create --round T20260101-demo/r1
# ... the agent edits files in the worktree ...
labops snapshot --round T20260101-demo/r1 --actor codex --message "Fix X" --files src\x.py
labops submit --round T20260101-demo/r1 --actor codex --commit <sha> --request-id r1-submit
labops verify --round T20260101-demo/r1          # runs the frozen commands in a separate worktree
labops review open --round T20260101-demo/r1 --reviewer opus
labops review submit --review <review-id> --actor opus --verdict PASS --request-id r1-review
labops gate check --task-id T20260101-demo
labops integrate propose --task-id T20260101-demo
labops integrate approve --integration <ig-id>   # asks you to type the tree prefix
```

The gate checks that the latest round is verified and reviewed, that no review is blocking, that the change stays inside the profile's paths, and that frozen files are untouched unless you approved a decision for them. When the same actor wrote and reviewed a fix, you also run `labops approve self-review --task-id T`. If `main` moved after the task started, the agent runs `sync_main` in a fix round and proposes again.

`examples/profile.toy.json` and `examples/policy.toy.json` show the file formats. `python scripts\smoke_m0a.py` runs the whole loop on a throwaway repository.

## Connecting agents over MCP

`labops.mcp_server` is a stdio MCP server. Each tool call runs in a short-lived worker that loads the current code from `src`, so code changes take effect without restarting the client; changes to tool names or parameters still need a restart.

The `LABOPS_ACTOR` variable decides which tools a client sees. It records who acted; it is not authentication.

| Actor | Intended client | Can |
| --- | --- | --- |
| `codex` | Codex | open implement rounds, snapshot, submit, sync `main`, register queue jobs, adopt GPU processes, message a Claude session, notify you |
| `opus` | Claude | everything `codex` can except `message_claude`, plus open fix rounds, submit reviews, propose integration, send to any desktop session, read progress |
| `secretary` | a chat assistant (for example an OpenClaw agent) | read progress and history, record queue intents and choices, forward a chat message to a desktop session |

No MCP tool can approve or merge. Approvals and `integrate approve` exist only in the CLI and require a TTY.

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.labops]
command = '<labops>\.venv\Scripts\python.exe'
args = ['-m', 'labops.mcp_server']
cwd = '<labops>'
env = { LABOPS_ACTOR = 'codex', LABOPS_HOME = '<home>\.labops', PYTHONPATH = '<labops>\src' }
tool_timeout_sec = 60
```

Claude Code (`claude mcp add` or `.mcp.json`):

```json
{
  "mcpServers": {
    "labops": {
      "command": "<labops>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "labops.mcp_server"],
      "cwd": "<labops>",
      "env": { "LABOPS_ACTOR": "opus", "LABOPS_HOME": "<home>\\.labops", "PYTHONPATH": "<labops>\\src" }
    }
  }
}
```

An agent that starts cold can call `next_task`, which returns the task it should work on and what to do next; `get_task_context` returns the task brief and the shared handoff rules.

Optional Codex hooks record session events into a spool without storing message content: point the `Stop` and `SessionEnd` hooks at `<labops>\labops.cmd hook ingest --client codex`.

## Notifications

Events (submitted, verified, review blocking, awaiting approval, run failed and so on) go into an outbox table. `labops notify run` scans for new events, folds them and sends them. Schedule it every minute with Windows Task Scheduler or an OpenClaw cron job.

- Urgent kinds are sent on the next run; several urgent alerts in one run become one message. Other kinds wait for a digest window (180 minutes by default). Configure with `labops notify config --urgent-kinds a,b --digest-minutes N`.
- `labops notify mute KIND` drops a kind. `repo set --alert-detail off` keeps only the first line of each alert for that repository.
- With `labops notify config --llm-rewrite on` and `DEEPSEEK_API_KEY` set, alerts are rewritten by DeepSeek into shorter prose. A rewrite that drops a task ID, SHA, number or command is discarded and the template is sent instead. Confidential repositories are never rewritten.
- If `LABOPS_NTFY_TOPIC` is set, the first line of an alert that could not be delivered goes to [ntfy](https://ntfy.sh) as a fallback.

Senders are pluggable. A sender is any callable `sender(text) -> (status, error)` where `status` is `sent`, `failed`, `unknown` or `token_expired`. An optional attribute `markdown_style = "wechat"` turns on WeChat-style Markdown conversion. Register one with `labops.notify.register_sender(name, factory)` and select it with `labops notify config --sender NAME` (stored in the database) or `LABOPS_NOTIFY_SENDER`.

The built-in `openclaw` sender runs `openclaw message send --channel <channel> --account <account> --target <target>`. Set `LABOPS_NOTIFY_ACCOUNT` and `LABOPS_NOTIFY_TARGET`, and `LABOPS_NOTIFY_CHANNEL` (default `openclaw-weixin`) to use another OpenClaw channel. Only the WeChat channel has been tested. Unknown or unparseable results are recorded as `UNKNOWN` and not resent automatically.

`labops notify test-send` sends one fixed test message through the selected sender.

## Training runs and the GPU queue

`notify run` also reads background-run state (default `%USERPROFILE%\.codex\background-state`, override with `LABOPS_BG_STATE_ROOT`) and tracks runs that belong to registered repositories: progress lines from the log tail, failures, and stalls (60 minutes without a log or watched-file update). Processes started outside that launcher can be registered with `labops runs adopt` or the `adopt_process` MCP tool; LabOps stores the PID together with its start time, so a reused PID is not mistaken for the old run.

`labops repo set --progress-rule '<json>'` extracts progress from JSON or text files inside the repository; `labops repo progress-preview` shows what each rule matches.

The GPU queue separates intent from execution. A chat assistant records an intent (`queue_intent`); Codex or Claude registers an existing `.ps1` entrypoint in the repository (`queue_register`), whose SHA-256 is checked again before launch. When the GPU is busy, LabOps asks once whether to wait for you or start automatically. At most one job is `LAUNCHING` at a time, enforced by a partial unique index.

```powershell
labops queue config --entry-root C:\work --launcher C:\tools\Start-BackgroundRun.ps1
labops queue config            # show the current settings
```

Without `--entry-root`, entrypoints only need to live inside their repository. Without `--launcher`, LabOps never starts jobs itself; the queue's `handoff` mode (the default) notifies an agent with a copy-paste command instead. The launcher is called as `pwsh -File <launcher> -ScriptPath <entry> -WorkingDirectory <repo> -RunDirectory <run-dir>`.

## Messaging desktop sessions

LabOps can send a message into a Claude or Codex session that is open in its desktop app and read back the reply, so the conversation stays visible where you work.

| Tool | Who | What |
| --- | --- | --- |
| `desktop_list` | Claude, chat assistant; Codex (Claude sessions only) | list candidate sessions (Claude) or recent threads (Codex) |
| `desktop_send` | Claude | send to a Claude session or Codex thread |
| `desktop_status` | Claude, chat assistant | phase (`not_seen`, `running`, `completed`, `interrupted`, `session_gone`) and the reply |
| `desktop_dispatch` | chat assistant | send a chat message on your behalf, marked as forwarded |
| `message_claude` | Codex | message a Claude session, marked as coming from Codex |
| `notify_user` | Codex, Claude | push a short report to your phone |

CLI equivalents: `labops desktop list|send|status`.

Every message carries a marker `[labops:<request_id>]`. A `request_id` is sent at most once; if the outcome of a send is unclear, the request is marked `UNKNOWN` and LabOps reads the transcript or rollout to find out what happened instead of retrying. A Codex thread must be open in the desktop app to receive messages.

When a message from chat reaches a final state, `notify run` pushes the reply back. Messages between Codex and Claude are also queued as `agent_relay` notifications; with DeepSeek configured they are summarized to 400 characters first. Messages from chat or from Codex arrive with a prefix saying they are not your direct instruction, and Claude treats them as peer messages rather than as your approval.

## Search, history and backups

- `labops search "QUERY"` combines exact matches on task IDs, review IDs, decision IDs and SHA prefixes with trigram and bigram full-text search (Chinese text is split into overlapping bigrams). Build the index with `labops index rebuild --all`.
- `labops history`, `labops events` and `labops context` answer what happened in a task.
- `labops backup run --all` bundles every ref of each repository, including `refs/labops/*`, and rotates old bundles; `labops backup verify-restore` clones a bundle into a temporary directory and compares refs.
- `labops onboard` registers a repository and runs configuration, backup and indexing in one idempotent command.

Run `labops --help` and `labops <command> --help` for the full command list.

## Confidential repositories

`labops repo set --repo-id ID --confidential on` hides goals, findings, paths and full SHAs from the `secretary` actor, keeps the repository out of DeepSeek rewrites, and replaces desktop-message bodies with a placeholder in phone notifications. Turning it off requires typing the repository ID in a terminal.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `LABOPS_HOME` | state directory (default `%USERPROFILE%\.labops`) |
| `LABOPS_ACTOR` | MCP actor: `codex`, `opus` or `secretary` |
| `LABOPS_CODE_ROOT` | source tree loaded by the MCP worker (default: next to the server) |
| `LABOPS_NOTIFY_SENDER` | sender name when none is stored in the database (default `openclaw`) |
| `LABOPS_NOTIFY_ACCOUNT`, `LABOPS_NOTIFY_TARGET`, `LABOPS_NOTIFY_CHANNEL` | OpenClaw sender settings |
| `LABOPS_NOTIFY_TIMEOUT` | send timeout in seconds (default 180) |
| `LABOPS_NTFY_TOPIC` | ntfy fallback topic |
| `DEEPSEEK_API_KEY`, `LABOPS_DS_BASE_URL`, `LABOPS_DS_MODEL` | DeepSeek rewrite and summaries |
| `LABOPS_BG_STATE_ROOT` | background-run state directory |
| `LABOPS_CLAUDE_HOME`, `LABOPS_CODEX_HOME` | Claude and Codex data directories (default `~/.claude`, `~/.codex`) |

## Safety notes

- Verification runs only in repositories registered with `--trusted-for-exec`, and only the argv lists frozen in the profile, without a shell.
- `snapshot` commits exactly the listed files, requires them to match every dirty file, and refuses model weights, data directories, large files and common secret formats.
- Integration is a fast-forward that checks `main` has not moved since the candidate was built.
- Hook events store hashes, IDs and timestamps, not message content.

## Development

```powershell
python -m unittest discover -s tests -t .
python scripts\smoke_wxdispatch.py --offline
```

Tests use temporary repositories and fake transports; none of them touch real desktop sessions, chat accounts or the network. Tests that start the MCP server use `LABOPS_TEST_PYTHON` (default: the current interpreter). CI runs the suite on Windows with Python 3.11 and 3.13.

## License

MIT. See [LICENSE](LICENSE).
