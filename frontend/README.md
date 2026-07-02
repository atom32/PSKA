# PSKA Frontend

This is the PSKA User Workspace for the tenant build.

For the full documentation map, see [docs/README.md](../docs/README.md).

## Run

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies PSKA API/login paths to
`PSKA_VITE_PROXY_TARGET`, falling back to `PSKA_VITE_API_TARGET`, then
`http://127.0.0.1:8765`. For hot-reload testing through PSKA Gateway, run the
gateway on another port and start Vite with:

```bash
PSKA_VITE_PROXY_TARGET=http://127.0.0.1:8080 npm run dev
```

From the repository root, the normal path is:

```bash
./start.sh
```

## Current Surfaces

- Today: real backend data with empty/error states.
- Discoveries: backend discovery feed and score-filtered items.
- Knowledge Sources: folder, RSS/Atom, and URL preview/create/sync flows with
  processing diagnostics, chunk preview, cleanup, and retry.
- Corpus/Brain: search and corpus-backed context panels.
- Ask PSKA: direct and FastReAct-backed answers with citations, evidence
  preview, progress, and no-answer diagnostics.
- Graph: early visualization over available graph/corpus context.
- Review-oriented flows: pending candidates and approve/apply paths continue to
  evolve with the backend review model.
- Writing/Evidence Briefs: digest notes, claims, and review artifacts can seed
  citation-backed writing board drafts.

The frontend is not yet a full durable document editor or general canvas
persistence layer. Keep production browser access behind AuthNode/Gateway or a
trusted ingress; local Vite mode is for hot reload.

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

For local login-protected testing on the usual frontend port, the default
`startup.frontend.mode` is `gateway`; `./start.sh` serves the built frontend
through PSKA Gateway on `:5173` and redirects
unauthenticated browsers to AuthNode `/login`. AuthNode redirects back with a
one-time code, PSKA Gateway exchanges it server-side, and the browser only keeps
a PSKA HttpOnly session cookie. Use `mode: "vite"` for hot reload.

See [Enterprise Auth Gateway](../docs/ENTERPRISE_AUTH_GATEWAY.zh.md).
