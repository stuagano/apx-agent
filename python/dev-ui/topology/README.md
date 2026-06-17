# apx-agent / dev-ui / topology

Interactive topology view served at `/_apx/topology`. React + Vite + `@xyflow/react`.

## Architecture

```
src/
  main.tsx              # Vite entry — renders <App />
  App.tsx               # owns selection state; wires graph + inspector
  TopologyGraph.tsx     # @xyflow/react graph (Agent 2)
  NodeInspector.tsx     # right-side details panel (Agent 3)
  types.ts              # TypeScript types matching the JSON schemas
  sample-topology.json  # dev fixture
  index.css             # dark theme matching the rest of /_apx/*
```

The Vite build outputs to `../../src/apx_agent/_static/topology/`, which the apx-agent wheel ships and FastAPI serves at `/_apx/topology` via `StaticFiles`.

## Develop

```bash
cd python/dev-ui/topology
npm install                  # one-time
npm run dev                  # http://localhost:5174
```

The dev server proxies `/_apx/topology.json` and `/_apx/topology/inspect/*` to `http://localhost:8000` — run `apx-agent dev` in another terminal against an example agent first.

If the backend isn't running, `App.tsx` falls back to `sample-topology.json` in dev mode so the React surface can be developed standalone.

## Build

```bash
npm run build                # tsc --noEmit + vite build
```

Outputs go to `python/src/apx_agent/_static/topology/`. The maintainer runs this before publishing the wheel; users get a working UI from `pip install` without needing Node installed.

The wheel ships the built bundle via `[tool.hatch.build.targets.wheel.force-include]` in `python/pyproject.toml`:

```toml
"src/apx_agent/_static/topology" = "apx_agent/_static/topology"
```

## Contract

The full design + JSON schemas live in [`docs/superpowers/specs/2026-05-22-topology-ui.md`](../../../docs/superpowers/specs/2026-05-22-topology-ui.md).

- `/_apx/topology.json` → `TopologyResponse` (root + nodes + edges)
- `/_apx/topology/inspect/{node_id}` → `InspectResponse` (type-specific details)

Both shapes are defined in `src/types.ts`. Backend in `python/src/apx_agent/_topology.py` emits matching JSON.
