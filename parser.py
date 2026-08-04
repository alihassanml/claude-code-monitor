"""JSONL transcript -> SQLite.

Reads ``~/.claude/projects/<project-path>/<session-id>.jsonl``. Each line is one
JSON event. Ingestion is incremental — a byte offset per file is stored in
``ingest_state``, so re-running only reads what was appended since last time.

The one non-obvious thing about the format
------------------------------------------
Claude Code writes **one line per content block** of an assistant turn
(thinking / text / tool_use each get their own line) and repeats the *same*
``message.usage`` object on every one of them. Summing usage per line
double- or triple-counts tokens. We key the ``messages`` table on
``message.id`` and use ``INSERT OR IGNORE`` so each API call is counted once.
Tool calls, by contrast, are genuinely one per line and are keyed on their own
``tool_use`` id.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import db
import pricing

PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects")
)

# Wrappers Claude Code injects into user turns that aren't part of what the
# person actually typed.
_NOISE_TAGS = re.compile(
    r"<(system-reminder|ide_opened_file|ide_selection|command-name|command-message|"
    r"command-args|local-command-stdout)>.*?</\1>",
    re.DOTALL,
)


@dataclass
class IngestReport:
    files_seen: int = 0
    files_read: int = 0
    lines: int = 0
    bytes_read: int = 0
    sessions: int = 0
    messages: int = 0
    prompts: int = 0
    tool_calls: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_seen": self.files_seen,
            "files_read": self.files_read,
            "lines": self.lines,
            "bytes_read": self.bytes_read,
            "sessions": self.sessions,
            "messages": self.messages,
            "prompts": self.prompts,
            "tool_calls": self.tool_calls,
            "errors": self.errors[:20],
            "unknown_models": pricing.unknown_models(),
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _clean_text(text: str) -> str:
    return _NOISE_TAGS.sub("", text or "").strip()


def _preview(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = [b.get("text", "") for b in _blocks(content) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# A bare basename like "src" or "frontend" says nothing about which repo it is,
# so for these we keep the parent directory too.
_GENERIC_DIRS = {
    "api", "app", "backend", "client", "code", "dist", "docs", "files", "frontend",
    "lib", "packages", "project", "projects", "server", "site", "source", "src",
    "test", "tests", "web", "www",
}


def _project_name(record: dict, folder: str) -> str:
    cwd = record.get("cwd")
    if cwd:
        path = Path(cwd)
        if path.name.lower() in _GENERIC_DIRS and path.parent.name:
            return f"{path.parent.name}/{path.name}"
        return path.name or cwd
    # Folder names are the cwd with separators replaced by '-'.
    return folder.strip("-").split("-")[-1] or folder


# --------------------------------------------------------------------------- #
# per-file ingestion
# --------------------------------------------------------------------------- #

def _ingest_file(conn: sqlite3.Connection, path: Path, report: IngestReport) -> None:
    cur = conn.cursor()
    stat = path.stat()
    state = cur.execute(
        "SELECT size, offset FROM ingest_state WHERE path = ?", (str(path),)
    ).fetchone()

    offset = 0
    if state:
        prev_size, prev_offset = state["size"], state["offset"]
        if stat.st_size < prev_size:
            offset = 0  # file was truncated or rewritten — start over
        elif stat.st_size == prev_size:
            return  # nothing appended
        else:
            offset = prev_offset

    session_id = path.stem
    folder = path.parent.name
    seen_session = False
    first_ts: str | None = None
    last_ts: str | None = None
    title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    project: str | None = None

    consumed = offset
    with path.open("rb") as fh:
        fh.seek(offset)
        raw = fh.read()

    if not raw:
        return

    report.files_read += 1
    report.bytes_read += len(raw)

    # Only process up to the last complete line; a trailing partial line means
    # Claude Code is mid-write and we'll pick it up on the next pass.
    last_nl = raw.rfind(b"\n")
    if last_nl == -1:
        return
    chunk, consumed = raw[: last_nl + 1], offset + last_nl + 1

    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        report.lines += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            report.errors.append(f"{path.name}: unparseable line")
            continue
        if not isinstance(rec, dict):
            continue

        rtype = rec.get("type")
        sid = rec.get("sessionId") or session_id
        ts = rec.get("timestamp")

        if ts:
            first_ts = min(first_ts, ts) if first_ts else ts
            last_ts = max(last_ts, ts) if last_ts else ts
        if rec.get("cwd"):
            cwd = rec["cwd"]
            project = _project_name(rec, folder)
        if rec.get("gitBranch"):
            git_branch = rec["gitBranch"]

        if rtype == "ai-title":
            title = rec.get("aiTitle") or title
            continue
        if rtype == "last-prompt" and not title:
            title = _preview(_clean_text(rec.get("lastPrompt") or ""), 120) or None
            continue

        if rtype == "assistant":
            seen_session = True
            _ingest_assistant(cur, rec, sid, report)
        elif rtype == "user":
            seen_session = True
            _ingest_user(cur, rec, sid, report)

    if seen_session or state is None:
        if project is None:
            project = folder.strip("-").split("-")[-1] or folder
        cur.execute(
            """INSERT INTO sessions (session_id, project, cwd, git_branch, title,
                                     first_ts, last_ts, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   project     = COALESCE(excluded.project, sessions.project),
                   cwd         = COALESCE(excluded.cwd, sessions.cwd),
                   git_branch  = COALESCE(excluded.git_branch, sessions.git_branch),
                   title       = COALESCE(excluded.title, sessions.title),
                   first_ts    = MIN(COALESCE(sessions.first_ts, excluded.first_ts),
                                     COALESCE(excluded.first_ts, sessions.first_ts)),
                   last_ts     = MAX(COALESCE(sessions.last_ts, excluded.last_ts),
                                     COALESCE(excluded.last_ts, sessions.last_ts)),
                   source_file = excluded.source_file""",
            (session_id, project, cwd, git_branch, title, first_ts, last_ts, str(path)),
        )
        report.sessions += 1

    cur.execute(
        """INSERT INTO ingest_state (path, size, mtime, offset, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
               size = excluded.size, mtime = excluded.mtime,
               offset = excluded.offset, updated_at = excluded.updated_at""",
        (str(path), stat.st_size, stat.st_mtime, consumed, db.now_iso()),
    )


def _ingest_assistant(
    cur: sqlite3.Cursor, rec: dict, session_id: str, report: IngestReport
) -> None:
    msg = rec.get("message") or {}
    if not isinstance(msg, dict):
        return
    ts = rec.get("timestamp") or db.now_iso()
    day = db.local_day(ts)

    api_id = msg.get("id")
    usage = msg.get("usage") or {}
    model = msg.get("model")

    # `<synthetic>` marks locally-generated assistant turns (interrupts, error
    # notices). They carry no usage and were never billed — skip them so they
    # don't show up as a zero-cost "model" in the breakdowns.
    is_synthetic = pricing.normalize_model(model) == "<synthetic>"

    # One row per API call. The same message.id repeats across content-block
    # lines with identical usage, so OR IGNORE keeps the first and drops copies.
    if api_id and not is_synthetic and isinstance(usage, dict) and usage:
        creation = usage.get("cache_creation") or {}
        c5m = creation.get("ephemeral_5m_input_tokens")
        c1h = creation.get("ephemeral_1h_input_tokens")
        speed = usage.get("speed")
        cost = pricing.cost_usd(
            model,
            input_tokens=usage.get("input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
            cache_creation_tokens=usage.get("cache_creation_input_tokens") or 0,
            cache_creation_5m=c5m,
            cache_creation_1h=c1h,
            fast=(speed == "fast"),
        )
        cur.execute(
            """INSERT OR IGNORE INTO messages (
                   api_message_id, uuid, session_id, ts, day, model, effort, speed,
                   input_tokens, output_tokens, cache_read_tokens,
                   cache_creation_tokens, cache_creation_5m, cache_creation_1h,
                   cost_usd, stop_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                api_id,
                rec.get("uuid"),
                session_id,
                ts,
                day,
                pricing.normalize_model(model),
                rec.get("effort"),
                speed,
                usage.get("input_tokens") or 0,
                usage.get("output_tokens") or 0,
                usage.get("cache_read_input_tokens") or 0,
                usage.get("cache_creation_input_tokens") or 0,
                c5m or 0,
                c1h or 0,
                cost,
                msg.get("stop_reason"),
            ),
        )
        report.messages += cur.rowcount

    for block in _blocks(msg.get("content")):
        if block.get("type") != "tool_use":
            continue
        tool_id = block.get("id")
        if not tool_id:
            continue
        cur.execute(
            """INSERT OR IGNORE INTO tool_calls
                   (tool_use_id, session_id, ts, day, name, input_preview, source)
               VALUES (?,?,?,?,?,?, 'transcript')""",
            (tool_id, session_id, ts, day, block.get("name"), _preview(block.get("input"))),
        )
        report.tool_calls += cur.rowcount


def _ingest_user(
    cur: sqlite3.Cursor, rec: dict, session_id: str, report: IngestReport
) -> None:
    msg = rec.get("message") or {}
    if not isinstance(msg, dict):
        return
    ts = rec.get("timestamp") or db.now_iso()
    day = db.local_day(ts)
    content = msg.get("content")

    results = [b for b in _blocks(content) if b.get("type") == "tool_result"]
    for block in results:
        tool_id = block.get("tool_use_id")
        if not tool_id:
            continue
        is_error = 1 if block.get("is_error") else 0
        error_text = _preview(block.get("content"), 800) if is_error else None
        cur.execute(
            """UPDATE tool_calls
                  SET is_error = ?, error_text = ?, result_ts = ?,
                      duration_ms = CAST(
                          (julianday(?) - julianday(ts)) * 86400000 AS INTEGER)
                WHERE tool_use_id = ?""",
            (is_error, error_text, ts, ts, tool_id),
        )
        # A result whose tool_use line we never saw (e.g. ingestion started
        # mid-file) still belongs in the failure log.
        if cur.rowcount == 0 and is_error:
            cur.execute(
                """INSERT OR IGNORE INTO tool_calls
                       (tool_use_id, session_id, ts, day, name, is_error,
                        error_text, result_ts, source)
                   VALUES (?,?,?,?,?,1,?,?, 'transcript')""",
                (tool_id, session_id, ts, day, None, error_text, ts),
            )

    # Anything that isn't purely tool results and isn't a subagent turn counts
    # as a prompt the person submitted.
    if results or rec.get("isSidechain"):
        return
    text = _clean_text(_text_of(content))
    if not text:
        return
    uuid = rec.get("uuid")
    if not uuid:
        return
    cur.execute(
        "INSERT OR IGNORE INTO prompts (uuid, session_id, ts, day, text) VALUES (?,?,?,?,?)",
        (uuid, session_id, ts, day, text[:8000]),
    )
    report.prompts += cur.rowcount


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #

def transcript_files(projects_dir: Path | None = None) -> list[Path]:
    root = projects_dir or PROJECTS_DIR
    if not root.exists():
        return []
    return sorted(root.glob("*/*.jsonl"))


def ingest(projects_dir: Path | None = None, paths: Iterable[Path] | None = None) -> IngestReport:
    """Ingest new lines from every transcript (or just ``paths``)."""
    db.init_db()
    report = IngestReport()
    files = list(paths) if paths is not None else transcript_files(projects_dir)
    conn = db.connect()
    try:
        for path in files:
            report.files_seen += 1
            try:
                _ingest_file(conn, path, report)
                conn.commit()
            except Exception as exc:  # one bad file shouldn't stop the run
                conn.rollback()
                report.errors.append(f"{path.name}: {exc.__class__.__name__}: {exc}")
    finally:
        conn.close()
    return report


def rebuild(projects_dir: Path | None = None) -> IngestReport:
    """Drop the ingest state (and derived rows) and re-read every transcript."""
    db.init_db()
    conn = db.connect()
    try:
        for table in ("ingest_state", "messages", "prompts", "tool_calls", "sessions"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()
    return ingest(projects_dir)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Ingest Claude Code transcripts into SQLite.")
    ap.add_argument("--rebuild", action="store_true", help="wipe and re-read everything")
    ap.add_argument("--dir", type=Path, default=None, help="override ~/.claude/projects")
    args = ap.parse_args()

    result = (rebuild if args.rebuild else ingest)(args.dir)
    for key, value in result.as_dict().items():
        print(f"{key:>16}: {value}")
