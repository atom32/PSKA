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
mkdir -p .pska
cp core/config.pska.example.json .pska/config.json
./scripts/pska --config .pska/config.json db-check
./scripts/pska --config .pska/config.json db-create --name pska
./scripts/pska --config .pska/config.json db-init
cd frontend
npm install
```

The default local database is:

```text
postgresql:///pska
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

Config roots are startup/default seed only. After initialization, runtime
knowledge source and sync state are stored in the database.

## Cold Start E2E

For a clean workspace and full backend/frontend check:

```bash
./scripts/pska-cold-start-e2e --workspace-root ~/PSKA_workspaces/default --reset
```

The reset path is guarded by a `.pska_workspace.json` sentinel so the script
will not delete an arbitrary directory.

FastReAct agentic search requires the FastReAct API endpoint and token in
`.pska/config.json`:

```json
"fastreact": {
  "url": "http://127.0.0.1:8000",
  "service_token": "<fastreact-service-token>",
  "timeout_seconds": 30
}
```

The FastReAct UI at `http://127.0.0.1:3000/service` can work while PSKA
agentic calls still fail with 401; PSKA talks to the API endpoint above, not the
UI page.

## FastReAct Integration

For daily integration, prefer HTTP MCP: start PSKA first with `./start.sh`, then
generate a FastReAct config that points to PSKA's HTTP MCP endpoint:

```bash
./scripts/fastreact-pska-service-config \
  --mcp-transport http \
  --output .pska/fastreact-pska-http.json

cd /Users/xudawei/FastReAct/fastreact-nano
NO_PROXY=127.0.0.1,localhost PYTHONPATH=src \
  python3 -m fastreact.adapters.http \
  --config "/Users/xudawei/Documents/personal archive/.pska/fastreact-pska-http.json"
```

If PSKA service auth is enabled, store the PSKA service token in
`~/.fastreact/credentials.json` under `mcp_api_keys.pska`.

The older stdio MCP mode is still useful for minimal MCP subprocess checks:

```bash
./scripts/fastreact-pska-service-config
python3 -m fastreact.adapters.http --config ~/.fastreact/config.json
```

See the Chinese release/init guide for the full startup and troubleshooting
flow:

```text
docs/RELEASE_INIT_FASTREACT_GUIDE.zh.md
```

## Useful Daily Commands

```bash
./scripts/pska --config .pska/config.json service-check
./scripts/pska --config .pska/config.json local-daemon status
./scripts/pska --config .pska/config.json daily-status
./scripts/pska --config .pska/config.json daily-briefing --owner-user-id user_primary --limit 5
./scripts/pska --config .pska/config.json review-list --status pending --summary
```

## Sync Local Files

Configured file roots live in `.pska/config.json` under `files.roots` for
initial seed/defaults. Runtime source state is database-backed.

```bash
./scripts/pska --config .pska/config.json files-sync
./scripts/pska --config .pska/config.json digest-schedule --owner-user-id user_primary --limit 10
```

`files-sync` covers active folder sources, optional PDF/DOCX extraction,
manifest reconciliation, and the workspace Twitter/X archive inbox. The manual
shortcut is:

```bash
./scripts/pska --config .pska/config.json digest-now
```

`digest-now` runs sync first and then processes one digest pass.

## Verify Today Uses Real Backend Data

Run:

```bash
curl -s http://127.0.0.1:8765/workspace/today/data?owner_user_id=user_primary\&limit=3 \
  | ./.pska/venvs/pska-py312/bin/python -m json.tool
```

If `.pska/config.json` sets `service.service_token`, add:

```bash
-H "Authorization: Bearer <service.service_token>"
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

```json
"startup": { "backend": false, "frontend": { "enabled": true } }
```

Backend only:

```json
"startup": { "backend": true, "frontend": { "enabled": false } }
```

After editing `.pska/config.json`, run `./start.sh`.
