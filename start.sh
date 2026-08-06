#!/usr/bin/env bash
# Launch Crossband: build the SPA if needed, boot the backend on 8902.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${MMC_PORT:-8902}"

# One clean instance at a time — but wait a little first. A restart normally
# follows a stop by a second or two, and an instance that was told to stop still
# holds the port while it drains; failing instantly turned an ordinary redeploy
# into "something is already listening", about a process that was on its way
# out (same root cause as the data-lock message).
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "· port $PORT is still in use — waiting up to 10s for a stopping instance to release it"
  for _ in $(seq 1 100); do
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.1
  done
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  HOLDER="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
  echo "✗ something is still listening on port $PORT after 10s (pid ${HOLDER:-unknown})." >&2
  echo "  Stop it:          kill ${HOLDER:-<pid>}" >&2
  echo "  Refuses to exit:  kill -9 ${HOLDER:-<pid>}  (stuck draining)" >&2
  exit 1
fi

# python env
if [ ! -d .venv ]; then
  echo "· creating .venv"
  python3 -m venv .venv
fi
STAMP=.venv/.deps-stamp
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "· installing python deps"
  .venv/bin/pip install -q -r requirements.txt
  touch "$STAMP"
fi

# Frontend build — skipped when dist is newer than everything that goes into it.
#
# That list must include the MANIFEST and BUILD CONFIG, not just the source.
# It didn't, and deploying the React 19 upgrade found out why: a pull
# that changes only package.json touches nothing this checks, so the gate
# declared dist current, skipped the block — and with it the `npm install` —
# leaving the service running the old bundle against the old node_modules.
# Nothing failed. It just quietly served stale code, which is worse than a
# build error, because every symptom points at the app instead of the deploy.
if [ ! -d frontend/dist ] || [ -n "$(find frontend/src frontend/index.html \
    frontend/package.json frontend/package-lock.json frontend/vite.config.js \
    -newer frontend/dist -print -quit 2>/dev/null)" ]; then
  echo "· building frontend"
  (cd frontend && npm install --silent && npm run build --silent)
fi

# env + key report
if [ -f .env ]; then
  # .env holds live API keys: owner-only, always. Tightened on every start, not
  # just at creation, so an .env made before this existed (or copied in by hand)
  # is fixed rather than left world-readable forever.
  chmod 600 .env 2>/dev/null || true
  set -a; source .env; set +a
elif [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "· created .env from .env.example — add your keys, then rerun ./start.sh"
  exit 0
fi

warn() { printf '\033[33m! %s\033[0m\n' "$1"; }
[ -z "${ANTHROPIC_API_KEY:-}" ]  && warn "ANTHROPIC_API_KEY missing — Claude participants disabled"
[ -z "${OPENAI_API_KEY:-}" ]     && warn "OPENAI_API_KEY missing — GPT participants disabled"
[ -z "${ELEVENLABS_API_KEY:-}" ] && warn "ELEVENLABS_API_KEY missing — voice conversations disabled (this is the key you want: each model speaks in its own voice)"
[ -z "${TAVILY_API_KEY:-}" ] && [ -z "${BRAVE_API_KEY:-}" ] && warn "no search key (TAVILY_API_KEY / BRAVE_API_KEY) — web research disabled"

if ! curl -sf --max-time 1 http://127.0.0.1:8901/v1/health >/dev/null 2>&1; then
  warn "memory service not detected on :8901; chat runs memoryless (run Membro for shared memory)"
fi

echo "→ http://127.0.0.1:$PORT"
exec .venv/bin/python -m backend
