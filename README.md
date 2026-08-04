# Claude Code Monitor Dashboard

A local dashboard over your own Claude Code usage: tokens, cost, prompts, sessions,
tool calls and failures — parsed from the JSONL transcripts Claude Code already
writes, with an optional live feed driven by HTTP hooks.

**Stack:** FastAPI · Jinja2 · SQLite (stdlib) · Chart.js via CDN. No build step.

```
├── main.py            FastAPI app: pages, JSON API, hook endpoints, WebSocket
├── parser.py          JSONL → SQLite (incremental)
├── db.py              schema + every query
├── pricing.py         token → USD table
├── auth_mode.py       subscription / API-key / gateway detection
├── templates/         base, dashboard, sessions, session_detail, failures, analytics
├── static/            style.css, charts.js
└── hooks/settings.json   hook config to merge into ~/.claude/settings.json
```

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python parser.py            # backfill history (~1s for 120 transcripts)
uvicorn main:app --reload   # http://localhost:8000
```

`python parser.py --rebuild` wipes the derived tables and re-reads everything —
use it after editing `pricing.py` so costs are recomputed.

## How the data is read

Claude Code writes one JSONL file per session to
`~/.claude/projects/<project-path>/<session-id>.jsonl`. Each line is one event.

**The gotcha this parser exists to handle:** an assistant turn is written as
*one line per content block* — thinking, text and each `tool_use` get their own
line — and the same `message.usage` object is repeated on every one of them. On
this machine that's 14,751 assistant lines for 7,810 actual API calls, so a
parser that sums `usage` per line inflates tokens and cost by roughly 1.9×.

`messages` is therefore keyed on `message.id` with `INSERT OR IGNORE`, so each
API call is counted exactly once. `tool_calls` are keyed on their own `tool_use`
id, and `tool_result` blocks from the following user turn fill in
`is_error` / `error_text` / `duration_ms`.

Ingestion is incremental: `ingest_state` stores a byte offset per file, only
complete lines are consumed (a partial trailing line means Claude Code is
mid-write), and a shrunken file is re-read from the start. A no-op pass over
120 files takes ~20ms, so it is cheap to call on every `Stop` hook.

## Cost model

`pricing.py` holds USD per 1M tokens per model — edit it there, nothing else
knows about rates. Cache tokens are billed off the input rate at the standard
multipliers: reads ×0.1, writes ×1.25 (5-minute TTL) or ×2.0 (1-hour TTL). The
transcripts record the 5m/1h split separately, so each is billed correctly.

Dated model ids (`claude-haiku-4-5-20251001`) are normalized to their alias.
Anything with no pricing entry falls back to Sonnet-tier rates and is listed on
the Analytics page so you know to add it.

Costs are what the published API rates *would* charge for that usage. Whether that's
an actual bill depends on how you're authenticated — which the dashboard detects (below)
and reflects in the labels: on a subscription the card reads "Value today · covered by
plan"; on an API key it reads "Cost today · billed per token".

## Billing mode detection

`auth_mode.py` works out how Claude Code is being billed and shows it as a badge at the
bottom of the sidebar, with the full breakdown at `/analytics#billing` (also
`GET /api/auth`).

**The obvious check isn't enough.** Testing `ANTHROPIC_API_KEY` vs
`CLAUDE_CODE_OAUTH_TOKEN` correctly identifies API-key users, but reports "unknown"
for most subscription users: `claude login` stores the OAuth token in the **OS keychain**
(macOS) or `~/.claude/.credentials.json`, not in an environment variable —
`CLAUDE_CODE_OAUTH_TOKEN` is the opt-in headless/CI path. On this machine none of those
env vars are set at all.

The authoritative signal for a logged-in subscription is the `oauthAccount` block in
`~/.claude.json`, which carries `billingType`, `organizationType`, and the subscription
start date. Detection prefers that, and resolves in the same precedence Claude Code
itself uses:

| Signal | Reported as |
|---|---|
| `ANTHROPIC_API_KEY` (env or `settings.json` → `env`) | API key · pay-per-token |
| `ANTHROPIC_AUTH_TOKEN` | Gateway token |
| `CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` | Cloud provider |
| `CLAUDE_CODE_OAUTH_TOKEN`, keychain login, or `oauthAccount` | Subscription (with plan name) |
| none of the above | Unknown |

**The warnings are the useful part.** An API key silently outranks a subscription, so
the dashboard flags:

- an API key set **while** you're signed in to a plan — you're paying per token and
  nothing in Claude Code's UI says so;
- a credential exported from a **shell profile** (`~/.zshrc`, `~/.zprofile`,
  `config.fish`, …) even when the dashboard's own environment is clean — a stale key
  from an old project overrides your plan in every new shell. Commented-out lines are
  ignored; only the file and line number are recorded;
- `apiKeyHelper` in `settings.json`, and a non-default `ANTHROPIC_BASE_URL`.

**No credential value is ever read**, logged, or returned — only presence. The keychain
probe checks for the item's existence without `-g`, so it never triggers an access
prompt. Results are cached for 30s.

One limitation, stated in the UI: this reads *this machine's* environment. If Claude
Code runs from a different shell, its environment may differ from the dashboard's — which
is exactly why the shell-profile scan exists.

## Pages

| Page | What's on it |
|---|---|
| `/` | Today's tokens/cost/prompts/sessions/failures, 7/30/90-day charts, live feed, recent sessions, top tools |
| `/sessions` | Every session with per-session rollups; filter by project, search by title/id/path |
| `/sessions/{id}` | Merged timeline of prompts, API calls (per-call tokens + cost) and tool calls, filterable |
| `/failures` | Failure-rate table per tool, bar chart, and the full failure log with error text |
| `/analytics` | Stacked cost by day & model, activity by hour, token mix, cost by project, billing mode, the pricing table in use |

JSON API: `/api/summary`, `/api/usage/daily`, `/api/usage/cost`, `/api/usage/hourly`,
`/api/tools`, `/api/auth`, `/api/sessions`, `/api/sessions/{id}`,
`POST /api/ingest[?rebuild=true]`.

**Theme.** Dark and light are both first-class; the toggle sits at the bottom of the
sidebar and the choice persists in `localStorage`. First visit follows your OS
setting. Chart colors are read from CSS custom properties at render time, so they
re-derive on switch rather than being hardcoded.

Three layout notes worth keeping if you edit `style.css`:

- The main column is `grid-template-columns: var(--sidebar) minmax(0, 1fr)` and
  `.content` sets `min-width: 0`. Without both, a grid item's default
  `min-width: auto` lets a wide table stretch the whole page instead of scrolling
  inside its own `.table-scroll`.
- `<body>` uses `overflow-x: clip`, **not** `hidden`. `hidden` makes the body a
  scroll container, which silently breaks `position: sticky` and lets the sidebar
  scroll away with the page.
- Tables with free-text columns use `table-layout: fixed` plus declared widths.
  Under the default `auto` layout, `max-width` on a `<td>` is ignored, so one long
  error string sets the table width and pushes the numeric columns out of view.

## Live updates (optional)

Start the dashboard first, then merge the `hooks` block from
[`hooks/settings.json`](hooks/settings.json) into `~/.claude/settings.json` (or a
project-level `.claude/settings.json`). Claude Code POSTs each event to the
dashboard, which stores it, pushes it to any open browser over `/ws` (the socket
reconnects with backoff on its own; there is no connection indicator), and
re-runs the incremental ingest on `Stop` / `SessionEnd`.

Endpoints accept either kebab-case URLs or raw event names:
`/hooks/post-tool-use`, `/hooks/post-tool-use-failure`, `/hooks/user-prompt-submit`,
`/hooks/stop`, `/hooks/session-start`, `/hooks/session-end`.

A hook pointing at a port with nothing listening will make Claude Code wait for
the timeout on every event — remove the block when the dashboard isn't running.

## Notes and limits

- **Timezone.** Daily buckets use your local date; timestamps are stored as UTC.
- **Resumed sessions.** Resuming copies earlier history into a new transcript.
  Deduping on `message.id` means those calls are counted once and attributed to
  whichever file was ingested first — right for cost totals, slightly arbitrary
  for per-session attribution.
- **`duration_ms`** is wall time from `tool_use` to `tool_result`, so tools that
  wait on you (`AskUserQuestion`) can show very long durations.
- **Sub-agent turns** are counted in tokens and tool calls but excluded from the
  prompt count, which tracks what you actually typed.
- **Not bundled:** auto-resume on failure and the prompt optimizer — both are
  independent of the core dashboard.

## Config

| Env var | Default |
|---|---|
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` |
| `CLAUDE_DASHBOARD_DB` | `./claude_dashboard.db` |

The dashboard binds to localhost and has no auth — it reads your full prompt
text, so don't expose it on a shared interface.
