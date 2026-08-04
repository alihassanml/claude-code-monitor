#!/usr/bin/env bash
#
# SessionStart hook: make sure the dashboard is running, starting it if not.
#
# Design constraints, in priority order:
#   1. Never fail a Claude Code session. Every path exits 0.
#   2. Never block session start. The slow work (venv, pip, backfill) happens in
#      a detached re-invocation of this script; the hook itself returns at once.
#   3. Never write state into ${CLAUDE_PLUGIN_ROOT}. That directory is replaced
#      on every plugin update, so the virtualenv and the SQLite database live in
#      ${CLAUDE_PLUGIN_DATA}, which survives updates.
#   4. Never adopt a stranger's server. A bare port check passes for any process
#      on the port; we require /api/health to identify itself.
#
# Usage: ensure-running.sh            (hook entry point)
#        ensure-running.sh --worker   (internal: does the slow work)

set -uo pipefail   # deliberately not -e: a failure here must not break a session

APP_ID="claude-code-monitor"
PORT="${CLAUDE_MONITOR_PORT:-8787}"

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$SELF")/.." && pwd)}"
DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.claude/plugins/data/${APP_ID}}"

VENV="$DATA/venv"
LOG="$DATA/server.log"
LOCK="$DATA/.starting.lock"
STAMP="$DATA/requirements.txt"       # manifest the current venv was built from
DB="$DATA/claude_dashboard.db"

mkdir -p "$DATA" 2>/dev/null || exit 0

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG" 2>/dev/null; }

health_ok() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null \
    | grep -q "\"app\":\"${APP_ID}\""
}

# ---------------------------------------------------------------------------
# Worker: the detached half. Installs dependencies if needed, backfills, then
# becomes the server process.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--worker" ]; then
  release_lock() { rm -rf "$LOCK" 2>/dev/null; }
  trap release_lock EXIT

  # Rebuild the venv when it is missing, or when a plugin update shipped a
  # different requirements.txt. Comparing manifests — rather than just testing
  # for the directory — is what makes updates pick up new dependencies.
  if [ ! -x "$VENV/bin/python" ] || ! diff -q "$ROOT/requirements.txt" "$STAMP" >/dev/null 2>&1; then
    log "installing dependencies into $VENV"
    rm -rf "$VENV" 2>/dev/null
    if ! "$PYTHON_BIN" -m venv "$VENV" >>"$LOG" 2>&1; then
      log "venv creation failed - on Debian/Ubuntu try: apt install python3-venv"
      rm -f "$STAMP" 2>/dev/null
      exit 0
    fi
    if ! "$VENV/bin/python" -m pip install -q --disable-pip-version-check \
          -r "$ROOT/requirements.txt" >>"$LOG" 2>&1; then
      log "pip install failed - will retry next session"
      rm -f "$STAMP" 2>/dev/null    # force a retry rather than keep a broken venv
      exit 0
    fi
    cp "$ROOT/requirements.txt" "$STAMP" 2>/dev/null
    log "dependencies installed"
  fi

  export CLAUDE_DASHBOARD_DB="$DB"
  cd "$ROOT" || exit 0

  # Backfill first so the first page load isn't empty.
  "$VENV/bin/python" parser.py >>"$LOG" 2>&1

  # We already know /api/health didn't answer, so anything holding this port is
  # not us. Say so plainly instead of letting uvicorn fail with a bare
  # "address already in use".
  if "$VENV/bin/python" - "$PORT" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(0)     # occupied
else:
    sys.exit(1)     # free
finally:
    s.close()
PY
  then
    log "port ${PORT} is held by another process - set CLAUDE_MONITOR_PORT to a free port"
    log "(and update the URLs in the plugin's hooks/hooks.json to match)"
    exit 0
  fi

  log "starting server on http://127.0.0.1:${PORT}"
  release_lock          # server runs for the session's lifetime; don't hold it
  trap - EXIT
  exec "$VENV/bin/python" -m uvicorn main:app \
       --host 127.0.0.1 --port "$PORT" --log-level warning >>"$LOG" 2>&1
fi

# ---------------------------------------------------------------------------
# Hook entry point: fast checks only, then hand off.
# ---------------------------------------------------------------------------
health_ok && exit 0

# Single-flight. Several sessions can start at once; only one should install.
# mkdir is atomic, so it doubles as the lock. A lock older than 10 minutes is
# assumed stale (a previous run was killed mid-install) and reclaimed.
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rm -rf "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0   # another session is already bringing it up
  fi
fi

PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  log "no Python 3.10+ on PATH - dashboard not started"
  rm -rf "$LOCK" 2>/dev/null
  exit 0
fi

export CLAUDE_PLUGIN_ROOT="$ROOT" CLAUDE_PLUGIN_DATA="$DATA"
export CLAUDE_MONITOR_PORT="$PORT" PYTHON_BIN

# Detach so the hook returns immediately and the server outlives this session.
if command -v setsid >/dev/null 2>&1; then
  setsid bash "$SELF" --worker </dev/null >/dev/null 2>&1 &
else
  nohup bash "$SELF" --worker </dev/null >/dev/null 2>&1 &
fi
disown 2>/dev/null || true

exit 0
