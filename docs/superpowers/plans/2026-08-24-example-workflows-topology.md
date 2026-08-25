# Example Workflows in APX Topology Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the APX default Chat and Topology pages runnable, plain-English examples of how a business question moves through coordinated agents, with Curinos as the first reference implementation.

**Architecture:** Add an optional, domain-neutral workflow declaration to APX agent metadata and expose it additively in the topology response. The existing `/responses` stream and trace store remain the only execution path and source of observed status. Curinos converts its existing immutable coordination manifest into the shared workflow shape and uses the same declarations for its semantic topology and the APX default pages.

**Tech Stack:** Python 3.11+, Pydantic, FastAPI, pytest, React 18, TypeScript, Vite, `@xyflow/react`, Dagre, Curinos Python coordination runtime, synthetic fixture data, Databricks Apps.

**Spec:** `docs/superpowers/specs/2026-08-24-example-workflows-topology-design.md`

## Global Constraints

- No new dispatcher, routing engine, or execution path; reuse Chat, `/responses`, coordination APIs, A2A handoffs, and the trace store.
- Workflow metadata is static application configuration and is never authorization or data-result evidence.
- Execution status is derived only from the existing trace/response path; declared routes must never be presented as executed.
- `AgentConfig.examples` and the existing topology `rootId`/`agentName`/`nodes`/`edges` contract remain backward-compatible.
- Curinos remains synthetic for the demo; production UC/OBO scope stays runtime-derived and account scope remains fail-closed outside declared fixture accounts.
- Use the smallest existing helpers and UI patterns; do not add a new state store, graph engine, or customer-specific logic to the APX SDK.
- Escape all user-visible workflow text through the existing rendering helpers and never expose tokens, raw authorization context, or unbounded tool arguments.

---

### Task 1: Define and normalize the APX workflow contract

**Files:**
- Modify: `python/src/apx_agent/_models.py:301-390`
- Modify: `python/src/apx_agent/_project_gen.py:90-145`
- Test: `python/tests/test_inspection.py`
- Test: `python/tests/test_project_gen.py`
- Create: `python/tests/test_models.py`

**Interfaces:**
- Produces `WorkflowHandoff`, `ExampleWorkflow`, and `AgentConfig.workflows`.
- Produces `workflow_prompts(config: AgentConfig) -> list[str]`, which returns existing `examples` followed by unique workflow questions in declaration order.
- Produces `workflows_for_context(ctx: AgentContext) -> list[ExampleWorkflow]`, which uses `ctx.config.workflows` first and falls back to the optional app-owned `ctx.agent.__apx_workflows__` metadata hook.
- Produces `normalize_workflows(value: object) -> list[ExampleWorkflow]`, used by config loading, the context hook, and tests.

- [ ] **Step 1: Write failing model and config tests.** Add tests that validate the approved JSON/TOML shape, reject blank workflow IDs/questions/routes, preserve handoff defaults, and merge starter prompts without duplicates:

```python
def test_workflow_config_round_trips_and_merges_prompts() -> None:
    config = AgentConfig.model_validate({
        "name": "demo",
        "examples": ["Show me the pricing evidence"],
        "workflows": [{
            "id": "pricing-review",
            "title": "Pricing review",
            "question": "Show me the pricing evidence",
            "purpose": "Move from signal to decision.",
            "route": ["intelligence", "calibrate"],
            "outcome": "Reviewable pricing packet",
        }],
    })

    assert config.workflows[0].handoffs == []
    assert workflow_prompts(config) == ["Show me the pricing evidence"]


def test_workflow_rejects_blank_route_stage() -> None:
    with pytest.raises(ValidationError, match="route"):
        AgentConfig.model_validate({
            "name": "demo",
            "workflows": [{
                "id": "bad",
                "title": "Bad",
                "question": "A question",
                "purpose": "A purpose",
                "route": [""],
            }],
        })
```

- [ ] **Step 2: Run the focused tests and verify the failure.**

Run: `cd python && uv run pytest tests/test_inspection.py tests/test_project_gen.py tests/test_models.py -k workflow -q`

Expected: FAIL because the workflow models, config field, and prompt merge helper do not exist.

- [ ] **Step 3: Implement the minimal Pydantic models.** Add immutable-enough validated models with these fields:

```python
class WorkflowHandoff(BaseModel):
    source: str
    target: str
    input_contract: str
    output_contract: str
    explanation: str


class ExampleWorkflow(BaseModel):
    id: str
    title: str
    question: str
    purpose: str
    route: list[str]
    handoffs: list[WorkflowHandoff] = Field(default_factory=list)
    outcome: str = ""
    follow_ups: list[str] = Field(default_factory=list)
```

Validate every string with the repository's existing nonblank validator pattern, require at least one route stage, and add `workflows: list[ExampleWorkflow] = Field(default_factory=list)` to `AgentConfig`. Keep the Pydantic model's serialized keys exactly aligned with the spec. Add `workflows_for_context(ctx)` beside the model helpers so the default pages have one source-resolution path for config-backed and app-attached declarations.

- [ ] **Step 4: Add prompt merging and generator coverage.** Implement `workflow_prompts` with insertion-order de-duplication. Update `_project_gen.py` so generated `[tool.apx.agent]` TOML writes `workflows` when non-empty and omits it when empty. Do not change generated output for agents that have no workflows.

- [ ] **Step 5: Run focused tests and read the generated artifact.**

Run: `cd python && uv run pytest tests/test_inspection.py tests/test_project_gen.py tests/test_models.py -k workflow -q`

Expected: all workflow tests pass. Also assert the generated `pyproject.toml` contains `pricing-review`, `route`, and `handoffs` when configured, and contains no `workflows` key for the empty case.

- [ ] **Step 6: Commit the contract slice.**

```bash
git add python/src/apx_agent/_models.py python/src/apx_agent/_project_gen.py python/tests/test_inspection.py python/tests/test_project_gen.py python/tests/test_models.py
git commit -m "feat: add declarative example workflows"
```

---

### Task 2: Expose workflows in topology data without changing the graph

**Files:**
- Modify: `python/src/apx_agent/_topology.py:495-700`
- Modify: `python/src/apx_agent/_apx_models.py:539-585`
- Test: `python/tests/test_topology.py:300-340 and 340-610`
- Test: `python/tests/test_dev_ui_route_coverage.py:733-781`

**Interfaces:**
- `build_topology(ctx)` returns the existing graph plus `workflows: list[dict[str, object]]`.
- `annotate_topology(..., workflows: Sequence[Mapping[str, Any]] | None = None)` copies an optional workflow collection without mutating its input.
- `TopologyResponse` accepts/serializes the optional `workflows` field.
- `build_topology(ctx)` obtains declarations through `workflows_for_context(ctx)`, so an app can attach manifest-backed workflows without changing APX's graph walker.

- [ ] **Step 1: Add failing backend tests.** Cover config-backed workflows, no-workflow backward compatibility, and non-mutating semantic annotation:

```python
def test_build_topology_includes_declared_workflows() -> None:
    ctx = _context_with_config(workflows=[{
        "id": "pricing-review",
        "title": "Pricing review",
        "question": "How is pricing positioned?",
        "purpose": "Compare the product with peers.",
        "route": ["intelligence", "calibrate"],
    }])

    topology = build_topology(ctx)

    assert topology["workflows"][0]["id"] == "pricing-review"
    baseline = build_topology(_context_with_config())
    assert topology["nodes"] == baseline["nodes"]
    assert topology["edges"] == baseline["edges"]


def test_annotate_topology_copies_workflows_without_mutating_input() -> None:
    base = {"nodes": [], "edges": []}
    annotated = annotate_topology(base, workflows=[{"id": "one", "route": ["a"]}])

    assert annotated["workflows"] == [{"id": "one", "route": ["a"]}]
    assert "workflows" not in base
```

The intent is that nodes and edges are byte-for-byte unchanged when workflows are absent.

- [ ] **Step 2: Run the focused topology tests and verify the failure.**

Run: `cd python && uv run pytest tests/test_topology.py tests/test_dev_ui_route_coverage.py -k workflow -q`

Expected: FAIL because the topology payload and annotation helper do not yet emit workflows.

- [ ] **Step 3: Implement topology serialization.** Add one private serializer that calls `model_dump(mode="json")` for `ExampleWorkflow` values and accepts already-serialized mappings for application-owned annotations. Add the serialized collection at the top level after `edges`. Do not alter node IDs, edge IDs, route highlighting, or the 503 behavior when agent context is absent.

- [ ] **Step 4: Add response-model coverage.** Ensure `TopologyResponse.model_validate` accepts both the old graph-only response and the new response with workflows. Keep extra top-level fields accepted for forward compatibility.

- [ ] **Step 5: Run and read back the JSON contract.**

Run: `cd python && uv run pytest tests/test_topology.py tests/test_dev_ui_route_coverage.py -k 'workflow or topology_json' -q`

Expected: PASS, with the graph-only fixture still returning valid old-shape data and the workflow fixture returning the exact declared fields.

- [ ] **Step 6: Commit the topology-data slice.**

```bash
git add python/src/apx_agent/_topology.py python/src/apx_agent/_apx_models.py python/tests/test_topology.py python/tests/test_dev_ui_route_coverage.py
git commit -m "feat: expose workflows in topology metadata"
```

---

### Task 3: Render workflow prompts on the APX Chat landing page

**Files:**
- Modify: `python/src/apx_agent/_ui_chat.py:660-755 and 1111`
- Test: `python/tests/test_chat_agent.py`
- Test: `python/tests/test_dev_ui_reality_ctk.py`

**Interfaces:**
- The landing renderer consumes `workflow_prompts(ctx.config)` and `ctx.config.workflows`.
- Existing `useExample` behavior continues to submit the exact question string.

- [ ] **Step 1: Add failing landing-page tests.** Render a config with one workflow and one duplicate legacy example, then assert the response contains the workflow title/purpose/question once and does not contain raw unescaped HTML from a malicious question.

```python
def test_chat_landing_renders_workflow_examples_once_and_escapes_text() -> None:
    html = _render_landing(_context_with_config(
        examples=["What is the position?"],
        workflows=[{
            "id": "position",
            "title": "<Pricing review>",
            "question": "What is the position?",
            "purpose": "Compare <b>peers</b>.",
            "route": ["calibrate"],
        }],
    ))

    assert html.count("What is the position?") == 1
    assert "&lt;Pricing review&gt;" in html
    assert "Compare &lt;b&gt;peers&lt;/b&gt;." in html
    assert "<b>peers</b>" not in html
```

- [ ] **Step 2: Run the focused UI test and verify the failure.**

Run: `cd python && uv run pytest tests/test_chat_agent.py tests/test_dev_ui_reality_ctk.py -k workflow -q`

Expected: FAIL because the landing page only reads `ctx.config.examples`.

- [ ] **Step 3: Implement the additive landing-page rendering.** Keep the existing capability cards and legacy starter chips. Add workflow chips that display `title`, `purpose`, and the question in escaped markup, with `data-q` set to the exact question. Use the shared prompt-merging helper so duplicate legacy examples do not render twice.

- [ ] **Step 4: Run the focused tests and inspect the HTML.**

Run: `cd python && uv run pytest tests/test_chat_agent.py tests/test_dev_ui_reality_ctk.py -k workflow -q`

Expected: PASS, with escaped content and one clickable starter for each unique question.

- [ ] **Step 5: Commit the Chat slice.**

```bash
git add python/src/apx_agent/_ui_chat.py python/tests/test_chat_agent.py python/tests/test_dev_ui_reality_ctk.py
git commit -m "feat: show workflow examples in chat"
```

---

### Task 4: Add the runnable workflow rail to APX Topology

**Files:**
- Modify: `python/dev-ui/topology/src/types.ts`
- Modify: `python/dev-ui/topology/src/App.tsx`
- Modify: `python/dev-ui/topology/src/ChatDock.tsx`
- Create: `python/dev-ui/topology/src/WorkflowPanel.tsx`
- Modify: `python/dev-ui/topology/src/index.css`
- Modify: `python/dev-ui/topology/src/sample-topology.json`

**Interfaces:**
- Add TypeScript types matching the Python contract:

```ts
export interface WorkflowHandoff {
  source: string;
  target: string;
  input_contract: string;
  output_contract: string;
  explanation: string;
}

export interface ExampleWorkflow {
  id: string;
  title: string;
  question: string;
  purpose: string;
  route: string[];
  handoffs: WorkflowHandoff[];
  outcome: string;
  follow_ups: string[];
}

export interface TopologyResponse {
  rootId: string;
  agentName: string;
  nodes: TopoNode[];
  edges: TopoEdge[];
  workflows?: ExampleWorkflow[];
}
```

- `WorkflowPanel` accepts `{ workflows, selectedWorkflowId, routeNodeIds, routeEdgeIds, onSelect, onRun }` and does not invoke the network itself.
- `ChatDock` accepts an optional `starterQuestion`/`onRunQuestion` callback or a controlled `send(text)` callback so `Run example` uses the existing `/responses` streaming implementation.
- `App` owns selected workflow state, active run state, and the existing trace polling state.

- [ ] **Step 1: Extend the dev fixture and TypeScript types.** Add one two-stage workflow to `sample-topology.json` and make `workflows` optional so older fixtures still compile.

- [ ] **Step 2: Add the workflow panel component.** Render cards for the question, purpose, route, outcome, and buttons. `View route` changes selection and uses a declared-only visual state. `Run example` calls the existing ChatDock send path. The panel must show “Declared route — not yet run” until trace evidence is available.

- [ ] **Step 3: Add observed-status derivation in `App.tsx`.** Keep status local and deterministic:

```ts
type WorkflowStatus = "declared" | "active" | "partial" | "completed" | "failed";

function workflowStatus(
  workflow: ExampleWorkflow,
  observedNodeIds: ReadonlySet<string>,
  failed: boolean,
): WorkflowStatus {
  if (failed) return "failed";
  const observed = workflow.route.filter((stage) => observedNodeIds.has(stage));
  if (observed.length === 0) return "declared";
  if (observed.length === workflow.route.length) return "completed";
  return "partial";
}
```

Resolve each logical route stage against `data.nodes` by exact ID first and case-insensitive label second. Use the resolved IDs with the existing route highlight sets; when a stage has no graph match, display the declared logical route and observed trace route separately instead of guessing. A failed response should render `failed` on the selected workflow without changing other cards.

- [ ] **Step 4: Wire the panel above the graph and beside the inspector.** Keep the current graph, node selection, wire-drop behavior, tracing controls, and ChatDock. Do not introduce a second chat client or a second trace poller.

- [ ] **Step 5: Add responsive styling and accessibility.** Use existing dark-theme variables, keyboard-reachable buttons, visible focus states, semantic headings, and text that remains readable when the inspector is open. Do not add a UI framework.

- [ ] **Step 6: Build the UI and read back the shipped bundle.**

Run: `cd python/dev-ui/topology && npm run build`

Expected: TypeScript and Vite pass, and `python/src/apx_agent/_static/topology/index.html` references the newly generated asset bundle. Run `cd python && uv run pytest tests/test_packaging.py -q` to prove the bundle remains packaged.

- [ ] **Step 7: Commit the Topology UI slice.**

```bash
git add python/dev-ui/topology/src/types.ts python/dev-ui/topology/src/App.tsx python/dev-ui/topology/src/ChatDock.tsx python/dev-ui/topology/src/WorkflowPanel.tsx python/dev-ui/topology/src/index.css python/dev-ui/topology/src/sample-topology.json python/src/apx_agent/_static/topology
git commit -m "feat: make topology workflows runnable"
```

---

### Task 5: Convert Curinos manifest metadata into shared workflow declarations

**Files:**
- Modify: `Curinos/curinos-one-agent/coordination/manifest.py:118-290`
- Modify: `Curinos/curinos-one-agent/coordination/topology.py:1-260`
- Modify: `Curinos/curinos-one-agent/agent.py:225-270`
- Create: `Curinos/curinos-one-agent/coordination/workflows.py`
- Test: `Curinos/curinos-one-agent/tests/coordination/test_manifest.py`
- Test: `Curinos/curinos-one-agent/tests/coordination/test_topology.py`
- Test: `Curinos/curinos-one-agent/tests/test_mounted_runtime.py`

**Interfaces:**
- Create immutable Curinos-side `WorkflowHandoff` and `ExampleWorkflow` dataclasses with `to_dict()` output matching the APX JSON contract.
- Add `AgentManifest.workflows: tuple[ExampleWorkflow, ...]` and expose `curinos_workflows()` from `coordination/workflows.py`.
- `TopologySnapshot.to_dict()` emits the same `workflows` collection through `annotate_topology`.
- The mounted fleet sets `agent.__apx_workflows__ = [workflow.to_dict() for workflow in manifest.workflows]`; APX's `workflows_for_context(ctx)` reads that optional metadata hook when config workflows are empty, so default `/_apx/agent` and `/_apx/topology` pages receive the declarations without copying them into HTML.

- [ ] **Step 1: Add failing manifest tests.** Assert exactly three workflows, their routes, handoff contracts, and outcomes:

```python
def test_curinos_manifest_declares_reference_workflows() -> None:
    workflows = curinos_manifest().workflows
    assert [workflow.id for workflow in workflows] == [
        "pricing-review",
        "document-led-review",
        "relationship-opportunity",
    ]
    assert workflows[0].route == ("intelligence", "calibrate", "compound")
    assert workflows[2].route == ("intelligence", "compound")
    assert workflows[0].handoffs[0].output_contract == "PricingDecisionPacket.v1"
```

- [ ] **Step 2: Run focused Curinos tests and verify the failure.**

Run: `cd Curinos/curinos-one-agent && python3 -m pytest tests/coordination/test_manifest.py tests/coordination/test_topology.py -k workflow -q`

Expected: FAIL because the manifest has only per-agent example questions.

- [ ] **Step 3: Implement one manifest-backed workflow source.** Put the three workflow declarations in `coordination/workflows.py`; validate nonblank IDs, routes, contract labels, and unique workflow IDs. Add the tuple to `AgentManifest` and use it from both the custom topology serializer and the mounted APX metadata adapter.

- [ ] **Step 4: Serialize the workflows through the existing additive topology seam.** Keep all current dispatcher, specialist, tool, contract, question, and data-domain nodes. Add only the top-level workflow collection. The custom topology response must still return the same execution object and trace-derived highlight IDs.

- [ ] **Step 5: Run the focused tests and inspect serialized output.**

Run: `cd Curinos/curinos-one-agent && python3 -m pytest tests/coordination/test_manifest.py tests/coordination/test_topology.py tests/test_mounted_runtime.py -k 'workflow or topology or mounted' -q`

Expected: PASS, with the three workflow IDs and their exact routes present in `/api/coordination/topology` serialization and no changes to existing handoff edge IDs.

- [ ] **Step 6: Commit the Curinos declaration slice.**

```bash
git add Curinos/curinos-one-agent/coordination/workflows.py Curinos/curinos-one-agent/coordination/manifest.py Curinos/curinos-one-agent/coordination/topology.py Curinos/curinos-one-agent/agent.py Curinos/curinos-one-agent/tests/coordination/test_manifest.py Curinos/curinos-one-agent/tests/coordination/test_topology.py Curinos/curinos-one-agent/tests/test_mounted_runtime.py
git commit -m "feat(curinos): declare coordinated example workflows"
```

---

### Task 6: Keep the Curinos account workspace and workflow descriptions aligned

**Files:**
- Modify: `Curinos/curinos-one-agent/agent_server/front_door.py:205-445`
- Modify: `Curinos/curinos-one-agent/tests/test_overview.py`
- Modify: `Curinos/curinos-one-agent/tests/test_coordination_api.py`
- Modify: `Curinos/curinos-one-agent/tests/test_topology_compatibility.py`

**Interfaces:**
- The account workspace consumes `topology.workflows` when available and falls back to its existing manifest-derived stage cards for older deployed responses.
- `runAsk` continues posting `{question, institution_id: activeAccountId}` to `/api/coordinate`.
- The existing plain-English answer, artifact chain, trace link, and account scope remain visible outside raw JSON.

- [ ] **Step 1: Add failing compatibility tests.** Assert that the account page accepts the shared workflow array, still renders the selected account question, and keeps the account ID in the coordinate request.

```python
def test_front_door_uses_shared_workflow_metadata_when_present() -> None:
    source = FRONT_DOOR_HTML
    assert "topology.workflows" in source
    assert "institution_id: activeAccountId" in source
```

- [ ] **Step 2: Run focused tests and verify the failure.**

Run: `cd Curinos/curinos-one-agent && python3 -m pytest tests/test_overview.py tests/test_coordination_api.py tests/test_topology_compatibility.py -k workflow -q`

Expected: FAIL because the current front door only renders the custom stage metadata.

- [ ] **Step 3: Implement the smallest adapter.** Add a workflow card/step explanation to the existing custom topology renderer, using the shared keys and existing `esc` helper. Do not add another endpoint or a second question runner. Preserve the current fallback copy for old topology payloads.

- [ ] **Step 4: Run focused tests and read back rendered HTML.**

Run: `cd Curinos/curinos-one-agent && python3 -m pytest tests/test_overview.py tests/test_coordination_api.py tests/test_topology_compatibility.py -k 'workflow or topology or account' -q`

Expected: PASS; rendered HTML contains the business question, declared route, and explicit “not yet run” language before execution.

- [ ] **Step 5: Commit the account-surface slice.**

```bash
git add Curinos/curinos-one-agent/agent_server/front_door.py Curinos/curinos-one-agent/tests/test_overview.py Curinos/curinos-one-agent/tests/test_coordination_api.py Curinos/curinos-one-agent/tests/test_topology_compatibility.py
git commit -m "feat(curinos): explain workflow routes in account view"
```

---

### Task 7: Run cross-repository gates and verify packaging

**Files:**
- No new source files.
- Read/check: APX `python/src/apx_agent/_static/topology/*`, APX `python/uv.lock`, Curinos `requirements.lock`, Curinos `uv.lock`.

**Interfaces:**
- APX produces a wheel containing the workflow-aware default pages.
- Curinos consumes the APX revision from the APX branch and retains its immutable pin/verifier surfaces.

- [ ] **Step 1: Run APX focused and UI gates.**

```bash
cd /Users/stuart.gano/Documents/apx-agent/.worktrees/apx-example-workflows
cd python/dev-ui/topology && npm run build
cd ../.. && uv run pytest tests/test_topology.py tests/test_chat_agent.py tests/test_dev_ui_route_coverage.py tests/test_packaging.py -q
```

Expected: TypeScript build succeeds and all selected Python tests pass. Read `python/src/apx_agent/_static/topology/index.html` and assert the referenced asset exists.

- [ ] **Step 2: Update Curinos's APX pin only after the APX commit is identified.** Replace the APX revision consistently in `pyproject.toml`, `databricks.yml`, `scripts/verify_bundle.py`, `scripts/prepare_app_manifest.py`, `requirements.lock`, and `uv.lock`. Use the full immutable commit SHA and run the repository's pin verifier.

- [ ] **Step 3: Run Curinos gates.**

```bash
cd /Users/stuart.gano/Documents/Customer/.worktrees/curinos-example-workflows/Curinos/curinos-one-agent
python3 -m pytest tests/test_ask.py tests/coordination/test_dispatcher.py tests/coordination/test_manifest.py tests/coordination/test_topology.py tests/test_overview.py tests/test_coordination_api.py tests/test_topology_compatibility.py -k 'not uc_overview' -q
python3 -m py_compile coordination/workflows.py coordination/manifest.py coordination/topology.py agent_server/front_door.py
git diff --check
```

Expected: all selected tests pass; the known local `apx_agent`-missing UC-only test remains excluded for the same environment reason documented in the existing branch verification.

- [ ] **Step 4: Commit the pin and lockfile update.**

```bash
git add Curinos/curinos-one-agent/pyproject.toml Curinos/curinos-one-agent/databricks.yml Curinos/curinos-one-agent/scripts/verify_bundle.py Curinos/curinos-one-agent/scripts/prepare_app_manifest.py Curinos/curinos-one-agent/requirements.lock Curinos/curinos-one-agent/uv.lock
git commit -m "chore(curinos): pin workflow-aware apx-agent"
```

---

### Task 8: Deploy and perform read-after-write walkthrough verification

**Files:**
- No source-file changes; deployment uses the committed APX and Curinos branches.
- Evidence: deployment output, production URL, trace IDs, and captured response summaries.

**Interfaces:**
- Databricks profile: `fevm-hvhhmh-7def3z`, always passed explicitly.
- Production app: `https://curinos-one-prod-7474660401027952.aws.databricksapps.com`.
- Existing synthetic catalog/schema/warehouse and Genie settings remain unchanged.

- [x] **Step 1: Build/deploy with the existing Databricks Apps bundle.** Use the pinned APX wheel and the explicit profile. Do not change catalog, schema, warehouse, Genie space, source allowlist, or data mode.

```bash
databricks bundle deploy -t prod --profile fevm-hvhhmh-7def3z
```

Expected: the existing `curinos-one-prod` app updates in place and remains `ACTIVE` at the existing URL.

- [x] **Step 2: Verify readiness and default-page payloads.** Check `/readyz`, `/_apx/topology.json`, and the Chat landing page. Assert the topology response contains the three workflow IDs and the Chat HTML contains their questions once each.

- [x] **Step 3: Run all three examples through the default Topology ChatDock.** For each question, record the response status, trace ID, displayed route, artifact/handoff summary, and final plain-English answer. Confirm a declared-but-unrun workflow says “not yet run” before invocation and changes only after trace evidence arrives.

- [x] **Step 4: Verify the Curinos account-scoped relationship example.** POST the existing coordinate request with `institution_id=bank-c` and assert HTTP 200, route `intelligence -> compound` or the declared coordinated route returned by the current dispatcher, account scope containing `bank-c`, and an account-specific answer. Verify the UI displays the answer outside the JSON/technical detail block. (Final rerun used the selected production account `bank-a`; the earlier bank-c scoped contract was also verified.)

- [x] **Step 5: Verify failure and safety boundaries.** Run an unsupported question and confirm it returns a bounded error with trace correlation, without marking a workflow completed. Confirm no raw token, OBO context, or unbounded tool arguments appear in the default pages.

- [x] **Step 6: Record deployment evidence and stop.** Capture the production URL, app deployment ID, workflow IDs, trace IDs, route/artifact summaries, and any environment-only test gap. Do not merge or delete worktrees until the user explicitly requests the repository handoff.

### Task 9: Resolve metadata-hook coverage on the APX Chat landing

**Files:**
- Modify: `python/src/apx_agent/_models.py`
- Modify: `python/src/apx_agent/_ui_chat.py`
- Test: `python/tests/test_models.py`
- Test: `python/tests/test_chat_agent.py`

**Interfaces:**
- The Chat landing must resolve workflow declarations through the same `workflows_for_context(ctx)` path as topology, while preserving config-backed `AgentConfig.workflows` behavior.
- Existing legacy examples remain first, deduplicated against attached workflow questions.
- Curinos's `agent.__apx_workflows__` metadata must appear as escaped workflow starter chips in `/_apx/chat` and `/_apx/agent` without copying customer declarations into APX.

- [x] **Step 1: Add a failing attached-workflow landing test.** Configure an otherwise empty `AgentConfig`, attach one valid serialized workflow through `agent.__apx_workflows__`, render the landing, and assert its escaped title/purpose/question appears once as a `workflow-chip` with the existing `useExample` behavior.
- [x] **Step 2: Implement the smallest shared prompt-resolution change.** Reuse `workflows_for_context(ctx)` from the Chat renderer and keep `workflow_prompts` insertion-order deduplication for legacy plus resolved workflow questions. Do not add a second metadata channel or execution path.
- [x] **Step 3: Run APX focused Chat/model tests and packaging checks.** Rebuild the Chat/topology assets as required by the existing packaging workflow.
- [x] **Step 4: Republish APX, repin Curinos to the new immutable SHA, redeploy the existing bundle, and rerun the Task 8 read-after-write checks.** Preserve the synthetic catalog/schema/warehouse/Genie settings and record the new deployment evidence. The final typed-artifact answer remediation is `21e5932b`; the final production rerun returned nonblank plain-English answers for all coordinated routes.

---

## Plan self-review

- **Spec coverage:** The plan covers the contract, additive topology response, Chat landing, Topology workflow rail, trace-derived status, Curinos manifest conversion, account-page alignment, testing, rollout, and production verification.
- **Completeness scan:** No unfinished-marker token or unspecified implementation step is required. Every task names files, interfaces, commands, and expected outcomes.
- **Type consistency:** Python uses `WorkflowHandoff`/`ExampleWorkflow`; TypeScript mirrors them as `WorkflowHandoff`/`ExampleWorkflow`; both serialize `input_contract`, `output_contract`, `follow_ups`, and `route` with the same names.
- **Scope check:** APX and Curinos changes are coupled by the shared workflow payload and are kept in separate commits/worktrees. No new execution subsystem or unrelated refactor is included.
