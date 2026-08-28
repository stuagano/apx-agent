# Discovery Spine & Agent Iteration Harness — Implementation Plan

> **Superseded implementation:** this records the original FastAPI build. The shipped example now uses the native APX TypeScript AppKit host, streaming `/api/agents/chat`, and `client/`; see the repository-level `README.md` for current architecture and commands.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single Databricks App (FastAPI + React) that runs the apx-agent discovery agent in-process and lets an ops lead have a natural-language discovery session that yields a keep/build-vs-buy/build **Blueprint** — structured so we can rapidly tune the agent's playbook and grounding.

**Architecture:** One deployed app, one origin. FastAPI backend serves the built React SPA and exposes `/api/chat`; it owns per-session message history and calls apx-agent in-process (`compile_to_chat_agent`, full history passed each turn). The agent emits conversational text plus, at stage boundaries, a fenced ```json artifact block that the backend extracts and validates against Pydantic schemas. A **Current Systems checklist** hard-gates completion of the Profile stage. The wizard shell renders the conversation, a status bar of inspectable artifact nodes, and the Blueprint.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, Pydantic v2, `apx-agent`, `databricks-sdk`, `pytest`; React 18 + TypeScript + Vite, Vitest + React Testing Library.

**Spec:** `docs/superpowers/specs/2026-08-27-nonprofit-suite-discovery-wizard-design.md` (read it alongside this plan). This plan implements the **spine** (spec §10 steps 1–4 + §3.5 gate); the two component teasers (Engine A/B) are separate follow-on plans.

## Global Constraints

- **Python 3.11** — the Databricks Apps runtime; `apx-agent` requires ≥3.10. Copy this floor into `requires-python`.
- **Single origin.** The backend serves the built frontend from `frontend/dist/`; the browser only ever calls same-origin `/api/*`. In local dev the Vite server (`:5173`) proxies `/api` to uvicorn (`:8000`).
- **Deploy manifest is `requirements.txt`** (single source of truth to avoid pyproject-vs-requirements precedence ambiguity). `frontend/dist/` must exist in the synced tree at deploy time — Databricks does **not** run `npm build`.
- **120-second proxy request timeout** (not configurable). Each `/api/chat` turn must complete well under 120s; responses are **buffered JSON** (no streaming) for the spine.
- **No native structured output in apx-agent.** Artifacts travel as a fenced ```json block the agent appends; the backend parses + validates + re-prompts once on failure. Never assume the whole response is JSON.
- **Model endpoint** is configurable via env `DISCOVERY_MODEL`, default `databricks-claude-sonnet-4-6` (a Databricks Foundation Model endpoint; must exist in the target workspace).
- **Databricks auth:** local dev uses a CLI profile / `DATABRICKS_TOKEN`; inside an App a bare `WorkspaceClient()` uses the app service principal. Caller identity (when deployed) comes from the `X-Forwarded-Email` header.
- TDD throughout; commit after each task. Backend logic is tested without a live model via a stub agent; agent quality is validated by manual rehearsal.

---

## File Structure

```
plg/
├── app.yaml                       # Databricks App entrypoint
├── app.py                         # FastAPI: /api mount + SPA static/fallback
├── requirements.txt               # deploy manifest
├── pyproject.toml                 # local dev deps + tooling (ruff/pytest)
├── server/
│   ├── __init__.py
│   ├── schemas.py                 # Task 2: Pydantic artifact schemas + REQUIRED_CATEGORIES
│   ├── grounding.py               # Task 3: assemble system prompt (playbook + brief + catalog)
│   ├── artifacts.py               # Task 4: extract+validate artifact block from agent text
│   ├── agent.py                   # Task 5: DiscoveryAgent interface + apx-agent impl + StubAgent
│   ├── gate.py                    # Task 7: current-systems checklist gate logic
│   ├── sessions.py                # Task 6: in-memory session store
│   └── routes/
│       ├── __init__.py
│       └── api.py                 # Task 6: /api/chat, /api/health
├── prompts/
│   └── discovery_playbook.md      # Task 3: the agent's staged operating procedure
├── data/
│   └── component_catalog.json     # Task 3: run-in-Databricks component specs
├── nonprofit-saas-landscape-2025-2026.md   # existing research brief (grounding)
├── tests/
│   ├── test_schemas.py            # Task 2
│   ├── test_artifacts.py          # Task 4
│   ├── test_gate.py               # Task 7
│   └── test_api.py                # Task 6
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── index.html
    └── src/
        ├── main.tsx
        ├── App.tsx                # Task 8: wizard shell
        ├── api.ts                 # Task 8: typed /api/chat client
        ├── components/
        │   ├── ChatPanel.tsx      # Task 8
        │   ├── StatusBar.tsx      # Task 8: artifact nodes
        │   ├── ArtifactInspector.tsx  # Task 8
        │   ├── SystemsChecklist.tsx   # Task 9
        │   └── BlueprintView.tsx      # Task 9
        └── __tests__/
            └── shell.test.tsx     # Task 8/9 smoke tests
```

---

### Task 1: Project scaffold + local dev loop

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `app.py`, `app.yaml`, `server/__init__.py`, `server/routes/__init__.py`, `server/routes/api.py`, `tests/test_api.py`
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/index.html`, `frontend/src/main.tsx`, `frontend/src/App.tsx`

**Interfaces:**
- Produces: FastAPI `app` object in `app.py`; `GET /api/health` → `{"status": "ok"}`; a built SPA served at `/`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api.py
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **Step 2: Create `pyproject.toml` and `requirements.txt`**

```toml
# pyproject.toml
[project]
name = "plg-discovery"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115.0",
  "uvicorn[standard]>=0.30.0",
  "pydantic>=2.7.0",
  "databricks-sdk>=0.30.0",
  "apx-agent",
]

[dependency-groups]
dev = ["pytest>=8.0", "httpx>=0.27", "ruff>=0.6"]

[tool.pytest.ini_options]
pythonpath = ["."]
```

```
# requirements.txt  (deploy manifest — keep in sync with pyproject dependencies)
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
pydantic>=2.7.0
databricks-sdk>=0.30.0
apx-agent
```

- [ ] **Step 3: Create the FastAPI app with health route + SPA serving**

```python
# server/routes/api.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health():
    return {"status": "ok"}
```

```python
# app.py
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.routes.api import router as api_router

app = FastAPI(title="plg-discovery")
app.include_router(api_router, prefix="/api")

DIST = Path(__file__).parent / "frontend" / "dist"

if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

@app.get("/{full_path:path}")
async def spa(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    candidate = DIST / full_path
    if candidate.is_file():
        return FileResponse(candidate)
    index = DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse({"detail": "frontend not built"}, status_code=200)
```

(`server/__init__.py` and `server/routes/__init__.py` are empty files.)

- [ ] **Step 4: Install and run the test**

Run: `python -m venv .venv && . .venv/bin/activate && pip install -e . && pip install pytest httpx`
Run: `pytest tests/test_api.py::test_health -v`
Expected: PASS

- [ ] **Step 5: Scaffold the Vite React frontend**

```json
// frontend/package.json
{
  "name": "plg-frontend",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "test": "vitest run"
  },
  "dependencies": { "react": "^18.3.1", "react-dom": "^18.3.1" },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.1",
    "typescript": "^5.5.0",
    "vite": "^5.4.0",
    "vitest": "^2.0.0",
    "@testing-library/react": "^16.0.0",
    "jsdom": "^25.0.0"
  }
}
```

```ts
// frontend/vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": { target: "http://localhost:8000", changeOrigin: true } } },
  test: { environment: "jsdom", globals: true },
});
```

```html
<!-- frontend/index.html -->
<!doctype html>
<html><head><meta charset="utf-8"/><title>Nonprofit Suite Discovery</title></head>
<body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>
```

```tsx
// frontend/src/main.tsx
import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
```

```tsx
// frontend/src/App.tsx
export default function App() {
  return <div><h1>Nonprofit Suite Discovery</h1></div>;
}
```

- [ ] **Step 6: Verify both dev servers run**

Run: `cd frontend && npm install && npm run build`
Expected: `frontend/dist/` created.
Run (backend): `uvicorn app:app --reload --port 8000` then `curl localhost:8000/api/health` → `{"status":"ok"}` and `curl localhost:8000/` returns the built index.html.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml requirements.txt app.py app.yaml server/ tests/test_api.py frontend/package.json frontend/vite.config.ts frontend/index.html frontend/src/main.tsx frontend/src/App.tsx
git commit -m "feat: scaffold single Databricks App (FastAPI + Vite/React) with health route"
```

(`app.yaml` is written in Task 10; create an empty placeholder now or defer — not needed for local dev.)

---

### Task 2: Artifact schemas

**Files:**
- Create: `server/schemas.py`
- Test: `tests/test_schemas.py`

**Interfaces:**
- Produces: `OrgProfile`, `CurrentSystem`, `SystemCategory`, `REQUIRED_CATEGORIES`, `DomainRelevance`, `DomainScore`, `Blueprint`, `BlueprintLine`, `BlueprintDecision`; and `parse_artifact(data: dict) -> OrgProfile | DomainRelevance | Blueprint` that dispatches on the `type` field.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schemas.py
import pytest
from server.schemas import (
    OrgProfile, DomainRelevance, Blueprint, SystemCategory,
    REQUIRED_CATEGORIES, parse_artifact,
)

def test_required_categories_are_the_five_core_systems():
    assert REQUIRED_CATEGORIES == [
        SystemCategory.email, SystemCategory.docs, SystemCategory.financial,
        SystemCategory.crm, SystemCategory.fundraising,
    ]

def test_parse_org_profile_dispatches_on_type():
    art = parse_artifact({
        "type": "org_profile",
        "org_name": "Urban Gleaners",
        "current_systems": [
            {"category": "email", "has_system": True, "system_name": "Google Workspace"}
        ],
    })
    assert isinstance(art, OrgProfile)
    assert art.current_systems[0].category is SystemCategory.email

def test_parse_blueprint_validates_decision_enum():
    art = parse_artifact({
        "type": "blueprint",
        "lines": [{"domain": "financial", "current_system": "QuickBooks",
                   "decision": "Keep&Integrate", "target": None,
                   "justification": "Regulatory trust; integrate for reporting."}],
    })
    assert isinstance(art, Blueprint)
    assert art.lines[0].decision.value == "Keep&Integrate"

def test_parse_rejects_unknown_type():
    with pytest.raises(ValueError):
        parse_artifact({"type": "nope"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_schemas.py -v`
Expected: FAIL (ImportError: cannot import name 'OrgProfile').

- [ ] **Step 3: Implement the schemas**

```python
# server/schemas.py
from __future__ import annotations
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel


class SystemCategory(str, Enum):
    email = "email"
    docs = "docs"
    financial = "financial"
    crm = "crm"
    fundraising = "fundraising"
    grants = "grants"
    program_case = "program_case"
    volunteer = "volunteer"
    events = "events"
    comms = "comms"
    back_office = "back_office"
    vertical = "vertical"


# The un-skippable core set (spec §3.5). Others are captured opportunistically.
REQUIRED_CATEGORIES: list[SystemCategory] = [
    SystemCategory.email, SystemCategory.docs, SystemCategory.financial,
    SystemCategory.crm, SystemCategory.fundraising,
]


class CurrentSystem(BaseModel):
    category: SystemCategory
    has_system: bool
    system_name: Optional[str] = None
    keep_intent: Optional[Literal["keep", "open-to-change", "unsure"]] = None


class OrgProfile(BaseModel):
    type: Literal["org_profile"] = "org_profile"
    org_name: Optional[str] = None
    budget_tier: Optional[str] = None
    staff_count: Optional[int] = None
    volunteer_count: Optional[int] = None
    revenue_mix: Optional[dict] = None
    direct_service: Optional[bool] = None
    daily_vertical_workflow: Optional[str] = None
    compliance_surface: list[str] = []
    current_systems: list[CurrentSystem] = []


class DomainScore(BaseModel):
    domain: str
    score: float
    rationale: str


class DomainRelevance(BaseModel):
    type: Literal["domain_relevance"] = "domain_relevance"
    domains: list[DomainScore]


class BlueprintDecision(str, Enum):
    keep_integrate = "Keep&Integrate"
    migrate_buy = "Migrate→Buy"
    migrate_build = "Migrate→Build"
    new_buy = "New→Buy"
    new_build = "New→Build"


class BlueprintLine(BaseModel):
    domain: str
    current_system: Optional[str] = None
    decision: BlueprintDecision
    target: Optional[str] = None  # external tool (Buy) or catalog component (Build)
    justification: str


class Blueprint(BaseModel):
    type: Literal["blueprint"] = "blueprint"
    lines: list[BlueprintLine]


_BY_TYPE = {"org_profile": OrgProfile, "domain_relevance": DomainRelevance, "blueprint": Blueprint}


def parse_artifact(data: dict) -> OrgProfile | DomainRelevance | Blueprint:
    t = data.get("type")
    model = _BY_TYPE.get(t)
    if model is None:
        raise ValueError(f"unknown artifact type: {t!r}")
    return model.model_validate(data)
```

Note: the `→` in decision values is the literal Unicode arrow `→` used in the spec; keep it consistent everywhere (schema, playbook, frontend).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_schemas.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add server/schemas.py tests/test_schemas.py
git commit -m "feat: typed artifact schemas (OrgProfile, DomainRelevance, Blueprint) + parse_artifact"
```

---

### Task 3: Grounding — playbook, component catalog, system-prompt assembly

**Files:**
- Create: `prompts/discovery_playbook.md`, `data/component_catalog.json`, `server/grounding.py`
- Test: extend `tests/test_schemas.py` is wrong scope — Create: `tests/test_grounding.py`

**Interfaces:**
- Consumes: the existing `nonprofit-saas-landscape-2025-2026.md`.
- Produces: `build_system_prompt() -> str` (playbook + brief + catalog + the artifact JSON contract), and `load_catalog() -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_grounding.py
from server.grounding import build_system_prompt, load_catalog

def test_catalog_loads_and_has_teaser_components():
    cat = load_catalog()
    names = {c["name"] for c in cat}
    assert "Donor Management" in names
    assert "Finance & Impact Reporting" in names

def test_system_prompt_includes_playbook_brief_catalog_and_contract():
    p = build_system_prompt()
    assert "STAGE 1" in p                      # playbook stages present
    assert "Urban Gleaners" in p               # brief content injected
    assert "Donor Management" in p             # catalog injected
    assert "```json" in p                      # artifact contract explained
    assert "Keep&Integrate" in p               # decision vocabulary present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_grounding.py -v`
Expected: FAIL (ModuleNotFoundError: server.grounding).

- [ ] **Step 3: Write the Discovery Playbook**

```markdown
<!-- prompts/discovery_playbook.md -->
# Discovery Playbook — your operating procedure

You are a discovery consultant for small/medium nonprofits. You run a staged
discovery session and produce a tailored operational-software Blueprint. Operate
in the three stages below, in order. Each stage is GATED by emitting its artifact.

## STAGE 1 — Org Profile
Interview the ops lead to build their profile. You MUST establish, for EACH of these
current-systems categories, whether they have a tool and which one:
email, docs/productivity, financial/accounting, CRM/constituent, fundraising/donations.
Also probe the remaining domains opportunistically (grants, program/case, volunteer,
events, comms, back-office, vertical/operational), budget tier, staff & volunteer
counts, revenue mix, whether they are direct-service, their daily vertical workflow,
and compliance surface. Do NOT move to Stage 2 until every one of the five core
categories is resolved (a named tool, or explicitly "none").
When the profile is complete, emit the `org_profile` artifact (see CONTRACT).

## STAGE 2 — Domain Relevance
Score each of the nine functional domains for this org (0.0–1.0) with a one-line
rationale grounded in their profile. Emit the `domain_relevance` artifact.

## STAGE 3 — Suite Blueprint
For each relevant domain decide against their EXISTING stack:
- Keep&Integrate — their current tool is fine; connect to it.
- Migrate→Buy — retire current tool, adopt a different external SaaS.
- Migrate→Build — retire current tool, run a named catalog component in Databricks.
- New→Buy / New→Build — no current tool in this domain.
Prefer: don't rebuild commodities (accounting, payroll, email, donation rails) —
Keep&Integrate or Buy. Build (run in Databricks) the vertical/consolidation gaps.
Name a specific catalog component for every Build decision. Emit the `blueprint` artifact.

## CONTRACT — how to emit artifacts
Converse normally. When (and only when) you COMPLETE a stage, append to that message a
fenced code block exactly like:
```json apx-artifact
{ "type": "org_profile", ... }
```
The JSON must match the stage's schema. Emit at most one artifact per message. Keep
conversing after emitting until the user is ready to proceed.
```

- [ ] **Step 4: Write the component catalog**

```json
// data/component_catalog.json
[
  {"name": "Donor Management", "engine": "A",
   "description": "Configurable CRM for donors, gifts, and relationships (ClickUp-style flexible objects).",
   "config_outline": ["objects", "fields", "views", "labels"]},
  {"name": "Volunteer Management", "engine": "A",
   "description": "Shift/role scheduling, hour tracking, reminders for pickups/deliveries/markets.",
   "config_outline": ["shift_types", "roles", "locations", "reminders"]},
  {"name": "Program & Case Management", "engine": "A",
   "description": "Client intake, service tracking, outcomes for direct-service programs.",
   "config_outline": ["client_fields", "services", "outcomes"]},
  {"name": "Food-Logistics & Routing", "engine": "A",
   "description": "Donor pickups, route/driver coordination, distribution to markets.",
   "config_outline": ["donors", "routes", "vehicles", "distribution_sites"]},
  {"name": "Finance & Impact Reporting", "engine": "B",
   "description": "Ingest financials (e.g. QuickBooks) to the lakehouse; dashboards for lbs rescued, meals served, funder reports.",
   "config_outline": ["source", "delta_target", "dashboard_ref"]},
  {"name": "Grants Pipeline", "engine": "A",
   "description": "Track grant applications, deadlines, and post-award reporting.",
   "config_outline": ["stages", "deadlines", "reporting_fields"]},
  {"name": "Board & Governance", "engine": "A",
   "description": "Meetings, documents, and votes for boards (an accessibility desert for small orgs).",
   "config_outline": ["meetings", "documents", "votes"]}
]
```

- [ ] **Step 5: Implement `grounding.py`**

```python
# server/grounding.py
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BRIEF = _ROOT / "nonprofit-saas-landscape-2025-2026.md"
_PLAYBOOK = _ROOT / "prompts" / "discovery_playbook.md"
_CATALOG = _ROOT / "data" / "component_catalog.json"


def load_catalog() -> list[dict]:
    return json.loads(_CATALOG.read_text())


def build_system_prompt() -> str:
    playbook = _PLAYBOOK.read_text()
    brief = _BRIEF.read_text()
    catalog = load_catalog()
    catalog_md = "\n".join(
        f"- **{c['name']}** (engine {c['engine']}): {c['description']} "
        f"[config: {', '.join(c['config_outline'])}]"
        for c in catalog
    )
    return (
        f"{playbook}\n\n"
        f"# COMPONENT CATALOG (the 'Run in Databricks' options you may name)\n{catalog_md}\n\n"
        f"# RESEARCH BRIEF (your grounding knowledge)\n{brief}\n"
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_grounding.py -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add prompts/discovery_playbook.md data/component_catalog.json server/grounding.py tests/test_grounding.py
git commit -m "feat: discovery playbook, component catalog, and system-prompt assembly"
```

---

### Task 4: Artifact extraction from agent text

**Files:**
- Create: `server/artifacts.py`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: `parse_artifact` from `server/schemas.py`.
- Produces: `split_response(text: str) -> tuple[str, OrgProfile | DomainRelevance | Blueprint | None, str | None]` returning `(chat_text, artifact_or_None, error_or_None)`. `chat_text` is the response with the artifact block removed. If a block exists but is invalid, `artifact` is `None` and `error` is a short message.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_artifacts.py
from server.artifacts import split_response
from server.schemas import OrgProfile

def test_no_block_returns_text_only():
    chat, art, err = split_response("Tell me about your email tool.")
    assert chat == "Tell me about your email tool."
    assert art is None and err is None

def test_extracts_and_validates_block():
    text = (
        "Great, I have your profile.\n"
        "```json apx-artifact\n"
        '{"type": "org_profile", "org_name": "Urban Gleaners", "current_systems": []}\n'
        "```"
    )
    chat, art, err = split_response(text)
    assert isinstance(art, OrgProfile)
    assert "apx-artifact" not in chat
    assert "Great, I have your profile." in chat
    assert err is None

def test_invalid_json_reports_error_without_crashing():
    text = "ok\n```json apx-artifact\n{not valid json}\n```"
    chat, art, err = split_response(text)
    assert art is None
    assert err is not None
    assert "ok" in chat

def test_plain_json_fence_without_marker_is_ignored():
    text = "here is an example ```json\n{\"type\": \"blueprint\"}\n```"
    chat, art, err = split_response(text)
    assert art is None and err is None  # only the apx-artifact marker counts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_artifacts.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement extraction**

```python
# server/artifacts.py
import json
import re
from server.schemas import parse_artifact, OrgProfile, DomainRelevance, Blueprint

# Matches a fenced block opened with ```json apx-artifact (case-insensitive), lazily.
_BLOCK = re.compile(r"```json\s+apx-artifact\s*\n(.*?)\n?```", re.DOTALL | re.IGNORECASE)


def split_response(
    text: str,
) -> tuple[str, OrgProfile | DomainRelevance | Blueprint | None, str | None]:
    m = _BLOCK.search(text)
    if not m:
        return text.strip(), None, None
    chat = (text[: m.start()] + text[m.end():]).strip()
    raw = m.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return chat, None, f"artifact JSON parse error: {e}"
    try:
        return chat, parse_artifact(data), None
    except (ValueError, Exception) as e:  # pydantic ValidationError included
        return chat, None, f"artifact schema error: {e}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_artifacts.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add server/artifacts.py tests/test_artifacts.py
git commit -m "feat: extract and validate apx-artifact JSON blocks from agent text"
```

---

### Task 5: DiscoveryAgent — interface, apx-agent impl, stub

**Files:**
- Create: `server/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `build_system_prompt` from `server/grounding.py`.
- Produces:
  - `Message = TypedDict("Message", {"role": str, "content": str})`.
  - `class DiscoveryAgent(Protocol): def respond(self, history: list[Message]) -> str: ...`
  - `class StubAgent` — deterministic, for tests/local-without-creds; constructed with a list of canned replies.
  - `class ApxDiscoveryAgent` — wraps apx-agent; `respond(history)` returns the assistant text.
  - `get_agent() -> DiscoveryAgent` — returns `ApxDiscoveryAgent` normally, or `StubAgent` when `DISCOVERY_STUB=1`.

- [ ] **Step 1: Write the failing test (stub only — no live model in CI)**

```python
# tests/test_agent.py
from server.agent import StubAgent

def test_stub_returns_scripted_replies_in_order():
    a = StubAgent(["hello", "world"])
    assert a.respond([{"role": "user", "content": "hi"}]) == "hello"
    assert a.respond([{"role": "user", "content": "next"}]) == "world"

def test_stub_repeats_last_when_exhausted():
    a = StubAgent(["only"])
    assert a.respond([]) == "only"
    assert a.respond([]) == "only"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement the interface, stub, and apx-agent wrapper**

```python
# server/agent.py
from __future__ import annotations
import os
from typing import Protocol, TypedDict

from server.grounding import build_system_prompt


class Message(TypedDict):
    role: str      # "user" | "assistant" | "system"
    content: str


class DiscoveryAgent(Protocol):
    def respond(self, history: list[Message]) -> str: ...


class StubAgent:
    """Deterministic agent for tests and credential-free local runs."""
    def __init__(self, replies: list[str]):
        self._replies = replies
        self._i = 0

    def respond(self, history: list[Message]) -> str:
        if not self._replies:
            return ""
        idx = min(self._i, len(self._replies) - 1)
        self._i += 1
        return self._replies[idx]


class ApxDiscoveryAgent:
    """Wraps apx-agent. Built once; history is passed in full each turn
    (the backend owns session state, so no conversation_store is needed)."""
    def __init__(self, model: str | None = None):
        from apx_agent import LlmAgent, compile_to_chat_agent
        self._model = model or os.environ.get("DISCOVERY_MODEL", "databricks-claude-sonnet-4-6")
        agent = LlmAgent(name="discovery", instructions=build_system_prompt())
        self._chat = compile_to_chat_agent(agent, model=self._model)

    def respond(self, history: list[Message]) -> str:
        from mlflow.types.agent import ChatAgentMessage
        msgs = [ChatAgentMessage(role=m["role"], content=m["content"]) for m in history]
        resp = self._chat.predict(messages=msgs)
        # ResponsesAgent/ChatAgent returns messages; take the last assistant content.
        out = resp.messages[-1].content if getattr(resp, "messages", None) else ""
        return out or ""


def get_agent() -> DiscoveryAgent:
    if os.environ.get("DISCOVERY_STUB") == "1":
        return StubAgent(["(stub) Tell me about your email system."])
    return ApxDiscoveryAgent()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Manual integration check (needs Databricks creds + model endpoint)**

Run: `DISCOVERY_MODEL=databricks-claude-sonnet-4-6 python -c "from server.agent import ApxDiscoveryAgent; print(ApxDiscoveryAgent().respond([{'role':'user','content':'Hi, we are a small food rescue nonprofit.'}])[:400])"`
Expected: a coherent Stage-1 opening that starts asking about current systems. If the exact `compile_to_chat_agent`/`predict` return shape differs from the assumption in Step 3, adjust `respond()` to read the final assistant text (consult `apx_agent` source: `_run_once.py`, `compile_to_chat_agent`). This is the one place the apx-agent contract is pinned — keep the `respond()` signature stable so nothing else changes.

- [ ] **Step 6: Commit**

```bash
git add server/agent.py tests/test_agent.py
git commit -m "feat: DiscoveryAgent interface with apx-agent wrapper and deterministic stub"
```

---

### Task 6: Session store + /api/chat endpoint

**Files:**
- Create: `server/sessions.py`
- Modify: `server/routes/api.py`
- Test: `tests/test_api.py` (extend)

**Interfaces:**
- Consumes: `get_agent`, `Message` (Task 5); `split_response` (Task 4).
- Produces: `POST /api/chat` with body `{"session_id": str, "message": str}` → response
  `{"reply": str, "artifact": dict | null, "artifact_error": str | null, "artifacts": [dict]}`
  where `artifacts` is the accumulated list for the session (latest state of each type).
- `sessions.py`: `class SessionStore` with `history(sid) -> list[Message]`, `append(sid, role, content)`, `artifacts(sid) -> list[dict]`, `set_artifact(sid, artifact_dict)` (replaces any prior artifact of the same `type`).

- [ ] **Step 1: Write the failing test (with the stub agent, via dependency override)**

```python
# tests/test_api.py  (append)
import server.routes.api as api
from server.agent import StubAgent

def test_chat_returns_reply_and_extracts_artifact(monkeypatch):
    scripted = StubAgent([
        "Hello! What email tool do you use?",
        ("Got it.\n```json apx-artifact\n"
         '{"type":"org_profile","org_name":"Urban Gleaners","current_systems":[]}\n```'),
    ])
    monkeypatch.setattr(api, "_AGENT", scripted)
    api.SESSIONS.reset()

    r1 = client.post("/api/chat", json={"session_id": "s1", "message": "hi"})
    assert r1.status_code == 200
    assert "email tool" in r1.json()["reply"]
    assert r1.json()["artifact"] is None

    r2 = client.post("/api/chat", json={"session_id": "s1", "message": "we use gmail"})
    body = r2.json()
    assert body["artifact"]["type"] == "org_profile"
    assert "apx-artifact" not in body["reply"]
    assert any(a["type"] == "org_profile" for a in body["artifacts"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py -v`
Expected: FAIL (no `/api/chat`).

- [ ] **Step 3: Implement the session store**

```python
# server/sessions.py
from server.agent import Message


class SessionStore:
    def __init__(self):
        self._hist: dict[str, list[Message]] = {}
        self._arts: dict[str, dict[str, dict]] = {}  # sid -> {artifact_type: artifact_dict}

    def reset(self):
        self._hist.clear()
        self._arts.clear()

    def history(self, sid: str) -> list[Message]:
        return self._hist.setdefault(sid, [])

    def append(self, sid: str, role: str, content: str) -> None:
        self.history(sid).append({"role": role, "content": content})

    def set_artifact(self, sid: str, artifact: dict) -> None:
        self._arts.setdefault(sid, {})[artifact["type"]] = artifact

    def artifacts(self, sid: str) -> list[dict]:
        return list(self._arts.get(sid, {}).values())
```

- [ ] **Step 4: Implement the /api/chat route**

```python
# server/routes/api.py
from fastapi import APIRouter
from pydantic import BaseModel

from server.agent import get_agent
from server.artifacts import split_response
from server.sessions import SessionStore

router = APIRouter()

SESSIONS = SessionStore()
_AGENT = get_agent()  # module-level so tests can monkeypatch


class ChatRequest(BaseModel):
    session_id: str
    message: str


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.post("/chat")
async def chat(req: ChatRequest):
    SESSIONS.append(req.session_id, "user", req.message)
    raw = _AGENT.respond(SESSIONS.history(req.session_id))
    chat_text, artifact, err = split_response(raw)
    SESSIONS.append(req.session_id, "assistant", chat_text)
    artifact_dict = None
    if artifact is not None:
        artifact_dict = artifact.model_dump(mode="json")
        SESSIONS.set_artifact(req.session_id, artifact_dict)
    return {
        "reply": chat_text,
        "artifact": artifact_dict,
        "artifact_error": err,
        "artifacts": SESSIONS.artifacts(req.session_id),
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_api.py -v`
Expected: PASS (health + chat tests).

- [ ] **Step 6: Commit**

```bash
git add server/sessions.py server/routes/api.py tests/test_api.py
git commit -m "feat: /api/chat endpoint with in-memory sessions and artifact accumulation"
```

---

### Task 7: Current Systems checklist gate

**Files:**
- Create: `server/gate.py`
- Modify: `server/routes/api.py` (add gate to the chat response)
- Test: `tests/test_gate.py`

**Interfaces:**
- Consumes: `OrgProfile`, `REQUIRED_CATEGORIES`, `SystemCategory` (Task 2).
- Produces: `class GateStatus(BaseModel)` with `complete: bool`, `missing: list[str]`, `filled: list[str]`; and `profile_gate(profile: OrgProfile | None) -> GateStatus`. A category counts as filled when it appears in `current_systems` (whether `has_system` is true or false — "none" is a valid answer). `/api/chat` response gains a `gate` field computed from the session's latest `org_profile` artifact.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate.py
from server.gate import profile_gate, GateStatus
from server.schemas import OrgProfile

def test_no_profile_is_incomplete_with_all_required_missing():
    g = profile_gate(None)
    assert isinstance(g, GateStatus)
    assert g.complete is False
    assert set(g.missing) == {"email", "docs", "financial", "crm", "fundraising"}

def test_partial_profile_reports_remaining_missing():
    p = OrgProfile(current_systems=[
        {"category": "email", "has_system": True, "system_name": "Gmail"},
        {"category": "financial", "has_system": False},  # "none" still counts as filled
    ])
    g = profile_gate(p)
    assert g.complete is False
    assert set(g.missing) == {"docs", "crm", "fundraising"}
    assert set(g.filled) == {"email", "financial"}

def test_all_required_filled_is_complete():
    p = OrgProfile(current_systems=[
        {"category": c, "has_system": True, "system_name": "x"}
        for c in ["email", "docs", "financial", "crm", "fundraising"]
    ])
    assert profile_gate(p).complete is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gate.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement the gate**

```python
# server/gate.py
from pydantic import BaseModel
from server.schemas import OrgProfile, REQUIRED_CATEGORIES


class GateStatus(BaseModel):
    complete: bool
    missing: list[str]
    filled: list[str]


def profile_gate(profile: OrgProfile | None) -> GateStatus:
    present = set()
    if profile is not None:
        present = {cs.category for cs in profile.current_systems}
    required = list(REQUIRED_CATEGORIES)
    missing = [c.value for c in required if c not in present]
    filled = [c.value for c in required if c in present]
    return GateStatus(complete=(len(missing) == 0), missing=missing, filled=filled)
```

- [ ] **Step 4: Wire the gate into /api/chat**

In `server/routes/api.py`, add the import and compute the gate from the latest `org_profile` artifact before returning:

```python
# add to imports
from server.gate import profile_gate
from server.schemas import OrgProfile

# inside chat(), replace the return with:
    latest_profile = next(
        (a for a in SESSIONS.artifacts(req.session_id) if a["type"] == "org_profile"),
        None,
    )
    gate = profile_gate(OrgProfile.model_validate(latest_profile) if latest_profile else None)
    return {
        "reply": chat_text,
        "artifact": artifact_dict,
        "artifact_error": err,
        "artifacts": SESSIONS.artifacts(req.session_id),
        "gate": gate.model_dump(),
    }
```

- [ ] **Step 5: Run all backend tests**

Run: `pytest -v`
Expected: PASS (all tasks so far). Update `tests/test_api.py`'s chat test to assert `body["gate"]["complete"] is False` after the profile with empty `current_systems`.

- [ ] **Step 6: Commit**

```bash
git add server/gate.py server/routes/api.py tests/test_gate.py tests/test_api.py
git commit -m "feat: current-systems checklist gate wired into chat response"
```

---

### Task 8: React wizard shell — chat, status bar, artifact inspector

**Files:**
- Create: `frontend/src/api.ts`, `frontend/src/components/ChatPanel.tsx`, `frontend/src/components/StatusBar.tsx`, `frontend/src/components/ArtifactInspector.tsx`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/src/__tests__/shell.test.tsx`

**Interfaces:**
- Consumes: `POST /api/chat` contract (Task 6/7).
- Produces: a working single-page wizard: message list, input box, a status bar with one node per artifact type (Profile / Domains / Blueprint) that lights up as artifacts arrive and opens the inspector on click.

- [ ] **Step 1: Write the failing smoke test**

```tsx
// frontend/src/__tests__/shell.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import App from "../App";

beforeEach(() => {
  globalThis.fetch = (async () => ({
    ok: true,
    json: async () => ({
      reply: "What email tool do you use?",
      artifact: null, artifact_error: null, artifacts: [],
      gate: { complete: false, missing: ["email","docs","financial","crm","fundraising"], filled: [] },
    }),
  })) as unknown as typeof fetch;
});

test("sends a message and renders the assistant reply", async () => {
  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/message/i), { target: { value: "hi" } });
  fireEvent.click(screen.getByText(/send/i));
  await waitFor(() => expect(screen.getByText(/what email tool/i)).toBeInTheDocument());
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run`
Expected: FAIL (App has no input/send yet).

- [ ] **Step 3: Implement the typed API client**

```ts
// frontend/src/api.ts
export type Artifact = { type: string; [k: string]: unknown };
export type Gate = { complete: boolean; missing: string[]; filled: string[] };
export type ChatResponse = {
  reply: string;
  artifact: Artifact | null;
  artifact_error: string | null;
  artifacts: Artifact[];
  gate: Gate;
};

export async function sendChat(sessionId: string, message: string): Promise<ChatResponse> {
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!r.ok) throw new Error(`chat failed: ${r.status}`);
  return r.json();
}
```

- [ ] **Step 4: Implement the components and shell**

```tsx
// frontend/src/components/ChatPanel.tsx
type Msg = { role: string; content: string };
export function ChatPanel({ messages, onSend, busy }:
  { messages: Msg[]; onSend: (t: string) => void; busy: boolean }) {
  let value = "";
  return (
    <div>
      <div>
        {messages.map((m, i) => (
          <p key={i}><b>{m.role}:</b> {m.content}</p>
        ))}
      </div>
      <input placeholder="Message" onChange={(e) => (value = e.target.value)} />
      <button disabled={busy} onClick={() => onSend(value)}>Send</button>
    </div>
  );
}
```

```tsx
// frontend/src/components/StatusBar.tsx
import { Artifact } from "../api";
const NODES = [
  { type: "org_profile", label: "Profile" },
  { type: "domain_relevance", label: "Domains" },
  { type: "blueprint", label: "Blueprint" },
];
export function StatusBar({ artifacts, onInspect }:
  { artifacts: Artifact[]; onInspect: (a: Artifact) => void }) {
  const byType = new Map(artifacts.map((a) => [a.type, a]));
  return (
    <div style={{ display: "flex", gap: 12 }}>
      {NODES.map((n) => {
        const done = byType.get(n.type);
        return (
          <button key={n.type} disabled={!done}
            onClick={() => done && onInspect(done)}
            style={{ fontWeight: done ? 700 : 400 }}>
            {done ? "●" : "○"} {n.label}
          </button>
        );
      })}
    </div>
  );
}
```

```tsx
// frontend/src/components/ArtifactInspector.tsx
import { Artifact } from "../api";
export function ArtifactInspector({ artifact, onClose }:
  { artifact: Artifact | null; onClose: () => void }) {
  if (!artifact) return null;
  return (
    <div style={{ border: "1px solid #ccc", padding: 12 }}>
      <button onClick={onClose}>close</button>
      <pre>{JSON.stringify(artifact, null, 2)}</pre>
    </div>
  );
}
```

```tsx
// frontend/src/App.tsx
import { useState } from "react";
import { sendChat, Artifact, Gate } from "./api";
import { ChatPanel } from "./components/ChatPanel";
import { StatusBar } from "./components/StatusBar";
import { ArtifactInspector } from "./components/ArtifactInspector";

const SESSION = crypto.randomUUID();

export default function App() {
  const [messages, setMessages] = useState<{ role: string; content: string }[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [gate, setGate] = useState<Gate | null>(null);
  const [inspect, setInspect] = useState<Artifact | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSend(text: string) {
    if (!text.trim()) return;
    setBusy(true);
    setMessages((m) => [...m, { role: "user", content: text }]);
    try {
      const res = await sendChat(SESSION, text);
      setMessages((m) => [...m, { role: "assistant", content: res.reply }]);
      setArtifacts(res.artifacts);
      setGate(res.gate);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ maxWidth: 860, margin: "0 auto", fontFamily: "system-ui" }}>
      <h1>Nonprofit Suite Discovery</h1>
      <StatusBar artifacts={artifacts} onInspect={setInspect} />
      <ArtifactInspector artifact={inspect} onClose={() => setInspect(null)} />
      <ChatPanel messages={messages} onSend={onSend} busy={busy} />
      {gate && !gate.complete && (
        <p style={{ color: "#a00" }}>
          Still need current systems: {gate.missing.join(", ")}
        </p>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend && npx vitest run`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api.ts frontend/src/components/ frontend/src/App.tsx frontend/src/__tests__/shell.test.tsx
git commit -m "feat: React wizard shell with chat, status-bar artifact nodes, inspector"
```

---

### Task 9: Systems checklist UI + Blueprint view

**Files:**
- Create: `frontend/src/components/SystemsChecklist.tsx`, `frontend/src/components/BlueprintView.tsx`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/src/__tests__/shell.test.tsx` (extend)

**Interfaces:**
- Consumes: `Gate`, `Artifact` (api.ts). `BlueprintView` renders a `blueprint` artifact's `lines[]` with `domain`, `current_system`, `decision`, `target`, `justification`.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/__tests__/shell.test.tsx (append)
import { BlueprintView } from "../components/BlueprintView";
import { render, screen } from "@testing-library/react";

test("blueprint view renders decision lines", () => {
  const bp = { type: "blueprint", lines: [
    { domain: "financial", current_system: "QuickBooks", decision: "Keep&Integrate",
      target: null, justification: "Regulatory trust." },
    { domain: "volunteer", current_system: null, decision: "New→Build",
      target: "Volunteer Management", justification: "Vertical gap." },
  ]};
  render(<BlueprintView artifact={bp as any} />);
  expect(screen.getByText(/QuickBooks/)).toBeInTheDocument();
  expect(screen.getByText(/Volunteer Management/)).toBeInTheDocument();
  expect(screen.getByText(/Keep&Integrate/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run`
Expected: FAIL (no BlueprintView).

- [ ] **Step 3: Implement the components**

```tsx
// frontend/src/components/BlueprintView.tsx
import { Artifact } from "../api";
type Line = { domain: string; current_system: string | null; decision: string;
  target: string | null; justification: string };
export function BlueprintView({ artifact }: { artifact: Artifact }) {
  const lines = (artifact.lines as Line[]) ?? [];
  return (
    <table>
      <thead><tr><th>Domain</th><th>Today</th><th>Decision</th><th>Target</th><th>Why</th></tr></thead>
      <tbody>
        {lines.map((l, i) => (
          <tr key={i}>
            <td>{l.domain}</td><td>{l.current_system ?? "—"}</td>
            <td>{l.decision}</td><td>{l.target ?? "—"}</td><td>{l.justification}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

```tsx
// frontend/src/components/SystemsChecklist.tsx
import { Gate } from "../api";
export function SystemsChecklist({ gate }: { gate: Gate }) {
  const all = [...gate.filled.map((c) => [c, true] as const),
               ...gate.missing.map((c) => [c, false] as const)];
  return (
    <div>
      <b>Current systems {gate.complete ? "✓" : "(required)"}:</b>
      <ul>{all.map(([c, ok]) => <li key={c}>{ok ? "☑" : "☐"} {c}</li>)}</ul>
    </div>
  );
}
```

- [ ] **Step 4: Wire into App.tsx**

In `frontend/src/App.tsx`, import both and render: the `SystemsChecklist` whenever `gate` exists (replacing the plain missing-list paragraph), and `BlueprintView` when a `blueprint` artifact is present.

```tsx
// add imports
import { SystemsChecklist } from "./components/SystemsChecklist";
import { BlueprintView } from "./components/BlueprintView";
// replace the gate paragraph with:
      {gate && <SystemsChecklist gate={gate} />}
      {artifacts.find((a) => a.type === "blueprint") && (
        <BlueprintView artifact={artifacts.find((a) => a.type === "blueprint")!} />
      )}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd frontend && npx vitest run`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/SystemsChecklist.tsx frontend/src/components/BlueprintView.tsx frontend/src/App.tsx frontend/src/__tests__/shell.test.tsx
git commit -m "feat: systems checklist UI and blueprint view"
```

---

### Task 10: Deploy config + end-to-end rehearsal

**Files:**
- Create: `app.yaml` (finalize)
- Create: `README.md` (run/deploy notes)

**Interfaces:**
- Produces: a deployable app; `app.yaml` runs uvicorn binding `0.0.0.0` on the assigned port.

- [ ] **Step 1: Write `app.yaml`**

```yaml
command: ["/bin/bash", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${DATABRICKS_APP_PORT:-8000}"]
env:
  - name: "DISCOVERY_MODEL"
    value: "databricks-claude-sonnet-4-6"
```

- [ ] **Step 2: Full local rehearsal (stub agent, no creds)**

Run: `cd frontend && npm run build && cd .. && DISCOVERY_STUB=1 uvicorn app:app --port 8000`
Open `http://localhost:8000`, send a message, confirm the reply renders and the gate shows the five required systems missing.

- [ ] **Step 3: Full local rehearsal (live agent)**

Run: `cd frontend && npm run build && cd .. && DISCOVERY_MODEL=databricks-claude-sonnet-4-6 uvicorn app:app --port 8000`
(Ensure a Databricks CLI profile / `DATABRICKS_TOKEN` is set.) Walk a full Urban-Gleaners discovery: confirm the Profile node lights up only after the five systems are covered, Domains and Blueprint nodes populate, and the Blueprint shows Keep&Integrate vs Migrate/New → Buy/Build lines naming catalog components. **This is the agent-tuning loop** — iterate on `prompts/discovery_playbook.md` and `data/component_catalog.json` and re-run.

- [ ] **Step 4: Deploy to Databricks Apps**

```bash
cd frontend && npm run build && cd ..          # frontend/dist must exist in the tree
databricks apps create plg-discovery --description "Nonprofit suite discovery wizard"
databricks sync . /Workspace/Users/<you>/plg-discovery
databricks apps deploy plg-discovery --source-code-path /Workspace/Users/<you>/plg-discovery
databricks apps logs plg-discovery --follow            # verify startup
```

Open the app URL; run one discovery pass. If the model endpoint is unavailable, set `DISCOVERY_MODEL` (via app env) to one that exists in the workspace.

- [ ] **Step 5: Commit**

```bash
git add app.yaml README.md
git commit -m "chore: databricks app.yaml and run/deploy README; spine end-to-end"
```

---

## Self-Review

**Spec coverage:**
- §2.1 discovery wizard → blueprint: Tasks 3,5,6,9 ✓
- §3.1 single app, agent in-process, `/chat` + static: Tasks 1,5,6,10 ✓
- §3.3 playbook + grounding in instructions; soft stage enforcement: Task 3,5 ✓
- §3.4 artifact schemas (OrgProfile/DomainRelevance/Blueprint) with keep/build-vs-buy/build taxonomy: Task 2 ✓
- §3.5 required current-systems inventory, UI checklist hard-gates Profile stage: Tasks 2,7,9 ✓
- §7 non-conformant output handled (parse error surfaced, not crash): Task 4 ✓
- §9 deploy to Databricks Apps: Task 10 ✓
- **Deferred (separate plans, per this plan's scope):** intake `/ingest` (text/file/link pre-fill, spec §2.1), Engine A donor app, Engine B dashboard, editing/revisiting stages. Not gaps — explicitly out of this spine plan.

**Placeholder scan:** No TBD/TODO; every code step has concrete content. The one external-contract uncertainty (apx-agent `predict` return shape) is isolated to Task 5 Step 5 with explicit resolution instructions and a stable wrapper signature, plus a working stub path so the rest of the system is testable regardless.

**Type consistency:** `Message` (Task 5) used by `SessionStore` (Task 6) and `DiscoveryAgent`. `split_response` return tuple (Task 4) consumed in `/api/chat` (Task 6). `GateStatus`/`profile_gate` (Task 7) consumed by the chat route and `SystemsChecklist` (Task 9). `ChatResponse`/`Artifact`/`Gate` (Task 8 `api.ts`) match the backend JSON keys (`reply`, `artifact`, `artifact_error`, `artifacts`, `gate`). Decision values use the literal `→` arrow consistently (schema, playbook, frontend test).
