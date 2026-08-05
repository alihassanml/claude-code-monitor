"""Claude Code Observability Dashboard — FastAPI app.

Run:  uvicorn main:app --reload
Then: http://localhost:8000
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth_mode
import db
import parser
import pricing

BASE_DIR = Path(__file__).parent

APP_ID = "claude-code-monitor"
APP_VERSION = "1.0.1"

app = FastAPI(title="Claude Code Observability Dashboard", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --------------------------------------------------------------------------- #
# template filters
# --------------------------------------------------------------------------- #

def fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def fmt_tokens(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "0"
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(n) >= limit:
            return f"{n / limit:.2f}{suffix}"
    return str(n)


def fmt_usd(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "$0.00"
    if n and abs(n) < 0.01:
        return f"${n:.4f}"
    return f"${n:,.2f}"


def fmt_ts(value: Any, with_date: bool = True) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M") if with_date else dt.strftime("%H:%M:%S")


def fmt_ago(value: Any) -> str:
    """Compact relative time — fits where a full timestamp wouldn't."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    for limit, div, suffix in (
        (3600, 60, "m"), (86400, 3600, "h"), (2592000, 86400, "d"),
    ):
        if seconds < limit:
            return f"{int(seconds // div)}{suffix} ago"
    return dt.astimezone().strftime("%b %-d")


def fmt_duration(ms: Any) -> str:
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return ""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60000}m {ms % 60000 // 1000}s"


templates.env.filters.update(
    fmt_int=fmt_int,
    fmt_tokens=fmt_tokens,
    fmt_usd=fmt_usd,
    fmt_ts=fmt_ts,
    fmt_ago=fmt_ago,
    fmt_duration=fmt_duration,
)


# --------------------------------------------------------------------------- #
# websocket fan-out for live hook events
# --------------------------------------------------------------------------- #

class LiveFeed:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        message = json.dumps(payload, default=str)
        async with self._lock:
            targets = list(self._clients)
        dead = []
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)


feed = LiveFeed()


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #

def _page(request: Request, template: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("today", date.today().isoformat())
    ctx.setdefault("auth", auth_mode.detect())
    return templates.TemplateResponse(request, template, ctx)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    today = date.today().isoformat()
    auth = auth_mode.detect()
    return _page(
        request,
        "dashboard.html",
        page="dashboard",
        auth=auth,
        cost_caption=auth_mode.cost_caption(auth),
        today_summary=db.summary(today),
        all_summary=db.summary(None),
        recent_sessions=db.list_sessions(limit=8),
        tools=db.tool_usage(limit=8),
        model_costs=db.cost_by_model(days=30),
        live_events=db.recent_hook_events(limit=25),
    )


@app.get("/sessions", response_class=HTMLResponse)
def sessions_page(
    request: Request,
    page: int = Query(1, ge=1),
    project: str | None = None,
    q: str | None = None,
):
    per_page = 50
    total = db.count_sessions(project=project, search=q)
    rows = db.list_sessions(
        limit=per_page, offset=(page - 1) * per_page, project=project, search=q
    )
    return _page(
        request,
        "sessions.html",
        page="sessions",
        sessions=rows,
        projects=db.list_projects(),
        current_project=project,
        query=q or "",
        page_num=page,
        per_page=per_page,
        total=total,
        pages=max(1, (total + per_page - 1) // per_page),
    )


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_detail(request: Request, session_id: str):
    session = db.get_session(session_id)
    if session is None:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"page": "sessions", "session_id": session_id, "today": date.today().isoformat()},
            status_code=404,
        )
    return _page(
        request,
        "session_detail.html",
        page="sessions",
        session=session,
        timeline=db.session_timeline(session_id),
    )


@app.get("/failures", response_class=HTMLResponse)
def failures_page(request: Request, tool: str | None = None):
    return _page(
        request,
        "failures.html",
        page="failures",
        failures=db.list_failures(limit=200, tool=tool),
        by_tool=db.failures_by_tool(),
        current_tool=tool,
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, days: int = Query(30, ge=1, le=365)):
    return _page(
        request,
        "analytics.html",
        page="analytics",
        days=days,
        cost_caption=auth_mode.cost_caption(auth_mode.detect()),
        model_costs=db.cost_by_model(days=days),
        project_costs=db.cost_by_project(days=days),
        pricing_rows=pricing.pricing_table(),
        unknown_models=pricing.unknown_models(),
        stats=db.db_stats(),
    )


# --------------------------------------------------------------------------- #
# JSON API (consumed by Chart.js)
# --------------------------------------------------------------------------- #

@app.get("/api/usage/daily")
def api_usage_daily(days: int = Query(30, ge=1, le=365)):
    return {"days": days, "series": db.daily_usage(days)}


@app.get("/api/usage/cost")
def api_usage_cost(days: int = Query(30, ge=1, le=365)):
    return {
        "days": days,
        "by_day_and_model": db.cost_by_day_and_model(days),
        "by_model": db.cost_by_model(days),
        "by_project": db.cost_by_project(days),
    }


@app.get("/api/usage/hourly")
def api_usage_hourly(days: int = Query(30, ge=1, le=365)):
    return {"days": days, "buckets": db.hourly_activity(days)}


@app.get("/api/tools")
def api_tools():
    return {"usage": db.tool_usage(limit=25), "failures": db.failures_by_tool()}


@app.get("/api/summary")
def api_summary():
    return {
        "today": db.summary(date.today().isoformat()),
        "all_time": db.summary(None),
        "stats": db.db_stats(),
        "auth": auth_mode.detect(),
        "live_clients": feed.count,
    }


@app.get("/api/auth")
def api_auth(refresh: bool = False):
    """How Claude Code is authenticated, and therefore how it's billed."""
    return auth_mode.detect(refresh=refresh)


@app.get("/api/health")
def api_health():
    """Liveness probe with an app identifier.

    The plugin's start script uses this to tell *this* server apart from
    whatever else might be listening on the port — a bare port check would
    happily accept someone else's dev server and then POST hooks at it.
    """
    return {"app": APP_ID, "version": APP_VERSION, "status": "ok"}


@app.get("/api/sessions")
def api_sessions(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    return {"sessions": db.list_sessions(limit=limit, offset=offset)}


@app.get("/api/sessions/{session_id}")
def api_session(session_id: str):
    session = db.get_session(session_id)
    if session is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"session": session, "timeline": db.session_timeline(session_id)}


@app.post("/api/ingest")
async def api_ingest(rebuild: bool = False):
    fn = parser.rebuild if rebuild else parser.ingest
    report = await asyncio.to_thread(fn)
    await feed.broadcast({"type": "ingest", "report": report.as_dict()})
    return report.as_dict()


# --------------------------------------------------------------------------- #
# hooks (live ingest from Claude Code)
# --------------------------------------------------------------------------- #

def _summarize_hook(event: str, body: dict) -> tuple[str | None, bool, str]:
    """-> (tool_name, is_error, human summary)"""
    tool = body.get("tool_name") or body.get("toolName")
    response = body.get("tool_response") or body.get("toolResponse") or {}
    is_error = bool(
        body.get("is_error")
        or body.get("error")
        or (isinstance(response, dict) and response.get("is_error"))
        or event.lower().endswith("failure")
    )
    if event == "UserPromptSubmit":
        summary = parser._preview(body.get("prompt"), 200)
    elif tool:
        summary = f"{tool} {parser._preview(body.get('tool_input') or body.get('toolInput'), 160)}"
    else:
        summary = parser._preview(body.get("message") or body.get("reason") or event, 200)
    if is_error:
        detail = parser._preview(
            body.get("error")
            or (response.get("error") if isinstance(response, dict) else None)
            or response,
            200,
        )
        if detail:
            summary = f"{summary} — {detail}"
    return tool, is_error, summary.strip()


async def _handle_hook(event: str, request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {"value": body}

    event_name = body.get("hook_event_name") or event
    session_id = body.get("session_id") or body.get("sessionId")
    tool, is_error, summary = _summarize_hook(event_name, body)

    record = await asyncio.to_thread(
        db.insert_hook_event, event_name, session_id, tool, is_error, summary, body
    )
    await feed.broadcast({"type": "hook", "event": record})

    # SessionEnd is the natural moment to pull the finished transcript in.
    if event_name in ("SessionEnd", "Stop"):
        report = await asyncio.to_thread(parser.ingest)
        await feed.broadcast({"type": "ingest", "report": report.as_dict()})

    return {"ok": True}


# Accept either the kebab-case URL from settings.json or the raw event name.
_HOOK_SLUGS = {
    "pre-tool-use": "PreToolUse",
    "post-tool-use": "PostToolUse",
    "post-tool-use-failure": "PostToolUseFailure",
    "user-prompt-submit": "UserPromptSubmit",
    "stop": "Stop",
    "session-start": "SessionStart",
    "session-end": "SessionEnd",
    "notification": "Notification",
    "subagent-stop": "SubagentStop",
}


@app.post("/hooks/{event}")
async def hook_endpoint(event: str, request: Request):
    return await _handle_hook(_HOOK_SLUGS.get(event.lower(), event), request)


@app.get("/api/hooks/recent")
def api_hooks_recent(limit: int = Query(50, ge=1, le=500)):
    return {"events": db.recent_hook_events(limit)}


# --------------------------------------------------------------------------- #
# websocket
# --------------------------------------------------------------------------- #

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await feed.connect(ws)
    try:
        await ws.send_text(
            json.dumps(
                {"type": "hello", "recent": db.recent_hook_events(20)}, default=str
            )
        )
        while True:
            await ws.receive_text()  # client keepalives; nothing to parse
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await feed.disconnect(ws)
