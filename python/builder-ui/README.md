# apx-agent Visual Builder UI

React SPA for the /_apx/builder dev-UI tab. Ported from [veenaramesh/dbrx-agent-builder](https://github.com/veenaramesh/dbrx-agent-builder); retargeted to emit apx-agent DSL instead of raw LangGraph.

## Build

    npm install
    npm run build:dist

That writes static assets into `../src/apx_agent/_builder_ui_dist/` — included in the framework wheel via `pyproject.toml`'s `force-include`.

## Dev

    npm run dev

Serves on `http://localhost:5173/_apx/builder/`. To exercise the save endpoint locally, run a scaffolded apx-agent app on `:8000` and configure CORS or use a proxy.
