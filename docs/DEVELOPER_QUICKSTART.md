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

In the UI, Today shows either:

- `真实 PSKA 数据已接入` when backend data is available.
- `本地原型数据正在显示` when the backend is offline or unauthorized.

## Troubleshooting

Backend status:

```bash
./scripts/pska --config .pska/config.json local-daemon status
./scripts/pska --config .pska/config.json service-check
```

Logs:

```bash
ls .pska/logs
tail -f .pska/logs/pska-service.log
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
