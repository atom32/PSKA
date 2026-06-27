# PSKA Frontend

This is the PSKA User Workspace scaffold.

For the full documentation map, see [docs/README.md](../docs/README.md).

## Run

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/workspace/*` to the local PSKA HTTP service at
`http://127.0.0.1:8765`.

From the repository root, the normal path is:

```bash
./start.sh
```

## Current Surfaces

- Today: real backend data with empty/error states.
- Discoveries: backend discovery feed and score-filtered items.
- Corpus/Brain: search and corpus-backed context panels.
- Graph: early visualization over available graph/corpus context.
- Review-adjacent flows: partial backend wiring; full Review workspace is still
  evolving.

The frontend is not yet a durable document editor, canvas persistence layer, or
Knowledge Sources/file management UI. Those are next-step product work.

See [Backend Feature Map](BACKEND_FEATURES.md) for the current frontend/backend
capability map.

## Multi-Tenant Identity

For local Vite development, the frontend keeps a lightweight PSKA identity
context in `sessionStorage`:

- `tenantId`, defaulting to `tenant_default`
- `userId`, defaulting to `user_primary`
- optional `representedUserId`
- optional bearer token/JWT

All API calls derive `X-PSKA-Tenant-Id`, `X-PSKA-User-Id`,
`X-PSKA-Represented-User-Id`, `owner_user_id`, `actor_user_id`, and search user
payloads from that single context. This removes the old single-user
`user_primary` request assumption while keeping local development simple.

For SaaS deployment, run the built frontend behind AuthNode/OIDC, a trusted
ingress, or a BFF that verifies AuthNode login artifacts. The browser must not
receive AuthNode admin tokens, PSKA service tokens, FastReAct tokens, or PSKA
JWTs.

For local login-protected testing on the usual frontend port, set
`startup.frontend.mode` to `gateway` in `.pska/config.json`; then `./start.sh`
serves the built frontend through PSKA Gateway on `:5173` and redirects
unauthenticated browsers to AuthNode `/login`. AuthNode redirects back with a
one-time code, PSKA Gateway exchanges it server-side, and the browser only keeps
a PSKA HttpOnly session cookie. Use `mode: "vite"` for hot reload.

See [Enterprise Auth Gateway](../docs/ENTERPRISE_AUTH_GATEWAY.zh.md).
