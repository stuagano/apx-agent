# Dev UI

Apps-hosted agents include built-in development tooling at:

- `/_apx/agent` — chat interface for testing
- `/_apx/builder` — visual agent builder (node-based canvas)
- `/_apx/tools` — tool inspector with live invocation
- `/_apx/probe?url=<url>` — outbound connectivity tester

Model Serving deployments use AI Playground as the equivalent surface.

## /_apx/builder — Visual agent builder

A node-based canvas for composing agents without writing Python by hand. Drop tools onto nodes, wire up Sequential / Router / KeywordRouter compositions, save, and the dev server hot-reloads the generated `agent.py`. Switch to `/_apx/agent` to test it. Switch to `/_apx/traces` to debug it.

### What it does

Drag-drop canvas with node types matching apx-agent's primitives — LLM (`Agent`), Supervisor (`SequentialAgent`), Router (`RouterAgent` or `KeywordRouter` depending on routing mode), Vector Search, UC Function, Lakebase. Connect nodes with edges to express composition. Hit Save → the canvas writes an idiomatic apx-agent `agent.py` to the project's working directory plus a `.apx-builder.json` sidecar capturing the layout.

### What it doesn't do (v1)

- **No round-trip from hand-edited `agent.py` back into the canvas.** The canvas reopens via `.apx-builder.json`. If you save with the canvas, then hand-edit `agent.py`, those edits won't appear if you reload the canvas — your edits stay in `agent.py`, the canvas just shows the last-saved graph.
- **No custom `@tool` decorator support.** v1 only emits factory tool calls (`uc_function_tool`, `vector_search_tool`, `genie_tool`). Custom Python tools defined with `@tool` stay hand-written in `tools.py`.
- **No multi-file projects.** Save only writes `agent.py`. If your project has `tools.py` or `prompts.py`, those stay hand-written; the canvas references them by import.
- **No live test runner inside the canvas.** Test by switching to the `/_apx/agent` chat tab — keeps responsibilities separated.

### Opting out

The builder is included in the framework wheel. If you don't want it in a deployment, the route still serves but the `/_apx/builder/save` endpoint can be disabled via a future config knob (TODO). To not see the Builder tab at all, override the nav in your dev UI (custom `_ui_nav.py`).

### Build (for framework contributors)

The SPA lives at `python/builder-ui/`. To rebuild after editing canvas code:

    cd python/builder-ui && npm run build:dist

This writes static assets into `python/src/apx_agent/_builder_ui_dist/`, which the framework wheel ships via `hatch`'s `force-include`.
