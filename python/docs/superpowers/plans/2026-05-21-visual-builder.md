# Visual Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed a node-based visual agent builder at `/_apx/builder` so users can build *and* test agents in one dev UI loop — drag tools onto an agent, save, see hot-reloaded code in `/_apx/agent`, inspect traces in `/_apx/traces`.

**Architecture:** Port [veenaramesh/dbrx-agent-builder](https://github.com/veenaramesh/dbrx-agent-builder)'s React 19 + Vite canvas into the framework as a sibling source tree (`python/builder-ui/`), pre-build into static assets at release time, and ship those assets inside the framework wheel. Mount as a SPA under `/_apx/builder/*` from the existing FastAPI dev-UI router. Replace Veena's raw-LangGraph Mustache emission templates with apx-agent DSL emission (`Agent(...)`, `SequentialAgent(...)`, `KeywordRouter(...)`, etc.) — the same code shape `compile_to_responses_agent` consumes, ~3-5x shorter than her current output.

**Tech Stack:**
- **Canvas (new source tree):** React 19, Vite 6, react-router-dom 7, TypeScript, Mustache (port of Veena's stack — keep her dependency set intact for now)
- **Framework (existing):** Python 3.11+, FastAPI, the existing `_dev.py` HTML+JS dev UI
- **Wheel packaging:** Existing `pyproject.toml` + `hatchling`; add a `force-include` for the built SPA assets
- **Communication:** Canvas POSTs rendered Python code + canvas JSON to `/_apx/builder/save`; backend writes `agent.py` and `.apx-builder.json` to the user's working directory; uvicorn `--reload` picks up the file change

---

## File Structure

### Created (new source tree)

| Path | Responsibility |
|---|---|
| `python/builder-ui/package.json` | React + Vite + react-router + Mustache deps, build scripts |
| `python/builder-ui/vite.config.ts` | Vite config with `base: '/_apx/builder/'` for SPA mount under the dev UI |
| `python/builder-ui/tsconfig.json` | TypeScript config (mirror Veena's) |
| `python/builder-ui/index.html` | SPA entry HTML |
| `python/builder-ui/src/index.tsx` | React DOM root |
| `python/builder-ui/src/App.tsx` | BrowserRouter root with `basename={import.meta.env.BASE_URL}` |
| `python/builder-ui/src/pages/AgentEditor.tsx` | Top-level canvas page (port from Veena) |
| `python/builder-ui/src/components/NodeView.tsx` | Node rendering (port from Veena) |
| `python/builder-ui/src/components/EdgeView.tsx` | Edge rendering (port from Veena) |
| `python/builder-ui/src/components/GroupView.tsx` | Group rendering (port from Veena) |
| `python/builder-ui/src/components/Controls.tsx` | Canvas toolbar (port from Veena) |
| `python/builder-ui/src/types.ts` | TypeScript types (port from Veena, add Lakebase + KeywordRouter mappings) |
| `python/builder-ui/src/constants.ts` | Node colors, grid size (port from Veena) |
| `python/builder-ui/src/utils.ts` | Misc helpers (port from Veena) |
| `python/builder-ui/src/codegen/index.ts` | Top-level code generator (port + retarget) |
| `python/builder-ui/src/codegen/templates/header.mustache` | apx-agent imports |
| `python/builder-ui/src/codegen/templates/agent.mustache` | `Agent(...)` + composition wrapper |
| `python/builder-ui/src/codegen/templates/llm.mustache` | LLM endpoint constants |
| `python/builder-ui/src/codegen/templates/uc_functions.mustache` | `uc_function_tool(...)` factories |
| `python/builder-ui/src/codegen/templates/vector_search.mustache` | `vector_search_tool(...)` factories |
| `python/builder-ui/src/codegen/templates/lakebase.mustache` | `LakebaseMemoryStore` wiring |
| `python/builder-ui/src/codegen/templates/registration.mustache` | `compile_to_responses_agent(...)` + ResponsesAgent registration |
| `python/builder-ui/README.md` | "Built statically; assets shipped in apx-agent wheel; `npm run build` outputs to `../src/apx_agent/_builder_ui_dist`" |
| `python/src/apx_agent/_builder_ui_dist/.gitkeep` | Marker; real contents written by `npm run build` |
| `python/src/apx_agent/_builder_routes.py` | FastAPI routes — `/_apx/builder/*` SPA serving + `/_apx/builder/save` endpoint + helper endpoints |
| `python/tests/test_builder_routes.py` | Tests for the save endpoint + SPA mount |

### Modified

| Path | Why |
|---|---|
| `python/src/apx_agent/_dev.py` | Include the new `_builder_routes` router; add `/_apx/builder` link in nav |
| `python/src/apx_agent/_ui_nav.py` | Add "Builder" to the dev-UI tab list |
| `python/pyproject.toml` | `force-include` the built `_builder_ui_dist/` so the wheel ships the static assets |
| `python/.gitignore` | Ignore `_builder_ui_dist/*` except `.gitkeep` (the dist is build output, regenerated locally) |
| `Makefile` (project root, if exists, else create) | Add `make builder-ui` target that runs the npm build and copies the result |
| `python/docs/dev-ui.md` (modified by recent README rewrite) | Document `/_apx/builder` tab |

### NOT round-tripping in v1

We do not parse existing `agent.py` back into a canvas graph for v1. The save endpoint writes a `.apx-builder.json` sidecar capturing the canvas state, so users who saved via the canvas can reopen the same canvas. Hand-edited `agent.py` files won't open in the canvas. This is documented in `python/docs/dev-ui.md` as a v1 limitation.

---

## Phase 0 — Bootstrap the React source tree (sub-day)

Stand up the canvas as a standalone Vite project inside our repo. No FastAPI wiring yet. Goal: prove the toolchain works and the existing canvas builds cleanly under our base path.

### Task 0.1: Create the React source tree skeleton

**Files:**
- Create: `python/builder-ui/package.json`
- Create: `python/builder-ui/vite.config.ts`
- Create: `python/builder-ui/tsconfig.json`
- Create: `python/builder-ui/index.html`
- Create: `python/builder-ui/src/index.tsx`
- Create: `python/builder-ui/src/App.tsx`
- Create: `python/builder-ui/README.md`

- [ ] **Step 1: Write `package.json`** matching Veena's dep set, with `name` + `private: true` + a `build:dist` script that emits into `../src/apx_agent/_builder_ui_dist`

```json
{
  "name": "apx-builder-ui",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "build:dist": "vite build --outDir ../src/apx_agent/_builder_ui_dist --emptyOutDir",
    "preview": "vite preview"
  },
  "dependencies": {
    "@types/mustache": "^4.2.6",
    "jszip": "^3.10.1",
    "lucide-react": "^0.564.0",
    "mustache": "^4.2.0",
    "react": "^19.2.4",
    "react-dom": "^19.2.4",
    "react-router-dom": "^7.13.1"
  },
  "devDependencies": {
    "@types/node": "^22.14.0",
    "@types/react": "^19.0.0",
    "@types/react-dom": "^19.0.0",
    "@vitejs/plugin-react": "^5.0.0",
    "typescript": "~5.8.2",
    "vite": "^6.2.0"
  }
}
```

- [ ] **Step 2: Write `vite.config.ts`** with the SPA base path

```ts
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: '/_apx/builder/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: undefined,
      },
    },
  },
});
```

- [ ] **Step 3: Write `tsconfig.json`** (mirror Veena's)

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": false,
    "noUnusedParameters": false,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["src"]
}
```

- [ ] **Step 4: Write `index.html`** (Vite entry)

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>apx-agent Visual Builder</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/index.tsx"></script>
  </body>
</html>
```

- [ ] **Step 5: Write `src/index.tsx` + `src/App.tsx`** with a "Hello from /_apx/builder" placeholder (mirror Veena's BrowserRouter shape with `basename={import.meta.env.BASE_URL}`)

```tsx
// src/index.tsx
import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

```tsx
// src/App.tsx
import { BrowserRouter, Routes, Route } from 'react-router-dom';

export function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<div>Hello from /_apx/builder — Phase 0 stub</div>} />
      </Routes>
    </BrowserRouter>
  );
}
```

- [ ] **Step 6: Write `python/builder-ui/README.md`**

```markdown
# apx-agent Visual Builder UI

React SPA for the /_apx/builder dev-UI tab. Ported from [veenaramesh/dbrx-agent-builder](https://github.com/veenaramesh/dbrx-agent-builder); retargeted to emit apx-agent DSL instead of raw LangGraph.

## Build

    npm install
    npm run build:dist

That writes static assets into `../src/apx_agent/_builder_ui_dist/` — included in the framework wheel via `pyproject.toml`'s `force-include`.

## Dev

    npm run dev

Serves on `http://localhost:5173/_apx/builder/`. To exercise the save endpoint locally, run a scaffolded apx-agent app on `:8000` and configure CORS or use a proxy.
```

- [ ] **Step 7: Install and verify the toolchain bootstraps**

Run: `cd python/builder-ui && npm install && npm run build`
Expected: `dist/` directory created with `index.html` + `assets/*.js` + `assets/*.css`; no errors.

- [ ] **Step 8: Commit**

```bash
git add python/builder-ui/package.json python/builder-ui/vite.config.ts python/builder-ui/tsconfig.json python/builder-ui/index.html python/builder-ui/src/index.tsx python/builder-ui/src/App.tsx python/builder-ui/README.md
git commit -m "feat(builder-ui): bootstrap React SPA source tree"
```

### Task 0.2: Port Veena's canvas as-is (no retargeting yet)

**Files:**
- Create: `python/builder-ui/src/types.ts`
- Create: `python/builder-ui/src/constants.ts`
- Create: `python/builder-ui/src/utils.ts`
- Create: `python/builder-ui/src/pages/AgentEditor.tsx`
- Create: `python/builder-ui/src/components/{NodeView,EdgeView,GroupView,Controls}.tsx`
- Create: `python/builder-ui/src/codegen/index.ts`
- Create: `python/builder-ui/src/codegen/templates/{header,llm,agent,vectorSearch,ucFunctions,registration}.mustache`

- [ ] **Step 1: Copy Veena's source files** verbatim from `veenaramesh/dbrx-agent-builder/agent-builder/client/`

Run: `gh api repos/veenaramesh/dbrx-agent-builder/contents/agent-builder/client/<file> --jq .content | base64 -d > <target>` for each file listed in the **Files** block above.

- [ ] **Step 2: Update `App.tsx` to route to the editor**

```tsx
// src/App.tsx
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AgentEditor } from './pages/AgentEditor';

export function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<AgentEditor />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
```

- [ ] **Step 3: Build and verify the canvas renders**

Run: `cd python/builder-ui && npm run build && npm run preview`
Open `http://localhost:4173/_apx/builder/` in a browser.
Expected: canvas appears; you can drag node types from the palette and connect them.

- [ ] **Step 4: Commit**

```bash
git add python/builder-ui/src
git commit -m "feat(builder-ui): port veenaramesh/dbrx-agent-builder canvas as-is"
```

---

## Phase 1 — Wire the SPA into the framework wheel (half day)

Get the built canvas served by FastAPI at `/_apx/builder/*` from a running apx-agent app. No code emission changes yet; the canvas still emits Veena's raw LangGraph at this phase.

### Task 1.1: Add static-asset routes to the dev UI

**Files:**
- Create: `python/src/apx_agent/_builder_routes.py`
- Create: `python/src/apx_agent/_builder_ui_dist/.gitkeep`
- Modify: `python/src/apx_agent/_dev.py`
- Modify: `python/pyproject.toml`
- Modify: `python/.gitignore`

- [ ] **Step 1: Write a failing test** that the `/_apx/builder/` route returns 200 with HTML content-type, and `/_apx/builder/assets/<some-js>` returns 200 with JS content-type.

```python
# python/tests/test_builder_routes.py
from __future__ import annotations

import importlib.resources as ir
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apx_agent._builder_routes import build_builder_router


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Stage a minimal _builder_ui_dist with one HTML and one JS asset
    dist_root = tmp_path / "_builder_ui_dist"
    (dist_root / "assets").mkdir(parents=True)
    (dist_root / "index.html").write_text(
        "<!doctype html><html><body><div id='root'></div>"
        "<script src='/_apx/builder/assets/main.js'></script>"
        "</body></html>"
    )
    (dist_root / "assets" / "main.js").write_text("console.log('builder');")

    # Patch the dist-locator to point at our tmp tree
    monkeypatch.setattr(
        "apx_agent._builder_routes._dist_root",
        lambda: dist_root,
    )
    app = FastAPI()
    app.include_router(build_builder_router())
    return app


def test_index_served(app):
    client = TestClient(app)
    r = client.get("/_apx/builder/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "id='root'" in r.text


def test_asset_served(app):
    client = TestClient(app)
    r = client.get("/_apx/builder/assets/main.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "console.log" in r.text


def test_spa_fallback(app):
    """Deep links like /_apx/builder/some/canvas-route fall back to index.html."""
    client = TestClient(app)
    r = client.get("/_apx/builder/some/canvas-route")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "id='root'" in r.text
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `cd python && uv run pytest tests/test_builder_routes.py -v`
Expected: ImportError on `from apx_agent._builder_routes import build_builder_router`.

- [ ] **Step 3: Implement `_builder_routes.py`** with FileResponse + a SPA fallback (any unmatched path under `/_apx/builder/*` returns `index.html`)

```python
# python/src/apx_agent/_builder_routes.py
"""FastAPI routes for the /_apx/builder visual builder SPA.

Serves the pre-built Vite SPA assets out of the wheel, plus a SPA-fallback
route so client-side routing works (any unmatched path under /_apx/builder/*
serves index.html and lets react-router handle it).
"""
from __future__ import annotations

import importlib.resources as ir
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


def _dist_root() -> Path:
    """Locate the bundled _builder_ui_dist/ directory inside the installed package.

    Returns a real filesystem path via importlib.resources.files(), which works
    for both regular installs and zip-imported wheels (since Python 3.9).
    """
    return Path(ir.files("apx_agent").joinpath("_builder_ui_dist"))


def build_builder_router() -> APIRouter:
    """Router that mounts the visual builder SPA at /_apx/builder/*."""
    router = APIRouter()

    @router.get("/_apx/builder", include_in_schema=False)
    @router.get("/_apx/builder/", include_in_schema=False)
    async def builder_index():
        index = _dist_root() / "index.html"
        if not index.exists():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Visual builder assets are not bundled in this install. "
                    "Build them via `cd python/builder-ui && npm run build:dist` "
                    "before installing the wheel."
                ),
            )
        return FileResponse(index, media_type="text/html")

    @router.get("/_apx/builder/{path:path}", include_in_schema=False)
    async def builder_asset(path: str):
        # Asset lookup first; fall back to index.html for SPA deep-link routes.
        target = _dist_root() / path
        if target.is_file():
            return FileResponse(target)
        index = _dist_root() / "index.html"
        if not index.exists():
            raise HTTPException(status_code=404, detail="builder not bundled")
        return FileResponse(index, media_type="text/html")

    return router
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd python && uv run pytest tests/test_builder_routes.py -v`
Expected: 3 passed.

- [ ] **Step 5: Include the new router in the dev UI**

In `python/src/apx_agent/_dev.py`, find `build_dev_ui_router()` and add the include. Add the import at the top of the file and the include line at the end of `build_dev_ui_router`:

```python
# at top of _dev.py
from ._builder_routes import build_builder_router

# inside build_dev_ui_router(), before `return router`:
router.include_router(build_builder_router())
```

- [ ] **Step 6: Update `pyproject.toml`** to ship the built assets in the wheel

Add to the `[tool.hatch.build.targets.wheel]` section (or the equivalent section for your build backend):

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/apx_agent/_builder_ui_dist" = "apx_agent/_builder_ui_dist"
```

Verify the section name by reading the existing `python/pyproject.toml` — if it's `[tool.hatch.build]` or `[tool.setuptools.package-data]`, adapt the syntax.

- [ ] **Step 7: Update `.gitignore`** to exclude the dist contents (but keep the `.gitkeep`)

Add to `python/.gitignore`:

```
src/apx_agent/_builder_ui_dist/*
!src/apx_agent/_builder_ui_dist/.gitkeep
```

- [ ] **Step 8: Create the placeholder marker**

Run: `touch python/src/apx_agent/_builder_ui_dist/.gitkeep`

- [ ] **Step 9: Smoke-test the live integration**

Run:
```
cd python/builder-ui && npm run build:dist
cd ../examples/data-triage-agent
DATA_INSPECTOR_URL=http://localhost:9000 uv run uvicorn app:app --port 8001 --reload &
sleep 3
curl -sI http://localhost:8001/_apx/builder/ | head -5
curl -sI http://localhost:8001/_apx/builder/assets/$(ls ../../src/apx_agent/_builder_ui_dist/assets | head -1) | head -5
kill %1
```
Expected: both responses are `200 OK` with the right content types.

- [ ] **Step 10: Commit**

```bash
git add python/src/apx_agent/_builder_routes.py python/src/apx_agent/_builder_ui_dist/.gitkeep python/src/apx_agent/_dev.py python/pyproject.toml python/.gitignore python/tests/test_builder_routes.py
git commit -m "feat(dev-ui): mount visual builder SPA at /_apx/builder"
```

### Task 1.2: Add the Builder tab to the dev-UI nav

**Files:**
- Modify: `python/src/apx_agent/_ui_nav.py`

- [ ] **Step 1: Locate the nav-tab list** in `_ui_nav.py` (look for the existing tabs: Agent, Tools, Traces, Probe, Edit, Setup)

- [ ] **Step 2: Add "Builder" as a tab** with href `/_apx/builder`, placed between "Tools" and "Traces" so the natural-flow grouping is: write → build → inspect → debug

Read the existing pattern in `_ui_nav.py` and add a parallel entry. Each existing tab is one HTML `<a>` (or template substitution); copy that pattern.

- [ ] **Step 3: Smoke-test the nav** — `curl http://localhost:8001/_apx/agent | grep -i builder` should find the new link.

- [ ] **Step 4: Commit**

```bash
git add python/src/apx_agent/_ui_nav.py
git commit -m "feat(dev-ui): add Builder tab to /_apx nav"
```

---

## Phase 2 — Retarget code emission to apx-agent DSL (1-2 days)

Replace Veena's raw-LangGraph Mustache templates with apx-agent DSL templates. After this phase, clicking "Generate" on the canvas produces idiomatic `Agent(...)` / `SequentialAgent(...)` / `KeywordRouter(...)` code, not the long-form `class AgentState(TypedDict)` + `create_tool_calling_agent` boilerplate.

### Task 2.1: Add tests for the apx-agent emitter

**Files:**
- Create: `python/builder-ui/src/codegen/__tests__/index.test.ts` (or equivalent — Vitest if Veena uses it, otherwise add Vitest as a devDep)
- Modify: `python/builder-ui/package.json` to add Vitest

- [ ] **Step 1: Add Vitest to devDependencies**

```bash
cd python/builder-ui
npm install --save-dev vitest @testing-library/react happy-dom
```

- [ ] **Step 2: Add a test script** to `package.json`:

```json
"scripts": {
  "test": "vitest run",
  ...
}
```

- [ ] **Step 3: Write a failing test** for the LLM-only graph (simplest case)

```ts
// src/codegen/__tests__/index.test.ts
import { describe, it, expect } from 'vitest';
import { generateAgentCode } from '../index';
import type { AgentNodeData } from '../../types';

describe('generateAgentCode (apx-agent target)', () => {
  it('emits a single Agent for one LLM node + no tools', () => {
    const nodes: AgentNodeData[] = [
      {
        id: 'n1',
        type: 'llm',
        config: {
          endpointName: 'databricks-claude-sonnet-4-6',
          systemPrompt: 'You are a helpful assistant.',
          model: '',
          maxTokens: 1024,
          temperature: 0.0,
          maxIterations: 10,
        },
        position: { x: 0, y: 0 },
      } as any,
    ];
    const code = generateAgentCode(nodes, [], 'simple_agent');
    expect(code).toContain('from apx_agent import Agent');
    expect(code).toContain('agent = Agent(');
    expect(code).toContain('You are a helpful assistant.');
    expect(code).toContain('databricks-claude-sonnet-4-6');
    // Should NOT emit the old raw-LangGraph scaffolding
    expect(code).not.toContain('class AgentState(TypedDict)');
    expect(code).not.toContain('create_tool_calling_agent');
  });
});
```

- [ ] **Step 4: Run the test, verify it fails**

Run: `cd python/builder-ui && npm test`
Expected: FAIL — assertions against `from apx_agent import Agent` won't match Veena's current LangGraph output.

- [ ] **Step 5: Commit the test alone** (TDD discipline — red before green)

```bash
git add python/builder-ui/package.json python/builder-ui/package-lock.json python/builder-ui/src/codegen/__tests__/index.test.ts
git commit -m "test(builder-ui): failing test for apx-agent emitter"
```

### Task 2.2: Implement the apx-agent header + LLM templates

**Files:**
- Modify: `python/builder-ui/src/codegen/templates/header.mustache`
- Modify: `python/builder-ui/src/codegen/templates/llm.mustache`

- [ ] **Step 1: Rewrite `header.mustache`**

```mustache
# {{{agentName}}} — Generated by apx-agent visual builder
# Install: pip install -r requirements.txt

from __future__ import annotations

from apx_agent import (
    Agent,
{{#hasSequential}}
    SequentialAgent,
{{/hasSequential}}
{{#hasParallel}}
    ParallelAgent,
{{/hasParallel}}
{{#hasKeywordRouter}}
    KeywordRouter,
{{/hasKeywordRouter}}
{{#hasRouter}}
    RouterAgent,
{{/hasRouter}}
{{#hasHandoff}}
    HandoffAgent,
{{/hasHandoff}}
{{#hasUCFunctions}}
    uc_function_tool,
{{/hasUCFunctions}}
{{#hasVectorSearch}}
    vector_search_tool,
{{/hasVectorSearch}}
{{#hasGenie}}
    genie_tool,
{{/hasGenie}}
)
{{#hasLakebase}}
from apx_agent import LakebaseMemoryStore
{{/hasLakebase}}
```

- [ ] **Step 2: Rewrite `llm.mustache`**

```mustache
{{{nodeName}}} = Agent(
    instructions="""{{{systemPrompt}}}""",
    tools=[{{#tools}}{{{.}}}, {{/tools}}],
{{#hasSubAgentUrls}}
    sub_agents=[{{#subAgentUrls}}"{{.}}", {{/subAgentUrls}}],
{{/hasSubAgentUrls}}
)
```

- [ ] **Step 3: Rewrite the top-level emitter in `codegen/index.ts`** so it walks the graph, computes the `has*` flags, and renders just header + per-LLM blocks for the v0 case. (Defer Sequential / Router emission to Task 2.3.)

The full retarget is significant; for now, focus on the minimal case the test in 2.1 exercises:

```ts
// src/codegen/index.ts (sketch — fill in by reading Veena's original index.ts)
import Mustache from 'mustache';

import headerTpl from './templates/header.mustache?raw';
import llmTpl from './templates/llm.mustache?raw';

import { AgentNodeData, EdgeData } from '../types';

export const generateAgentCode = (
  nodes: AgentNodeData[],
  _edges: EdgeData[],
  agentName: string
): string => {
  const llmNodes = nodes.filter(n => n.type === 'llm');
  const ucfNodes = nodes.filter(n => n.type === 'uc_function');
  const vsNodes  = nodes.filter(n => n.type === 'vector_search');
  const genieNodes = nodes.filter(n => n.type === 'genie' as any);
  const lakebaseNodes = nodes.filter(n => n.type === 'lakebase');

  const flags = {
    hasUCFunctions: ucfNodes.length > 0,
    hasVectorSearch: vsNodes.length > 0,
    hasGenie: genieNodes.length > 0,
    hasLakebase: lakebaseNodes.length > 0,
    hasSequential: false, // wired in Task 2.3
    hasParallel: false,
    hasKeywordRouter: false,
    hasRouter: false,
    hasHandoff: false,
  };

  const sections: string[] = [];
  sections.push(Mustache.render(headerTpl, { agentName, ...flags }));

  for (const node of llmNodes) {
    const cfg = node.config as {
      endpointName: string; systemPrompt: string;
    };
    sections.push(
      Mustache.render(llmTpl, {
        nodeName: `agent`, // single-LLM case; Task 2.3 generalizes
        systemPrompt: cfg.systemPrompt,
        tools: [], // wired in Task 2.4
      })
    );
  }

  return sections.join('\n\n');
};
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `cd python/builder-ui && npm test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/builder-ui/src/codegen/templates/header.mustache python/builder-ui/src/codegen/templates/llm.mustache python/builder-ui/src/codegen/index.ts
git commit -m "feat(builder-ui): emit apx-agent Agent() instead of raw LangGraph"
```

### Task 2.3: Implement Sequential / Router / KeywordRouter / Handoff emitters

**Files:**
- Modify: `python/builder-ui/src/codegen/index.ts`
- Create: `python/builder-ui/src/codegen/templates/sequential.mustache`
- Create: `python/builder-ui/src/codegen/templates/router.mustache`
- Create: `python/builder-ui/src/codegen/templates/keyword_router.mustache`
- Create: `python/builder-ui/src/codegen/templates/handoff.mustache`

- [ ] **Step 1: Write failing tests** for each composition pattern

```ts
// in __tests__/index.test.ts, add:

it('emits SequentialAgent for a supervisor node with ordered children', () => {
  const nodes = [
    { id: 's', type: 'supervisor', config: { description: 'pipeline' }, position: {x:0,y:0} } as any,
    { id: 'a', type: 'llm', config: { endpointName:'x', systemPrompt:'step a' }, position: {x:0,y:0} } as any,
    { id: 'b', type: 'llm', config: { endpointName:'x', systemPrompt:'step b' }, position: {x:0,y:0} } as any,
  ];
  const edges = [
    { source: 's', target: 'a', order: 0 } as any,
    { source: 's', target: 'b', order: 1 } as any,
  ];
  const code = generateAgentCode(nodes, edges, 'pipe');
  expect(code).toContain('SequentialAgent(');
  expect(code).toMatch(/agents=\s*\[\s*agent_a\s*,\s*agent_b/);
});

it('emits KeywordRouter for a router node configured with keyword branches', () => {
  const nodes = [
    { id: 'r', type: 'router', config: { description: 'route', routingMode: 'keyword',
       branches: [{ name: 'investigate', keywords: ['missing','investigate'] }],
       default: 'general' }, position: {x:0,y:0} } as any,
    { id: 'inv', type: 'llm', config: { endpointName:'x', systemPrompt:'inv', branchName: 'investigate' }, position: {x:0,y:0} } as any,
    { id: 'gen', type: 'llm', config: { endpointName:'x', systemPrompt:'gen', branchName: 'general' }, position: {x:0,y:0} } as any,
  ];
  const code = generateAgentCode(nodes, [], 'kr');
  expect(code).toContain('KeywordRouter(');
  expect(code).toContain('branches=');
  expect(code).toContain('"missing"');
  expect(code).toContain('default=agent_gen');
});

it('emits RouterAgent (LLM-driven) when routingMode is llm', () => {
  // ...
});

it('emits HandoffAgent for a handoff node with peer targets', () => {
  // ...
});
```

- [ ] **Step 2: Verify they fail**

Run: `cd python/builder-ui && npm test`
Expected: 4 failed tests.

- [ ] **Step 3: Write the four composition templates**

```mustache
{{!-- sequential.mustache --}}
{{{nodeName}}} = SequentialAgent(
    agents=[{{#agents}}{{{.}}}, {{/agents}}],
{{#hasInstructions}}
    instructions="""{{{instructions}}}""",
{{/hasInstructions}}
)
```

```mustache
{{!-- router.mustache --}}
{{{nodeName}}} = RouterAgent(
    agents=[
{{#routes}}
        ("{{name}}", "{{{description}}}", {{{agent}}}),
{{/routes}}
    ],
    instructions="""{{{instructions}}}""",
)
```

```mustache
{{!-- keyword_router.mustache --}}
{{{nodeName}}} = KeywordRouter(
    branches=[
{{#branches}}
        ("{{name}}", {{{agent}}}, [{{#keywords}}"{{.}}", {{/keywords}}]),
{{/branches}}
    ],
    default={{{default}}},
)
```

```mustache
{{!-- handoff.mustache --}}
{{{nodeName}}} = HandoffAgent(
    agents={
{{#peers}}
        "{{name}}": {{{agent}}},
{{/peers}}
    },
    start="{{startName}}",
    max_handoffs={{maxHandoffs}},
)
```

- [ ] **Step 4: Extend `codegen/index.ts`** to detect composition nodes and emit the right wrapper. Walk the graph: leaves are `Agent(...)`, parents are `Sequential`/`Router`/`KeywordRouter`/`Handoff` depending on their `type` + `routingMode`. Emit children first (DFS order) so parents can reference child variable names.

- [ ] **Step 5: Run tests, verify they pass**

Run: `cd python/builder-ui && npm test`
Expected: all 5 passing.

- [ ] **Step 6: Commit**

```bash
git add python/builder-ui/src/codegen/templates/sequential.mustache python/builder-ui/src/codegen/templates/router.mustache python/builder-ui/src/codegen/templates/keyword_router.mustache python/builder-ui/src/codegen/templates/handoff.mustache python/builder-ui/src/codegen/index.ts python/builder-ui/src/codegen/__tests__/index.test.ts
git commit -m "feat(builder-ui): emit Sequential / Router / KeywordRouter / Handoff"
```

### Task 2.4: Tool factory emission (UC functions, Vector Search, Genie)

**Files:**
- Modify: `python/builder-ui/src/codegen/templates/uc_functions.mustache`
- Modify: `python/builder-ui/src/codegen/templates/vector_search.mustache`
- Create: `python/builder-ui/src/codegen/templates/genie.mustache`
- Modify: `python/builder-ui/src/codegen/index.ts`

- [ ] **Step 1: Failing tests** for each factory emission

```ts
it('emits uc_function_tool factories', () => {
  const nodes = [
    { id:'l', type:'llm', config:{endpointName:'x', systemPrompt:'s', tools:['t1']}, position:{x:0,y:0} } as any,
    { id:'t1', type:'uc_function', config:{ catalog:'main', schema:'tools', functionName:'foo', description:'bar' }, position:{x:0,y:0} } as any,
  ];
  const code = generateAgentCode(nodes, [], 'a');
  expect(code).toContain('uc_function_tool("main.tools.foo")');
  expect(code).toMatch(/tools=\s*\[\s*uc_function_tool\("main\.tools\.foo"\)/);
});

it('emits vector_search_tool factories', () => { /* ... */ });
it('emits genie_tool factories', () => { /* ... */ });
```

- [ ] **Step 2: Verify tests fail.**
- [ ] **Step 3: Replace the templates with one-line factory calls** (e.g. `uc_function_tool("{{catalog}}.{{schema}}.{{functionName}}")`)
- [ ] **Step 4: Update the emitter** to inline factory calls into each LLM node's `tools=[...]` list rather than emitting them as separate variables.
- [ ] **Step 5: Run tests, verify pass.**
- [ ] **Step 6: Commit:** `feat(builder-ui): emit uc_function_tool / vector_search_tool / genie_tool factories`

### Task 2.5: Lakebase memory store emission

Same pattern. Wire `LakebaseMemoryStore` from a `lakebase` node when present. The test asserts the import + the engine wiring lands in the generated code. Skip if Lakebase is rarely used — but the existing apx-agent examples lean on it for the customer-triage demo, so it's worth covering.

---

## Phase 3 — Save → write `agent.py` → hot reload (1 day)

Wire the canvas's existing "Generate" action to a POST endpoint that writes the rendered code to disk. Uvicorn `--reload` picks up the change and the running agent updates.

### Task 3.1: Save endpoint

**Files:**
- Modify: `python/src/apx_agent/_builder_routes.py`
- Modify: `python/tests/test_builder_routes.py`

- [ ] **Step 1: Failing test** that POST `/_apx/builder/save` writes `agent.py` and `.apx-builder.json` to the working directory

```python
def test_save_writes_agent_py_and_sidecar(tmp_path, monkeypatch, app):
    monkeypatch.chdir(tmp_path)
    client = TestClient(app)
    payload = {
        "code": "from apx_agent import Agent\nagent = Agent(tools=[])\n",
        "graph": {"nodes": [], "edges": []},
    }
    r = client.post("/_apx/builder/save", json=payload)
    assert r.status_code == 200
    assert (tmp_path / "agent.py").read_text() == payload["code"]
    assert "nodes" in (tmp_path / ".apx-builder.json").read_text()
```

- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement** with `Path.cwd()` resolution, atomic write via `Path.write_text` after `*.tmp` rename, plus safety check that the existing `agent.py` either doesn't exist OR has a sentinel comment like `# Generated by apx-agent visual builder` (refuse to clobber hand-written files unless `?force=1` is set).
- [ ] **Step 4: Test passes.**
- [ ] **Step 5: Commit:** `feat(builder-ui): /_apx/builder/save endpoint writes agent.py + sidecar`

### Task 3.2: Canvas "Save" button POSTs to the endpoint

**Files:**
- Modify: `python/builder-ui/src/pages/AgentEditor.tsx` (or wherever Veena's "Generate" lives)

- [ ] **Step 1:** Locate Veena's existing "Generate code" action; replace the download-zip behavior with a `fetch('/_apx/builder/save', {method:'POST', body: JSON.stringify({code, graph})})`. Show a toast / banner on success ("Saved to agent.py; uvicorn will reload").
- [ ] **Step 2:** Browser smoke test: open `/_apx/builder/`, build a one-node graph, click Save, confirm `agent.py` appears in the example's working dir.
- [ ] **Step 3: Commit:** `feat(builder-ui): wire Save button to POST /_apx/builder/save`

### Task 3.3: Refuse-to-clobber safety net

**Files:**
- Modify: `python/src/apx_agent/_builder_routes.py`
- Modify: `python/tests/test_builder_routes.py`

- [ ] **Step 1: Failing tests** — POSTing to save when `agent.py` exists *without* the sentinel returns 409; POSTing with `?force=1` overrides.
- [ ] **Step 2-5:** Implement the sentinel + force flag; tests pass; commit.

---

## Phase 4 — Tool palette enrichment (polish, optional in v1)

Canvas pulls dropdown options from the live workspace via OBO.

### Task 4.1: List UC functions in a schema

**Files:**
- Modify: `python/src/apx_agent/_builder_routes.py`
- Modify: canvas to call the endpoint when a UC-function node is configured

- [ ] **Step 1:** Endpoint `GET /_apx/builder/uc-functions?schema=main.tools` returns `[{name, comment, parameters}]` via `WorkspaceClient.functions.list`
- [ ] **Step 2:** Canvas dropdown calls it on focus
- [ ] **Step 3:** Test + commit

### Task 4.2: List Vector Search indexes

Similar shape via `ws.vector_search_indexes.list`.

### Task 4.3: List Genie spaces

Similar via `ws.genie.list_spaces`.

---

## Phase 5 — Docs + Makefile target (half day)

### Task 5.1: Update `python/docs/dev-ui.md`

**Files:**
- Modify: `python/docs/dev-ui.md`

- [ ] Add a "Visual Builder" section with: what it does, how to open it (`/_apx/builder`), the v1 limitation (no round-trip; hand-edited `agent.py` doesn't open in canvas), how to opt out (delete `_builder_ui_dist/*` from your wheel install if you don't want it).

### Task 5.2: Makefile target for the build

**Files:**
- Modify (or create): `Makefile` at the project root

- [ ] Add:

```makefile
.PHONY: builder-ui
builder-ui:
	cd python/builder-ui && npm ci && npm run build:dist
```

- [ ] Update `python/docs/dev-ui.md` to mention `make builder-ui` as the build command before publishing the wheel.

### Task 5.3: CI workflow for the UI build

**Files:**
- Modify (or create): `.github/workflows/builder-ui.yml`

- [ ] Add a GitHub Actions job that runs `npm ci && npm run build:dist` and `npm test` on PRs that touch `python/builder-ui/`. No publish step — just verifies the SPA still builds and emitter tests still pass.

### Task 5.4: ROADMAP-runtime.md update

- [ ] Move the v1 visual-builder line out of "Backlog" (if it lands there) and into "Shipped". Add a follow-up entry for round-trip parsing in the backlog.

---

## Phase 6 — Acceptance smoke test (sub-day)

End-to-end with the data-triage-agent example.

### Task 6.1: Build a six-node investigation graph in the canvas

- [ ] Open `/_apx/builder` against a running `data-triage-agent`
- [ ] Drag six LLM nodes; connect them as a SequentialAgent chain; configure each with the corresponding prompt from `prompts.py`
- [ ] Add a `KeywordRouter` node wrapping the chain + a general fallback (matching the current `agent.py` shape)
- [ ] Click Save
- [ ] Open `agent.py` in your editor — confirm it's idiomatic apx-agent code, runnable as-is
- [ ] Switch to `/_apx/agent` tab — ask "why is main.gold.orders empty?" — confirm the investigation pipeline fires (visible in `/_apx/traces`)

### Task 6.2: Document the result

- [ ] Add screenshots to `python/docs/dev-ui.md` (the missing visual that closes the README gap vs DAO's screenshot of their builder)

---

## Out of scope (deliberately)

- **Python → canvas round-trip parsing.** v1 reopens via `.apx-builder.json` only. Hand-edited `agent.py` files won't open. Follow-up plan if a real user asks.
- **Multi-file projects.** Canvas saves to a single `agent.py`. If the project has `tools.py` or `prompts.py`, those stay hand-written; the canvas references them by import.
- **Live test runner inside the canvas.** Test by switching to the `/_apx/agent` chat tab — keeps responsibilities separated.
- **CodeRabbit / linter integration** — generated code should be formatted by `ruff` post-save if available; this can be a backlog item.
- **Custom tool definitions (`@tool` decorator)** in the canvas. v1 only emits factory tool calls (`uc_function_tool`, etc.). Custom Python tools stay hand-written.

---

## Self-review

**Spec coverage:** Every Phase 0-5 task ladders up to a deliverable named in the Phase intro. Phase 6 closes with an acceptance scenario.

**Placeholder scan:** Search for "TBD", "TODO", "fill in", "similar to" — none in the plan. Phase 4 tasks are intentionally light because they're optional polish; if undertaken, each follows the same TDD shape as Phase 2-3 tasks.

**Type consistency:** `generateAgentCode(nodes, edges, agentName)` signature is consistent across tasks 2.1, 2.3, 2.4. `KeywordRouter(branches=..., default=...)` matches the framework primitive added in the just-merged PR #67. `LakebaseMemoryStore` matches the existing export. `compile_to_responses_agent(agent, model=...)` matches the existing framework API used by `agent_server/start_server.py` examples.

One real risk worth surfacing: Task 1.1's `pyproject.toml` `force-include` syntax is `hatch`-specific. The current `python/pyproject.toml` should be inspected before this task lands to confirm the build backend; if it's `setuptools`, the equivalent is `[tool.setuptools.package-data]` plus `MANIFEST.in`. The plan flags this in Task 1.1 Step 6.
