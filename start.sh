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
elif expr == "frontend_mode":
    print(config.startup.frontend.mode)
elif expr == "frontend_host":
    print(config.startup.frontend.host)
elif expr == "frontend_port":
    print(config.startup.frontend.port)
elif expr == "frontend_url":
    print(f"http://{config.startup.frontend.host}:{config.startup.frontend.port}/")
elif expr == "service_client_url":
    host = config.service.host
    if host in {"", "0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    print(f"http://{host}:{config.service.port}")
else:
    raise SystemExit(f"unknown config expression: {expr}")
PY
}

port_listening() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

port_listener_pids() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

port_listener_hosts() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 {print $9}' | sed -E 's/^(.+):[0-9]+$/\1/' | sort -u
}

port_has_wildcard_listener() {
  local port="$1"
  port_listener_hosts "$port" | grep -Eq '^(\\*|0\\.0\\.0\\.0|\\[::\\]|::)$'
}

pid_command() {
  local pid="$1"
  ps -p "$pid" -o command= 2>/dev/null || true
}

is_project_pska_process() {
  local pid="$1"
  local subcommand="$2"
  local command
  command="$(pid_command "$pid")"
  [[ "$command" == *"pska_core.cli"* && "$command" == *"--config $CONFIG"* && "$command" == *" $subcommand "* ]]
}

stop_project_pska_listeners() {
  local port="$1"
  local subcommand="$2"
  local label="$3"
  local pids=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    if is_project_pska_process "$pid" "$subcommand"; then
      pids+=("$pid")
    fi
  done < <(port_listener_pids "$port")

  if [[ "${#pids[@]}" -eq 0 ]]; then
    return 0
  fi

  echo "Stopping stale PSKA $label listener(s) on port $port: ${pids[*]}"
  for pid in "${pids[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  for _ in {1..30}; do
    local any_running=false
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" >/dev/null 2>&1; then
        any_running=true
        break
      fi
    done
    if [[ "$any_running" == "false" ]]; then
      return 0
    fi
    sleep 0.2
  done
  echo "PSKA warning: stale $label listener did not exit after SIGTERM; sending SIGKILL." >&2
  for pid in "${pids[@]}"; do
    kill -9 "$pid" >/dev/null 2>&1 || true
  done
}

lan_ip() {
  local ip=""
  if command -v ipconfig >/dev/null 2>&1; then
    local iface=""
    iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}' || true)"
    if [[ -n "$iface" ]]; then
      ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    fi
    if [[ -z "$ip" ]]; then
      for iface in en0 en1; do
        ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
        [[ -n "$ip" ]] && break
      done
    fi
  fi
  if [[ -z "$ip" ]] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  echo "${ip:-127.0.0.1}"
}

display_url() {
  local host="$1"
  local port="$2"
  if [[ "$host" == "0.0.0.0" || "$host" == "::" ]]; then
    host="$(lan_ip)"
  fi
  echo "http://$host:$port/"
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
  SERVICE_PORT="$(config_value service_port)"
  stop_project_pska_listeners "$SERVICE_PORT" "serve" "backend"
  echo "Starting PSKA backend supervisor..."
  "$ROOT/scripts/pska" --config "$CONFIG" local-daemon --restart &
  BACKEND_PID=$!
else
  echo "Skipping backend because startup.backend=false in $CONFIG"
fi

if [[ "$(config_value startup_frontend)" == "true" ]]; then
  FRONTEND_HOST="$(config_value frontend_host)"
  FRONTEND_PORT="$(config_value frontend_port)"
  FRONTEND_MODE="$(config_value frontend_mode)"
  if [[ "$FRONTEND_MODE" == "gateway" ]]; then
    stop_project_pska_listeners "$FRONTEND_PORT" "gateway" "gateway frontend"
  fi
  if port_listening "$FRONTEND_PORT"; then
    echo "Frontend port $FRONTEND_PORT already has a listener; reusing it."
    if [[ "$FRONTEND_HOST" == "0.0.0.0" || "$FRONTEND_HOST" == "::" ]] && ! port_has_wildcard_listener "$FRONTEND_PORT"; then
      echo "PSKA warning: the existing frontend listener does not appear to be bound to $FRONTEND_HOST." >&2
      echo "For LAN access, stop the old frontend process and rerun ./start.sh." >&2
      echo "Current frontend listeners:" >&2
      port_listener_hosts "$FRONTEND_PORT" >&2 || true
    fi
  else
    if [[ "$FRONTEND_MODE" == "gateway" ]]; then
      if [[ -z "${AUTHNODE_ADMIN_TOKEN:-}" && -z "${PSKA_GATEWAY_AUTHNODE_ADMIN_TOKEN:-}" ]]; then
        echo "PSKA gateway will use AuthNode browser login when no local token-broker admin token is configured."
        echo "Set PSKA_GATEWAY_AUTHNODE_URL or AUTHNODE_URL if AuthNode is not at http://127.0.0.1:8788."
      fi
      if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
        echo "Installing frontend dependencies..."
        (cd "$ROOT/frontend" && npm install)
      fi
      echo "Building PSKA frontend for gateway mode..."
      (cd "$ROOT/frontend" && npm run build)
      echo "Starting PSKA gateway frontend..."
      "$ROOT/scripts/pska" --config "$CONFIG" gateway \
        --host "$FRONTEND_HOST" \
        --port "$FRONTEND_PORT" \
        --pska-url "$(config_value service_client_url)" &
      FRONTEND_PID=$!
    else
      if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
        echo "Installing frontend dependencies..."
        (cd "$ROOT/frontend" && npm install)
      fi
      echo "Starting PSKA frontend..."
      (cd "$ROOT/frontend" && npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT") &
      FRONTEND_PID=$!
    fi
  fi
else
  echo "Skipping frontend because startup.frontend.enabled=false in $CONFIG"
fi

FRONTEND_HOST="$(config_value frontend_host)"
FRONTEND_PORT="$(config_value frontend_port)"
SERVICE_HOST="$(config_value service_host)"
SERVICE_PORT="$(config_value service_port)"
FRONTEND_URL="$(display_url "$FRONTEND_HOST" "$FRONTEND_PORT")"
SERVICE_URL="$(display_url "$SERVICE_HOST" "$SERVICE_PORT")"

cat <<EOF

PSKA dev stack is starting.

Frontend:
  $FRONTEND_URL

Backend:
  $SERVICE_URL

Listening:
  Frontend bind: $FRONTEND_HOST:$FRONTEND_PORT ($(config_value frontend_mode))
  Backend bind:  $SERVICE_HOST:$SERVICE_PORT

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
