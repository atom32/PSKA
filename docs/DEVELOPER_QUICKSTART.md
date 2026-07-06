# PSKA Developer Quickstart

Goal: get a new developer from checkout to a working local PSKA UI in about 30
minutes.

Examples use path variables for sibling checkouts:

```bash
export PSKA_REPO="$(pwd)"
export AUTHNODE_REPO="/path/to/AuthNode"
export FASTREACT_NANO_REPO="/path/to/FastReAct/fastreact-nano"
```

## One-Command Dev Stack

From the repository root:

```bash
./start.sh
```

This starts:

- PSKA backend supervisor via `./scripts/pska --config .pska/config.json local-daemon --restart`
- Frontend according to `.pska/config.json`: gateway mode serves the built app
  behind AuthNode login; vite mode starts the Vite hot-reload server.

Open:

```text
http://127.0.0.1:5173/
```

The frontend proxies `/workspace/*` to:

```text
http://127.0.0.1:8765/
```

Press `Ctrl-C` in the `start.sh` terminal to stop both frontend and backend.

## Local AuthNode Login Smoke

For the normal login-protected local entrypoint, use gateway mode on port
`5173`:

```json
"startup": {
  "frontend": {
    "enabled": true,
    "mode": "gateway",
    "host": "0.0.0.0",
    "port": 5173,
    "public_url": "http://127.0.0.1:5173"
  }
}
```

`host` is the bind address. `public_url` is the browser-facing gateway URL used
as the AuthNode callback origin. For LAN or Docker testing, set `public_url` to
the URL users actually open.

Start AuthNode from its own repository, then start PSKA from this repository:

```bash
cd "$AUTHNODE_REPO"
./start.sh

cd "$PSKA_REPO"
export AUTHNODE_URL=http://127.0.0.1:8788
export PSKA_GATEWAY_SESSION_SECRET='<random-long-secret>'
./start.sh
```

Open `http://127.0.0.1:5173/`. If there is no PSKA gateway session, PSKA
redirects the browser to AuthNode `/login`. AuthNode either uses its Local IAM
catalog, shows the legacy JSON dev form, or redirects to Keycloak when
configured for OIDC. It then redirects back with a short-lived one-time code;
PSKA Gateway exchanges that code server-side, sets a signed HttpOnly session
cookie, and proxies frontend API calls to PSKA.

The browser receives only the HttpOnly session cookie and tenant/user metadata
from `/auth/session`. AuthNode admin tokens, PSKA service tokens, FastReAct
tokens, and PSKA JWTs remain server-side.

AuthNode Local IAM can be initialized with `python -m authnode iam init
--seed-config`. AuthNode local dev login remains available at AuthNode
`/login?local=1`. PSKA Gateway does not provide its own browser login form or
token-broker path; it redirects to AuthNode and only handles the AuthNode
callback/session boundary. Production should use AuthNode Local IAM,
AuthNode/OIDC, or a trusted ingress to perform user login, while PSKA runs with
JWT or trusted-header authentication.

See `docs/ENTERPRISE_AUTH_GATEWAY.zh.md` for the full AuthNode/PSKA/FastReAct
identity flow. Use `"mode": "vite"` only when you explicitly want frontend hot
reload instead of the login-protected gateway entrypoint.

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
  "url": "http://127.0.0.1:18741",
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

cd "$FASTREACT_NANO_REPO"
NO_PROXY=127.0.0.1,localhost PYTHONPATH=src \
  python3 -m fastreact.adapters.http \
  --config "$PSKA_REPO/.pska/fastreact-pska-http.json"
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

`files-sync` covers active folder sources, PDF/DOCX/XLSX extraction,
optional legacy XLS extraction, manifest reconciliation, and the workspace
Twitter/X archive inbox. Uploads through the product UI use the same document
extractors. The built-in PDF extractor only reads text with `pypdf`; PDF table
layout, scanned PDFs, and image-only files require the optional external
document parser. Large-file and spreadsheet extraction limits are controlled by
`files.max_bytes`, `files.spreadsheet_max_rows_per_sheet`, and
`files.spreadsheet_max_columns` in `.pska/config.json`. Existing deployments
must update their runtime `.pska/config.json`; pulling git changes only updates
`core/config.pska.example.json`, not an already-created local config. For browser
upload acceptance tests with annual-report spreadsheets, use at least:

```json
{
  "files": {
    "max_bytes": 52428800,
    "spreadsheet_max_rows_per_sheet": 2000,
    "spreadsheet_max_columns": 80
  }
}
```

To use `~/DocParserServer` for PDF tables, scanned PDFs, and images, start that
service separately and enable PSKA's parser bridge:

```json
{
  "document_parser": {
    "enabled": true,
    "url": "http://127.0.0.1:8083/rag/model_parser_file",
    "timeout_seconds": 120,
    "extract_image": false,
    "extract_image_content": true,
    "return_json": true,
    "extensions": [".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"]
  }
}
```

The same settings can be supplied with `PSKA_DOCUMENT_PARSER_ENABLED`,
`PSKA_DOCUMENT_PARSER_URL`, `PSKA_DOCUMENT_PARSER_TIMEOUT_SECONDS`,
`PSKA_DOCUMENT_PARSER_EXTRACT_IMAGE`,
`PSKA_DOCUMENT_PARSER_EXTRACT_IMAGE_CONTENT`,
`PSKA_DOCUMENT_PARSER_RETURN_JSON`, and
`PSKA_DOCUMENT_PARSER_EXTENSIONS`.

The end-to-end browser path is covered by:

```bash
npm --prefix frontend run e2e:upload-delete
```

It uploads an XLSX through the product UI, asks over the uploaded workbook, then
soft-deletes that source and confirms the deleted source is no longer returned
as Ask evidence. The manual shortcut is:

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
