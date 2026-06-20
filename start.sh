#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${PSKA_CONFIG:-$ROOT/.pska/config.json}"
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
  local status=$?
  echo
  echo "Stopping PSKA dev stack..."
  if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" >/dev/null 2>&1; then
    kill "$FRONTEND_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    kill "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
  wait "$FRONTEND_PID" >/dev/null 2>&1 || true
  wait "$BACKEND_PID" >/dev/null 2>&1 || true
  exit "$status"
}

trap cleanup INT TERM EXIT

if [[ ! -x "$ROOT/.pska/venvs/pska-py312/bin/python" ]]; then
  echo "Missing PSKA Python environment." >&2
  echo "Run first: ./scripts/bootstrap_pska_env" >&2
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "Missing config: $CONFIG" >&2
  echo "Copy one from core/config.pska.example.json or run with PSKA_CONFIG=/path/to/config.json ./start.sh" >&2
  exit 1
fi

if [[ "${PSKA_SKIP_BACKEND:-0}" != "1" ]]; then
  echo "Starting PSKA backend supervisor..."
  "$ROOT/scripts/pska" --config "$CONFIG" local-daemon --restart &
  BACKEND_PID=$!
else
  echo "Skipping backend because PSKA_SKIP_BACKEND=1"
fi

if [[ "${PSKA_SKIP_FRONTEND:-0}" != "1" ]]; then
  if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
    echo "Installing frontend dependencies..."
    (cd "$ROOT/frontend" && npm install)
  fi
  echo "Starting PSKA frontend..."
  (cd "$ROOT/frontend" && npm run dev) &
  FRONTEND_PID=$!
else
  echo "Skipping frontend because PSKA_SKIP_FRONTEND=1"
fi

cat <<'EOF'

PSKA dev stack is starting.

Frontend:
  http://127.0.0.1:5173/

Backend:
  http://127.0.0.1:8765/

Useful checks:
  ./scripts/pska --config .pska/config.json service-check
  ./scripts/pska --config .pska/config.json local-daemon status

Logs:
  .pska/logs/

Press Ctrl-C to stop this dev stack.
EOF

while true; do
  if [[ -n "$BACKEND_PID" ]] && ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    wait "$BACKEND_PID"
  fi
  if [[ -n "$FRONTEND_PID" ]] && ! kill -0 "$FRONTEND_PID" >/dev/null 2>&1; then
    wait "$FRONTEND_PID"
  fi
  sleep 1
done
