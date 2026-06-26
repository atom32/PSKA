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

The frontend keeps a lightweight PSKA identity context in `sessionStorage`:

- `tenantId`, defaulting to `tenant_default`
- `userId`, defaulting to `user_primary`
- optional `representedUserId`
- optional bearer token/JWT

All API calls derive `X-PSKA-Tenant-Id`, `X-PSKA-User-Id`,
`X-PSKA-Represented-User-Id`, `owner_user_id`, `actor_user_id`, and search user
payloads from that single context. This removes the old single-user
`user_primary` request assumption while keeping local development simple.

For SaaS deployment, run the frontend behind AuthNode or another trusted
gateway that injects JWT/trusted headers. Do not put AuthNode admin tokens in
the browser.
