<div align="center">

# Claude Code Dashboard — Token Usage, Cost & Analytics Monitor

**A self-hosted observability dashboard for [Claude Code](https://claude.com/claude-code).**
Track token usage, API cost, prompts, sessions, tool calls and failures — parsed directly
from the JSONL transcripts Claude Code already writes to your disk.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![SQLite](https://img.shields.io/badge/SQLite-stdlib-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Chart.js](https://img.shields.io/badge/Chart.js-4.4-FF6384?logo=chartdotjs&logoColor=white)](https://www.chartjs.org/)
[![Local first](https://img.shields.io/badge/data-100%25%20local-2ea44f)](#privacy)
[![No build step](https://img.shields.io/badge/build%20step-none-8957e5)](#quick-start-manual-without-the-plugin)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757)](#install-as-a-claude-code-plugin-recommended)

</div>

![Claude Code usage dashboard showing token usage, cost per day, live activity and cost by model](docs/dashboard.png)

---

## Why this exists

Claude Code gives you `/usage` and `/cost` for the current session, but nothing that answers
the questions you actually care about over time:

- **How many tokens am I burning per day, and on which model?**
- **What is my Claude Code usage actually worth in API dollars?**
- **Which projects and sessions are the expensive ones?**
- **Which tools fail most often, and why?**
- **Am I on my Pro/Max subscription right now — or silently paying per token?**

Every answer is already sitting in `~/.claude/projects/**/*.jsonl`. This dashboard parses
those transcripts into SQLite and puts a fast, local UI on top. Nothing leaves your machine.

## Features

- 🔌 **Installs as a Claude Code plugin** — auto-starts on session start, no manual `uvicorn`, no `settings.json` editing
- 📊 **Token usage analytics** — input, output, cache-read and cache-write tokens, per day, per model, per project
- 💰 **Cost tracking** — an editable per-model pricing table with correct cache-read (×0.1) and cache-write (×1.25 / ×2.0) multipliers
- 🧾 **Billing mode detection** — tells you whether you're on a **Claude Pro/Max subscription**, an **API key**, a **gateway**, or **Bedrock/Vertex**, and warns when a stale API key is silently overriding your plan
- 🗂 **Session explorer** — every session with rollups, searchable and filterable by project
- ⏱ **Session timeline** — prompts, API calls (with per-call tokens and cost) and tool calls, merged chronologically
- ⚠️ **Failure tracking** — failure rate per tool plus a full error log with the actual error text
- 🔴 **Live activity feed** — optional real-time updates over WebSocket, driven by Claude Code hooks
- 🌗 **Dark & light themes** — follows your OS, toggle persists
- ⚡ **Incremental ingestion** — byte-offset resume; a no-op pass over 120 transcripts takes ~20 ms
- 🔒 **100% local** — SQLite file on disk, no telemetry, no account, no cloud

**Stack:** FastAPI · Jinja2 · SQLite (stdlib) · Chart.js via CDN. **No build step, no npm.**

## Screenshots

<table>
<tr>
<td width="50%"><img src="docs/sessions.png" alt="Claude Code session list with token and cost rollups per session"><br><em>Sessions — rollups, search, project filter</em></td>
<td width="50%"><img src="docs/session-detail.png" alt="Claude Code session timeline showing prompts, API calls and tool calls"><br><em>Session timeline — per-call tokens and cost</em></td>
</tr>
<tr>
<td width="50%"><img src="docs/failures.png" alt="Claude Code tool failure analytics and error log"><br><em>Failures — rate per tool and full error log</em></td>
<td width="50%"><img src="docs/analytics.png" alt="Claude Code cost analytics by model, project and hour of day"><br><em>Analytics — cost by model, project and hour</em></td>
</tr>
</table>

<details>
<summary><strong>Light theme</strong></summary>

![Claude Code dashboard in light theme](docs/dashboard-light.png)

</details>

## Install as a Claude Code plugin (recommended)

The repo ships as a Claude Code plugin, so there is nothing to clone, install, or launch —
the dashboard starts itself whenever a Claude Code session begins.

```
/plugin marketplace add alihassanml/claude-code-monitor
/plugin install claude-code-monitor@alihassanml
```

On the next session start, the plugin creates its own virtualenv, installs dependencies,
backfills your transcript history, and starts the server on **<http://localhost:8787>**.
First run takes ~30 s in the background; the session itself is never blocked. After that,
startup is a single health check (~30 ms).

Hooks for the live feed are registered automatically — no editing `settings.json`.

**Control it from any Bash tool call** (the `bin/` directory is on `PATH` while the plugin
is enabled):

```bash
claude-monitor status     # running? plus today's tokens, cost and billing mode
claude-monitor open       # open the dashboard in your browser
claude-monitor sync       # re-read transcripts now
claude-monitor logs       # tail the server log
claude-monitor stop|start|restart|where
```

| Plugin detail | Value |
|---|---|
| Port | `8787` (override with `CLAUDE_MONITOR_PORT`, and update the URLs in `hooks/hooks.json`) |
| Virtualenv + database | `${CLAUDE_PLUGIN_DATA}` (`~/.claude/plugins/data/…`) — **survives plugin updates** |
| Log | `${CLAUDE_PLUGIN_DATA}/server.log` |
| Python | 3.10+ must exist on `PATH`; the plugin does the rest |

The start script is deliberately defensive: it always exits 0 so a failure can never break a
session, it detaches all slow work so session start isn't delayed, it uses an atomic lock so
ten simultaneous sessions produce exactly one server, and it verifies `/api/health` returns
this app's identifier rather than adopting any process that happens to hold the port.
Dependencies are reinstalled automatically when a plugin update changes `requirements.txt`.

## Quick start (manual, without the plugin)

**Requirements:** Python 3.10+ and an existing Claude Code install (so there are transcripts to read).

```bash
git clone <your-repo-url> claude-code-dashboard
cd claude-code-dashboard

python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python parser.py            # backfill history (~1s for 120 transcripts)
uvicorn main:app --reload   # http://localhost:8000
```

That's it — open <http://localhost:8000>. Hit **Sync transcripts** in the sidebar any time
to pull in new sessions, or wire up [live hooks](#live-updates-via-hooks).

> `python parser.py --rebuild` wipes the derived tables and re-reads every transcript.
> Run it after editing `pricing.py` so costs are recomputed.

## How it works

Claude Code writes one JSONL file per session to
`~/.claude/projects/<project-path>/<session-id>.jsonl`. Each line is one event.

### The duplicate-usage gotcha

This is the single most important detail, and the reason a naive parser reports numbers
that are roughly **1.9× too high**.

An assistant turn is written as **one line per content block** — thinking, text, and each
`tool_use` each get their own line — and the **same `message.usage` object is repeated on
every one of them**. On the machine this was built against, that's **14,751 assistant lines
for 7,810 actual API calls**. Summing `usage` per line double- and triple-counts tokens
and cost.

So the `messages` table is keyed on **`message.id`** with `INSERT OR IGNORE`, counting each
API call exactly once. `tool_calls` are keyed on their own `tool_use` id, and `tool_result`
blocks from the following user turn fill in `is_error`, `error_text` and `duration_ms`.

### Incremental ingestion

`ingest_state` stores a byte offset per file. Only complete lines are consumed — a partial
trailing line means Claude Code is mid-write — and a shrunken file is re-read from the
start. A no-op pass over 120 files takes about **20 ms**, so it's cheap to trigger on every
`Stop` hook.

### Cost model

`pricing.py` holds USD per 1M tokens per model. Edit it there; nothing else knows about
rates. Cache tokens are billed off the input rate at the standard multipliers: **reads ×0.1,
writes ×1.25 (5-minute TTL) or ×2.0 (1-hour TTL)**. Transcripts record the 5m/1h split
separately, so each is billed correctly.

Dated model ids (`claude-haiku-4-5-20251001`) are normalized to their alias. Anything with
no pricing entry falls back to Sonnet-tier rates and is flagged on the Analytics page.

Costs are what published API rates *would* charge for that usage. Whether that's an actual
bill depends on how you're authenticated — which the dashboard detects.

### Billing mode detection

`auth_mode.py` works out how Claude Code is being billed and shows it as a badge at the
bottom of the sidebar, with the full breakdown at `/analytics#billing` (also `GET /api/auth`).

**The obvious check isn't enough.** Testing `ANTHROPIC_API_KEY` vs `CLAUDE_CODE_OAUTH_TOKEN`
correctly identifies API-key users but reports *"unknown"* for most subscription users:
`claude login` stores the OAuth token in the **OS keychain** (macOS) or
`~/.claude/.credentials.json`, **not** in an environment variable —
`CLAUDE_CODE_OAUTH_TOKEN` is the opt-in headless/CI path.

The authoritative signal for a logged-in subscription is the `oauthAccount` block in
`~/.claude.json`, which carries `billingType`, `organizationType` and the subscription start
date. Detection prefers that, and resolves in the same precedence Claude Code itself uses:

| Signal | Reported as |
|---|---|
| `ANTHROPIC_API_KEY` (env or `settings.json` → `env`) | API key · pay-per-token |
| `ANTHROPIC_AUTH_TOKEN` | Gateway token |
| `CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` | Cloud provider |
| `CLAUDE_CODE_OAUTH_TOKEN`, keychain login, or `oauthAccount` | Subscription (with plan name) |
| none of the above | Unknown |

**The warnings are the useful part.** An API key silently outranks a subscription, so the
dashboard flags:

- an API key set **while** you're signed in to a plan — you're paying per token and nothing
  in Claude Code's UI says so;
- a credential exported from a **shell profile** (`~/.zshrc`, `~/.zprofile`, `config.fish`, …)
  even when the dashboard's own environment is clean — a stale key from an old project
  overrides your plan in every new shell. Commented-out lines are ignored; only the file and
  line number are recorded;
- `apiKeyHelper` in `settings.json`, and a non-default `ANTHROPIC_BASE_URL`.

<a name="privacy"></a>
**No credential value is ever read**, logged, or returned — only presence. The keychain probe
checks for the item's existence without `-g`, so it never triggers an access prompt. Results
are cached for 30 s.

## Pages & API

| Page | What's on it |
|---|---|
| `/` | Today's tokens, cost, prompts, sessions and failures; 7/30/90-day charts; live feed; recent sessions; top tools |
| `/sessions` | Every session with rollups; filter by project, search by title/id/path |
| `/sessions/{id}` | Merged timeline of prompts, API calls (per-call tokens + cost) and tool calls, filterable |
| `/failures` | Failure rate per tool, bar chart, and the full failure log with error text |
| `/analytics` | Stacked cost by day & model, activity by hour, token mix, cost by project, billing mode, pricing table |

The plugin serves these on port **8787**; a manual `uvicorn` run defaults to **8000**.

**JSON API** — every page is backed by a documented endpoint:

```
GET  /api/summary                 today + all-time totals, billing mode, DB stats
GET  /api/usage/daily?days=30     per-day token and cost series
GET  /api/usage/cost?days=30      cost by day×model, by model, by project
GET  /api/usage/hourly?days=30    API calls bucketed by local hour
GET  /api/tools                   tool usage and failure counts
GET  /api/auth                    billing mode + warnings
GET  /api/health                  liveness probe with app identifier
GET  /api/sessions                session list with rollups
GET  /api/sessions/{id}           one session + its full timeline
POST /api/ingest[?rebuild=true]   run the incremental parser
```

## Live updates via hooks

**Installed as a plugin, this is already done** — [`hooks/hooks.json`](hooks/hooks.json) is
registered automatically. This section is for manual installs.

Start the dashboard first, then merge the `hooks` block from
[`hooks/settings.json`](hooks/settings.json) into `~/.claude/settings.json` (or a
project-level `.claude/settings.json`). Claude Code POSTs each event to the dashboard, which
stores it, pushes it to any open browser over `/ws`, and re-runs the incremental ingest on
`Stop` / `SessionEnd`.

> The two files are different formats on purpose: `hooks/hooks.json` is the plugin hook
> format (port 8787), `hooks/settings.json` is a `settings.json` fragment for manual setup
> (port 8000).

Endpoints accept either kebab-case URLs or raw event names:
`/hooks/post-tool-use`, `/hooks/post-tool-use-failure`, `/hooks/user-prompt-submit`,
`/hooks/stop`, `/hooks/session-start`, `/hooks/session-end`.

> ⚠️ A hook pointing at a port with nothing listening makes Claude Code wait for the timeout
> on **every** event. Remove the block when the dashboard isn't running.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Where transcripts are read from |
| `CLAUDE_DASHBOARD_DB` | `./claude_dashboard.db` | SQLite file location (the plugin overrides this to its data dir) |
| `CLAUDE_MONITOR_PORT` | `8787` | Port the plugin serves on |

The dashboard binds to localhost and **has no authentication** — it renders your full prompt
text, so don't expose it on a shared interface without putting auth in front of it.

## What this plugin does on your machine

Claude Code plugins run with your user privileges, so here is the complete list of what this
one does — no network calls to anywhere except PyPI, and no data leaves your machine.

**On session start**, `scripts/ensure-running.sh` runs and:

1. Checks `http://127.0.0.1:8787/api/health`. If this app answers, it exits immediately.
2. Otherwise, in a detached background process: creates a Python virtualenv under
   `~/.claude/plugins/data/…`, `pip install`s the four packages in
   [`requirements.txt`](requirements.txt) **from PyPI**, reads your Claude Code transcripts
   into a local SQLite file, and starts a `uvicorn` server bound to `127.0.0.1`.

**During a session**, the HTTP hooks POST each tool-use event to that local server so the
Live activity feed updates.

**What it reads:** `~/.claude/projects/**/*.jsonl` (your transcripts, including prompt text),
`~/.claude.json` and `~/.claude/settings.json` (billing mode), and your shell profiles —
grepped **only** for the *presence* of `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` on a line.

**What it never does:** read or transmit any credential value (only presence is recorded, and
the macOS keychain probe omits `-g` so it can't read the secret and won't prompt), send
telemetry, phone home, or bind to anything but `127.0.0.1`.

**What it writes:** everything stays in `~/.claude/plugins/data/…` — the virtualenv, the
SQLite database, and `server.log`. Nothing is written into your projects. `claude-monitor where`
prints the exact paths, and uninstalling the plugin offers to delete the whole data directory.

To audit it yourself, the entire surface is two files:
[`scripts/ensure-running.sh`](scripts/ensure-running.sh) and
[`auth_mode.py`](auth_mode.py).

## FAQ

<details>
<summary><strong>How do I see how much I've spent on Claude Code?</strong></summary>

Run the parser and open the dashboard — the Cost card shows today and all-time, and
`/analytics` breaks it down by model, project and day. If you're on a subscription, the
figure is *API-equivalent value* rather than a bill, and the UI labels it that way.
</details>

<details>
<summary><strong>Am I being billed per token or is it covered by my Pro plan?</strong></summary>

The badge at the bottom of the sidebar tells you. If an `ANTHROPIC_API_KEY` is set, it
overrides your subscription and you're paying per token — the dashboard raises a warning
banner when it detects this, including the case where the key only exists in a shell profile.
</details>

<details>
<summary><strong>Does this send my data anywhere?</strong></summary>

No. It reads local files, writes a local SQLite database, and serves a local web page. The
only outbound request is the Chart.js CDN script tag — vendor it locally if you want the
dashboard to work fully offline.
</details>

<details>
<summary><strong>Does it work with Claude Max, Team, or an API key?</strong></summary>

Yes — all of them. Usage parsing is identical; only the billing label changes. Amazon
Bedrock and Google Vertex AI are detected too.
</details>

<details>
<summary><strong>Why are my token numbers higher than other tools report?</strong></summary>

Most likely they're cache-read tokens, which dominate agentic workloads and are billed at
0.1× the input rate. The Analytics page has a token-mix breakdown. Conversely, if another
tool reports numbers ~1.9× *higher* than this one, it's probably counting the repeated
`usage` block on every content-block line — see [the gotcha](#the-duplicate-usage-gotcha).
</details>

<details>
<summary><strong>Can I run it against a different transcript directory?</strong></summary>

Set `CLAUDE_PROJECTS_DIR`. Useful for inspecting transcripts copied from another machine.
</details>

## Notes & limits

- **Timezone** — daily buckets use your local date; timestamps are stored as UTC.
- **Resumed sessions** — resuming copies earlier history into a new transcript. Deduping on
  `message.id` counts those calls once, attributed to whichever file was ingested first:
  correct for totals, slightly arbitrary for per-session attribution.
- **`duration_ms`** is wall time from `tool_use` to `tool_result`, so tools that wait on you
  (`AskUserQuestion`) can show very long durations.
- **Sub-agent turns** are counted in tokens and tool calls, but excluded from the prompt
  count, which tracks what you actually typed.
- **Not bundled** — auto-resume on failure and a prompt optimizer; both are independent of
  the core dashboard.

<details>
<summary><strong>CSS notes worth keeping if you edit <code>style.css</code></strong></summary>

Three non-obvious layout rules are load-bearing:

- The main column is `grid-template-columns: var(--sidebar) minmax(0, 1fr)` and `.content`
  sets `min-width: 0`. Without both, a grid item's default `min-width: auto` lets a wide
  table stretch the whole page instead of scrolling inside its own `.table-scroll`.
- `<body>` uses `overflow-x: clip`, **not** `hidden`. `hidden` makes the body a scroll
  container, which silently breaks `position: sticky` and lets the sidebar scroll away with
  the page.
- Tables with free-text columns use `table-layout: fixed` plus declared widths. Under the
  default `auto` layout, `max-width` on a `<td>` is ignored, so one long error string sets
  the table width and pushes the numeric columns out of view.

Chart colors are read from CSS custom properties at render time, so they re-derive on theme
switch rather than being hardcoded.
</details>

## Project structure

```
├── main.py              FastAPI app: pages, JSON API, hook endpoints, WebSocket
├── parser.py            JSONL → SQLite (incremental, offset-resumed)
├── db.py                schema + every query
├── pricing.py           token → USD table (edit rates here)
├── auth_mode.py         subscription / API-key / gateway detection
├── templates/           base, dashboard, sessions, session_detail, failures, analytics
├── static/              style.css, charts.js
├── docs/                screenshots
├── .claude-plugin/      plugin.json + marketplace.json (Claude Code plugin)
├── hooks/hooks.json     plugin hooks (auto-registered)
├── hooks/settings.json  settings fragment for manual installs
├── scripts/             ensure-running.sh — SessionStart auto-start
└── bin/claude-monitor   control CLI, on PATH while the plugin is enabled
```

## Roadmap

- [ ] Budget alerts when daily or monthly spend crosses a threshold
- [ ] CSV / JSON export for external reporting
- [ ] Prompt-length vs cost correlation view
- [ ] Optional auto-resume on failed sessions

## Contributing

Issues and pull requests are welcome. The codebase is small and dependency-light on purpose —
if you're adding a feature, keeping it to stdlib + the existing four packages is preferred.

## License

[MIT](LICENSE) — use it, fork it, ship it.

---

<div align="center">

**Keywords** — claude code dashboard · claude code usage tracker · claude code token usage ·
claude code cost tracking · anthropic api cost calculator · claude code analytics ·
claude pro usage monitor · llm observability · ai coding assistant analytics ·
self-hosted · fastapi · sqlite · python

</div>
