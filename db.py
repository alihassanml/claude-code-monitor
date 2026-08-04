"""SQLite schema + every query the app runs.

Design notes
------------
* One row per *API message* (keyed on ``message.id``), not per JSONL line.
  Claude Code writes one line per content block and repeats the same ``usage``
  block on each of them, so keying on the line ``uuid`` would multiply-count
  tokens for any turn that produced thinking + text + tool_use.
* ``day`` columns hold the **local** date so "today" on the dashboard means the
  user's today. Timestamps themselves stay ISO-8601 UTC.
* Ingestion is incremental: ``ingest_state`` remembers a byte offset per file.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(os.environ.get("CLAUDE_DASHBOARD_DB", Path(__file__).parent / "claude_dashboard.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    project      TEXT,
    cwd          TEXT,
    git_branch   TEXT,
    title        TEXT,
    first_ts     TEXT,
    last_ts      TEXT,
    source_file  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_last ON sessions(last_ts DESC);

CREATE TABLE IF NOT EXISTS messages (
    api_message_id TEXT PRIMARY KEY,   -- message.id; dedupes multi-line turns
    uuid           TEXT,
    session_id     TEXT NOT NULL,
    ts             TEXT NOT NULL,
    day            TEXT NOT NULL,      -- local YYYY-MM-DD
    model          TEXT,
    effort         TEXT,
    speed          TEXT,
    input_tokens            INTEGER DEFAULT 0,
    output_tokens           INTEGER DEFAULT 0,
    cache_read_tokens       INTEGER DEFAULT 0,
    cache_creation_tokens   INTEGER DEFAULT 0,
    cache_creation_5m       INTEGER DEFAULT 0,
    cache_creation_1h       INTEGER DEFAULT 0,
    cost_usd       REAL DEFAULT 0,
    stop_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_day ON messages(day);
CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model);

CREATE TABLE IF NOT EXISTS prompts (
    uuid        TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    day         TEXT NOT NULL,
    text        TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_prompts_day ON prompts(day);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    day         TEXT NOT NULL,
    name        TEXT,
    input_preview TEXT,
    is_error    INTEGER DEFAULT 0,
    error_text  TEXT,
    result_ts   TEXT,
    duration_ms INTEGER,
    source      TEXT DEFAULT 'transcript'   -- 'transcript' | 'hook'
);
CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_tools_day ON tool_calls(day);
CREATE INDEX IF NOT EXISTS idx_tools_error ON tool_calls(is_error, ts DESC);

CREATE TABLE IF NOT EXISTS hook_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    day         TEXT NOT NULL,
    event_type  TEXT,
    session_id  TEXT,
    tool_name   TEXT,
    is_error    INTEGER DEFAULT 0,
    summary     TEXT,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_hooks_ts ON hook_events(ts DESC);

CREATE TABLE IF NOT EXISTS ingest_state (
    path        TEXT PRIMARY KEY,
    size        INTEGER,
    mtime       REAL,
    offset      INTEGER,
    updated_at  TEXT
);
"""


# --------------------------------------------------------------------------- #
# connection helpers
# --------------------------------------------------------------------------- #

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def local_day(ts_iso: str | None) -> str:
    """ISO-8601 UTC timestamp -> local YYYY-MM-DD."""
    if not ts_iso:
        return date.today().isoformat()
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return date.today().isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date().isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# aggregate queries
# --------------------------------------------------------------------------- #

_TOKEN_SUM = """
    COALESCE(SUM(input_tokens), 0)          AS input_tokens,
    COALESCE(SUM(output_tokens), 0)         AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
    COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
    COALESCE(SUM(cost_usd), 0.0)            AS cost_usd,
    COUNT(*)                                AS api_calls
"""


def summary(day: str | None = None) -> dict[str, Any]:
    """Totals for one local day, or all time when ``day`` is None."""
    where, params = ("WHERE day = ?", (day,)) if day else ("", ())
    with cursor() as cur:
        row = cur.execute(f"SELECT {_TOKEN_SUM} FROM messages {where}", params).fetchone()
        out = dict(row)
        out["total_tokens"] = (
            out["input_tokens"]
            + out["output_tokens"]
            + out["cache_read_tokens"]
            + out["cache_creation_tokens"]
        )
        out["prompts"] = cur.execute(
            f"SELECT COUNT(*) FROM prompts {where}", params
        ).fetchone()[0]
        out["tool_calls"] = cur.execute(
            f"SELECT COUNT(*) FROM tool_calls {where}", params
        ).fetchone()[0]
        out["failures"] = cur.execute(
            f"SELECT COUNT(*) FROM tool_calls {'WHERE day = ? AND' if day else 'WHERE'} is_error = 1",
            params,
        ).fetchone()[0]
        out["sessions"] = cur.execute(
            "SELECT COUNT(DISTINCT session_id) FROM messages " + where, params
        ).fetchone()[0]
    return out


def daily_usage(days: int = 30) -> list[dict[str, Any]]:
    """One row per local day for the last N days, zero-filled."""
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    with cursor() as cur:
        rows = cur.execute(
            f"""SELECT day, {_TOKEN_SUM} FROM messages
                WHERE day >= ? GROUP BY day ORDER BY day""",
            (start,),
        ).fetchall()
        prompts = dict(
            cur.execute(
                "SELECT day, COUNT(*) FROM prompts WHERE day >= ? GROUP BY day", (start,)
            ).fetchall()
        )
        sessions = dict(
            cur.execute(
                "SELECT day, COUNT(DISTINCT session_id) FROM messages WHERE day >= ? GROUP BY day",
                (start,),
            ).fetchall()
        )

    by_day = {r["day"]: dict(r) for r in rows}
    series = []
    for i in range(days):
        d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        r = by_day.get(d)
        series.append(
            {
                "day": d,
                "input_tokens": r["input_tokens"] if r else 0,
                "output_tokens": r["output_tokens"] if r else 0,
                "cache_read_tokens": r["cache_read_tokens"] if r else 0,
                "cache_creation_tokens": r["cache_creation_tokens"] if r else 0,
                "total_tokens": (
                    r["input_tokens"]
                    + r["output_tokens"]
                    + r["cache_read_tokens"]
                    + r["cache_creation_tokens"]
                )
                if r
                else 0,
                "cost_usd": round(r["cost_usd"], 6) if r else 0.0,
                "api_calls": r["api_calls"] if r else 0,
                "prompts": prompts.get(d, 0),
                "sessions": sessions.get(d, 0),
            }
        )
    return series


def cost_by_model(days: int | None = 30) -> list[dict[str, Any]]:
    where, params = "", ()
    if days:
        where = "WHERE day >= ?"
        params = ((date.today() - timedelta(days=days - 1)).isoformat(),)
    with cursor() as cur:
        rows = cur.execute(
            f"""SELECT COALESCE(model, 'unknown') AS model, {_TOKEN_SUM}
                FROM messages {where}
                GROUP BY model ORDER BY cost_usd DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def cost_by_day_and_model(days: int = 30) -> dict[str, Any]:
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    with cursor() as cur:
        rows = cur.execute(
            """SELECT day, COALESCE(model, 'unknown') AS model, SUM(cost_usd) AS cost_usd
               FROM messages WHERE day >= ?
               GROUP BY day, model ORDER BY day""",
            (start,),
        ).fetchall()
    models = sorted({r["model"] for r in rows})
    days_list = [(date.today() - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    table = {m: {d: 0.0 for d in days_list} for m in models}
    for r in rows:
        table[r["model"]][r["day"]] = round(r["cost_usd"], 6)
    return {
        "days": days_list,
        "models": models,
        "series": [{"model": m, "cost": [table[m][d] for d in days_list]} for m in models],
    }


def cost_by_project(days: int | None = 30) -> list[dict[str, Any]]:
    where, params = "", ()
    if days:
        where = "WHERE m.day >= ?"
        params = ((date.today() - timedelta(days=days - 1)).isoformat(),)
    with cursor() as cur:
        rows = cur.execute(
            f"""SELECT COALESCE(s.project, 'unknown') AS project,
                       COUNT(DISTINCT m.session_id) AS sessions,
                       SUM(m.input_tokens + m.output_tokens
                           + m.cache_read_tokens + m.cache_creation_tokens) AS total_tokens,
                       SUM(m.cost_usd) AS cost_usd
                FROM messages m LEFT JOIN sessions s ON s.session_id = m.session_id
                {where}
                GROUP BY project ORDER BY cost_usd DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def hourly_activity(days: int = 30) -> list[int]:
    """API calls bucketed by local hour-of-day (0-23) — a simple activity profile."""
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    buckets = [0] * 24
    with cursor() as cur:
        for (ts,) in cur.execute("SELECT ts FROM messages WHERE day >= ?", (start,)):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            buckets[dt.hour] += 1
    return buckets


# --------------------------------------------------------------------------- #
# session queries
# --------------------------------------------------------------------------- #

SESSION_ROLLUP = """
SELECT s.session_id, s.project, s.cwd, s.git_branch, s.title,
       s.first_ts, s.last_ts,
       COALESCE(m.api_calls, 0)      AS api_calls,
       COALESCE(m.input_tokens, 0)   AS input_tokens,
       COALESCE(m.output_tokens, 0)  AS output_tokens,
       COALESCE(m.cache_read_tokens, 0) AS cache_read_tokens,
       COALESCE(m.cache_creation_tokens, 0) AS cache_creation_tokens,
       COALESCE(m.input_tokens, 0) + COALESCE(m.output_tokens, 0)
         + COALESCE(m.cache_read_tokens, 0) + COALESCE(m.cache_creation_tokens, 0)
         AS total_tokens,
       COALESCE(m.cost_usd, 0.0)     AS cost_usd,
       m.models                      AS models,
       COALESCE(p.n, 0)              AS prompts,
       COALESCE(t.n, 0)              AS tool_calls,
       COALESCE(t.errors, 0)         AS failures
FROM sessions s
LEFT JOIN (
    SELECT session_id, COUNT(*) AS api_calls,
           SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
           SUM(cache_read_tokens) AS cache_read_tokens,
           SUM(cache_creation_tokens) AS cache_creation_tokens,
           SUM(cost_usd) AS cost_usd,
           GROUP_CONCAT(DISTINCT model) AS models
    FROM messages GROUP BY session_id
) m ON m.session_id = s.session_id
LEFT JOIN (SELECT session_id, COUNT(*) AS n FROM prompts GROUP BY session_id) p
       ON p.session_id = s.session_id
LEFT JOIN (
    SELECT session_id, COUNT(*) AS n, SUM(is_error) AS errors
    FROM tool_calls GROUP BY session_id
) t ON t.session_id = s.session_id
"""


def list_sessions(
    limit: int = 100, offset: int = 0, project: str | None = None, search: str | None = None
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if project:
        clauses.append("s.project = ?")
        params.append(project)
    if search:
        clauses.append("(s.title LIKE ? OR s.session_id LIKE ? OR s.cwd LIKE ?)")
        params += [f"%{search}%"] * 3
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with cursor() as cur:
        rows = cur.execute(
            f"{SESSION_ROLLUP} {where} ORDER BY s.last_ts DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_sessions(project: str | None = None, search: str | None = None) -> int:
    clauses, params = [], []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if search:
        clauses.append("(title LIKE ? OR session_id LIKE ? OR cwd LIKE ?)")
        params += [f"%{search}%"] * 3
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with cursor() as cur:
        return cur.execute(f"SELECT COUNT(*) FROM sessions {where}", params).fetchone()[0]


def get_session(session_id: str) -> dict[str, Any] | None:
    with cursor() as cur:
        row = cur.execute(f"{SESSION_ROLLUP} WHERE s.session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def session_timeline(session_id: str) -> list[dict[str, Any]]:
    """Prompts, API calls and tool calls merged into one chronological list."""
    events: list[dict[str, Any]] = []
    with cursor() as cur:
        for r in cur.execute(
            "SELECT ts, text FROM prompts WHERE session_id = ?", (session_id,)
        ):
            events.append({"kind": "prompt", "ts": r["ts"], "text": r["text"] or ""})
        for r in cur.execute(
            """SELECT ts, model, effort, input_tokens, output_tokens, cache_read_tokens,
                      cache_creation_tokens, cost_usd, stop_reason
               FROM messages WHERE session_id = ?""",
            (session_id,),
        ):
            e = dict(r)
            e["kind"] = "message"
            events.append(e)
        for r in cur.execute(
            """SELECT ts, name, input_preview, is_error, error_text, duration_ms
               FROM tool_calls WHERE session_id = ?""",
            (session_id,),
        ):
            e = dict(r)
            e["kind"] = "tool"
            events.append(e)
    events.sort(key=lambda e: e["ts"] or "")
    return events


def list_projects() -> list[dict[str, Any]]:
    with cursor() as cur:
        rows = cur.execute(
            """SELECT project, COUNT(*) AS sessions, MAX(last_ts) AS last_ts
               FROM sessions WHERE project IS NOT NULL
               GROUP BY project ORDER BY last_ts DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# failures
# --------------------------------------------------------------------------- #

def list_failures(limit: int = 200, tool: str | None = None) -> list[dict[str, Any]]:
    where, params = "WHERE t.is_error = 1", []
    if tool:
        where += " AND t.name = ?"
        params.append(tool)
    with cursor() as cur:
        rows = cur.execute(
            f"""SELECT t.tool_use_id, t.session_id, t.ts, t.name, t.input_preview,
                       t.error_text, s.project
                FROM tool_calls t LEFT JOIN sessions s ON s.session_id = t.session_id
                {where} ORDER BY t.ts DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def failures_by_tool() -> list[dict[str, Any]]:
    with cursor() as cur:
        rows = cur.execute(
            """SELECT name,
                      SUM(is_error) AS failures,
                      COUNT(*)      AS total,
                      ROUND(100.0 * SUM(is_error) / COUNT(*), 1) AS failure_rate
               FROM tool_calls GROUP BY name
               HAVING failures > 0 ORDER BY failures DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def tool_usage(limit: int = 20) -> list[dict[str, Any]]:
    with cursor() as cur:
        rows = cur.execute(
            """SELECT name, COUNT(*) AS calls, SUM(is_error) AS failures
               FROM tool_calls GROUP BY name ORDER BY calls DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# hook events (live feed)
# --------------------------------------------------------------------------- #

def insert_hook_event(
    event_type: str,
    session_id: str | None,
    tool_name: str | None,
    is_error: bool,
    summary_text: str,
    payload: dict,
) -> dict[str, Any]:
    ts = now_iso()
    with cursor() as cur:
        cur.execute(
            """INSERT INTO hook_events (ts, day, event_type, session_id, tool_name,
                                        is_error, summary, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ts,
                local_day(ts),
                event_type,
                session_id,
                tool_name,
                1 if is_error else 0,
                summary_text,
                json.dumps(payload)[:20000],
            ),
        )
    return {
        "ts": ts,
        "event_type": event_type,
        "session_id": session_id,
        "tool_name": tool_name,
        "is_error": is_error,
        "summary": summary_text,
    }


def recent_hook_events(limit: int = 50) -> list[dict[str, Any]]:
    with cursor() as cur:
        rows = cur.execute(
            """SELECT ts, event_type, session_id, tool_name, is_error, summary
               FROM hook_events ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def db_stats() -> dict[str, Any]:
    with cursor() as cur:
        stats = {
            t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("sessions", "messages", "prompts", "tool_calls", "hook_events")
        }
        stats["files_ingested"] = cur.execute("SELECT COUNT(*) FROM ingest_state").fetchone()[0]
        stats["last_ingest"] = cur.execute(
            "SELECT MAX(updated_at) FROM ingest_state"
        ).fetchone()[0]
    stats["db_path"] = str(DB_PATH)
    stats["db_size_bytes"] = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return stats
