#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$ROOT/.pska/config.json"
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
  echo "Creating default config at $CONFIG"
  mkdir -p "$(dirname "$CONFIG")"
  cp "$ROOT/core/config.pska.example.json" "$CONFIG"
fi

config_value() {
  local expression="$1"
  PYTHONPATH="$ROOT/core/src" "$ROOT/.pska/venvs/pska-py312/bin/python" - "$CONFIG" "$expression" <<'PY'
import sys
from urllib.parse import urlparse
from pska_core.config import PSKAConfig

config = PSKAConfig.load(sys.argv[1])
expr = sys.argv[2]
if expr == "database_url":
    print(config.database.url)
elif expr == "database_name":
    parsed = urlparse(config.database.url)
    print((parsed.path or "").lstrip("/"))
elif expr == "database_host":
    parsed = urlparse(config.database.url)
    print(parsed.hostname or "")
elif expr == "workspace_root":
    print(config.workspace.root.expanduser())
elif expr == "service_url":
    print(f"http://{config.service.host}:{config.service.port}/")
elif expr == "service_host":
    print(config.service.host)
elif expr == "service_port":
    print(config.service.port)
elif expr == "startup_bootstrap":
    print(str(config.startup.bootstrap).lower())
elif expr == "startup_backend":
    print(str(config.startup.backend).lower())
elif expr == "startup_frontend":
    print(str(config.startup.frontend.enabled).lower())
elif expr == "frontend_host":
    print(config.startup.frontend.host)
elif expr == "frontend_port":
    print(config.startup.frontend.port)
elif expr == "frontend_url":
    print(f"http://{config.startup.frontend.host}:{config.startup.frontend.port}/")
else:
    raise SystemExit(f"unknown config expression: {expr}")
PY
}

port_listening() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

if [[ "$(config_value startup_bootstrap)" == "true" ]]; then
  echo "Preparing PSKA database and Knowledge Sources..."
  DB_NAME="$(config_value database_name)"
  DB_HOST="$(config_value database_host)"
  if [[ -n "$DB_NAME" && ( -z "$DB_HOST" || "$DB_HOST" == "127.0.0.1" || "$DB_HOST" == "localhost" ) ]]; then
    "$ROOT/scripts/pska" --config "$CONFIG" db-create --name "$DB_NAME"
  fi
  "$ROOT/scripts/pska" --config "$CONFIG" db-init
  WORKSPACE_ROOT="$(config_value workspace_root)"
  DEFAULT_NOTES_ROOT="$WORKSPACE_ROOT/notes"
  mkdir -p "$DEFAULT_NOTES_ROOT"
  "$ROOT/scripts/pska" --config "$CONFIG" knowledge-source add-folder \
    --path "$DEFAULT_NOTES_ROOT" \
    --name "Personal Notes" \
    --mode manual >/dev/null
  if ! "$ROOT/scripts/pska" --config "$CONFIG" files-sync; then
    echo
    echo "PSKA warning: initial Knowledge Source sync failed." >&2
    echo "The app will still start; open the 语料库 page to inspect sources and sync status." >&2
  fi
else
  echo "Skipping bootstrap because startup.bootstrap=false in $CONFIG"
fi

if [[ "$(config_value startup_backend)" == "true" ]]; then
  echo "Starting PSKA backend supervisor..."
  "$ROOT/scripts/pska" --config "$CONFIG" local-daemon --restart &
  BACKEND_PID=$!
else
  echo "Skipping backend because startup.backend=false in $CONFIG"
fi

if [[ "$(config_value startup_frontend)" == "true" ]]; then
  FRONTEND_HOST="$(config_value frontend_host)"
  FRONTEND_PORT="$(config_value frontend_port)"
  if port_listening "$FRONTEND_PORT"; then
    echo "Frontend already appears to be running on $FRONTEND_HOST:$FRONTEND_PORT; reusing it."
  else
  if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
    echo "Installing frontend dependencies..."
    (cd "$ROOT/frontend" && npm install)
  fi
  echo "Starting PSKA frontend..."
  (cd "$ROOT/frontend" && npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT") &
  FRONTEND_PID=$!
  fi
else
  echo "Skipping frontend because startup.frontend.enabled=false in $CONFIG"
fi

cat <<EOF

PSKA dev stack is starting.

Frontend:
  $(config_value frontend_url)

Backend:
  $(config_value service_url)

Useful places:
  Frontend 语料库 page: Knowledge Sources, sync reports, and source/chunk visibility

Useful commands:
  ./scripts/pska --config "$CONFIG" knowledge-source list
  ./scripts/pska --config "$CONFIG" files-sync
  ./scripts/pska --config "$CONFIG" service-check
  ./scripts/pska --config "$CONFIG" local-daemon status

Logs:
  $(config_value workspace_root)/logs/

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
