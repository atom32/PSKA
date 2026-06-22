# PSKA Developer Quickstart

Goal: get a new developer from checkout to a working local PSKA UI in about 30
minutes.

## One-Command Dev Stack

From the repository root:

```bash
./start.sh
```

This starts:

- PSKA backend supervisor via `./scripts/pska --config .pska/config.json local-daemon --restart`
- Frontend Vite dev server via `cd frontend && npm run dev`

Open:

```text
http://127.0.0.1:5173/
```

The frontend proxies `/workspace/*` to:

```text
http://127.0.0.1:8765/
```

Press `Ctrl-C` in the `start.sh` terminal to stop both frontend and backend.

## First-Time Setup

```bash
brew install python@3.12
./scripts/bootstrap_pska_env
./scripts/pska --config .pska/config.json db-check
cd frontend
npm install
```

If the database does not exist yet:

```bash
./scripts/pska --config .pska/config.json db-init
```

The current local config is expected at:

```text
.pska/config.json
```

Do not commit `.pska/`; it may contain local paths and credentials.

PSKA runtime/user data defaults to:

```text
~/PSKA_workspaces/default
```

Imports, Twitter/X zip inboxes, daemon run files, logs, and cold-start fixtures
belong there, not under the source repo's `workspaces/default/`.

## Cold Start E2E

For a clean workspace and full backend/frontend check:

```bash
./scripts/pska-cold-start-e2e --workspace-root ~/PSKA_workspaces/default --reset
```

The reset path is guarded by a `.pska_workspace.json` sentinel so the script
will not delete an arbitrary directory.

FastReAct agentic search requires the FastReAct API token explicitly:

```bash
export PSKA_FASTREACT_URL="http://127.0.0.1:8000"
export PSKA_FASTREACT_SERVICE_TOKEN="<fastreact-service-token>"
```

The FastReAct UI at `http://127.0.0.1:3000/service` can work while PSKA
agentic calls still fail with 401; PSKA talks to the API endpoint above, not the
UI page.

## Useful Daily Commands

```bash
./scripts/pska --config .pska/config.json service-check
./scripts/pska --config .pska/config.json local-daemon status
./scripts/pska --config .pska/config.json daily-status
./scripts/pska --config .pska/config.json daily-briefing --owner-user-id user_primary --limit 5
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

## Sync Local Files

Configured file roots live in `.pska/config.json` under `files.roots`.

```bash
./scripts/pska --config .pska/config.json files-sync
./scripts/pska --config .pska/config.json digest-schedule --owner-user-id user_primary --limit 10
```

## Verify Today Uses Real Backend Data

Run:

```bash
curl -s http://127.0.0.1:8765/workspace/today/data?owner_user_id=user_primary\&limit=3 \
  | ./.pska/venvs/pska-py312/bin/python -m json.tool
```

If `PSKA_SERVICE_TOKEN` is configured, add:

```bash
-H "Authorization: Bearer $PSKA_SERVICE_TOKEN"
```

In the UI, Today only shows real backend data. Empty sections render as empty
states, and backend/proxy/token failures render as errors rather than prototype
cards.

## Troubleshooting

Backend status:

```bash
./scripts/pska --config .pska/config.json local-daemon status
./scripts/pska --config .pska/config.json service-check
```

Logs:

```bash
ls ~/PSKA_workspaces/default/logs
tail -f ~/PSKA_workspaces/default/logs/pska-service.log
```

Port conflicts:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
```

Frontend only:

```bash
PSKA_SKIP_BACKEND=1 ./start.sh
```

Backend only:

```bash
PSKA_SKIP_FRONTEND=1 ./start.sh
```
