# PSKA Frontend

This is the first production-oriented PSKA User Workspace.

The product stance is ambient intelligence: the user writes or arranges ideas freely, and PSKA observes context after low-interruption triggers. The right sidebar surfaces related knowledge, entities, historical context, and suggested connections without inserting or rewriting user content.

## Run

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/workspace/*` to the local PSKA HTTP service at `http://127.0.0.1:8765`. If the backend is unavailable, the workspace keeps running with local context analysis and sample knowledge panels.

## Surfaces

- Left sidebar: navigation only, 250px expanded and collapsible.
- Main workspace: document mode with Tiptap and canvas mode with React Flow.
- PSKA Brain: passive ambient suggestions, 350-450px, always visible on desktop.

Context analysis runs on pause, blur, significant content changes, and manual refresh.
