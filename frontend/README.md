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
