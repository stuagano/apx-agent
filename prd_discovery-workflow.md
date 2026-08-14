# PRD: Generic Discovery Workflow (Tech-to-Biz PoV)

## Summary

Build the **domain-agnostic** discovery workflow described in
`docs/superpowers/specs/2026-08-14-discovery-workflow-design.md`: a durable,
resumable 6-step "tech-to-biz PoV & discovery guide" chain on apx-agent's
existing `WorkflowEngine`. This is the GENERIC engine only — no Databricks /
UCO / SFDC / Genie / internal-tool references anywhere in the new module. The
Databricks provider and handoff steps (brickroad-ee-agent) are OUT OF SCOPE.

## Background

`go/techpov` is a manual tab-per-step Google Doc where a human copy/pastes
prompts into Gemini across steps. Its step chain maps 1:1 onto
`apx_agent.workflow.WorkflowEngine` (durable checkpointed `step(run_id,
step_key, handler)`, resumable via `start_run(run_id=...)`). See the design
spec for the full mapping.

## Research Inputs

- Design spec: `docs/superpowers/specs/2026-08-14-discovery-workflow-design.md`
- Engine: `python/src/apx_agent/workflow/engine.py` (Protocol),
  `engine_memory.py` (`InMemoryEngine`, use for tests).
- Model invocation: inspect `_executor.py` / `_executor_factory.py` / `_models.py`
  for a vendor-neutral completion abstraction to inject. If none is neutral,
  the module defines its own injectable `Completion` callable Protocol and
  tests stub it — do NOT import a vendor SDK into the discovery module.

## Goals

1. A `DiscoveryWorkflow` that runs 6 sequential steps on `WorkflowEngine`, each
   output persisted and fed forward.
2. Resumable: re-opening a run replays completed steps and continues.
3. Structured output per step (matrices/scores/guide are typed, not prose) +
   a final markdown render.
4. Pluggable `ResearchProvider` (default `LLMResearchProvider`) feeding Step 1.
5. A handoff hook to append domain steps (core registers none).
6. Zero vendor-specific identifiers in the new module.

## Non-Goals

- `DatabricksResearchProvider`, UCO split, solution-builder, reffy, GVP (all brickroad-ee).
- Feeding PoC doc / arch diagram / demo plan.
- UI, TypeScript engine mirror, deployment.

## Requirements

### Functional
- 6 steps, stable keys: `priorities`, `value_matrices`, `heat_map`,
  `wow_selection`, `discovery_guide`, `three_ws`.
- Each step: render a prompt template against accumulated run state → call the
  injected completion → parse structured output → return it (engine persists).
- `ResearchProvider` Protocol: `async research(customer, persona) -> ResearchBundle`;
  `LLMResearchProvider` default uses the injected completion.
- Handoff hook: caller supplies an ordered list of extra `(step_key, handler)`
  appended after `three_ws`, each able to read prior run state; default empty.
- Prompt templates live in an overridable `prompts/` location (not hardcoded
  inline), so a domain agent can override per step.

### Non-functional
- No import of any vendor SDK or reference to databricks/dbu/uco/salesforce/
  sfdc/genie/reffy/solution-builder in the discovery module (case-insensitive).
- `make check` (ruff + existing suite) stays green.

## Acceptance Criteria

## Agent Handoff
```json
{
  "prd_version": "1.0",
  "goal": "A domain-agnostic DiscoveryWorkflow that runs 6 sequential structured-output steps on apx_agent.workflow.WorkflowEngine, is resumable, feeds Step 1 from a pluggable ResearchProvider (LLMResearchProvider default), supports appended handoff steps, renders a final markdown brief, and contains zero vendor-specific identifiers — proven by pytest.",
  "success_criteria": ["6-step chain persists + feeds forward", "resume replays completed steps", "pluggable ResearchProvider", "vendor-neutral module", "structured outputs + markdown render"],
  "convergence": {
    "stopping_signal": "pytest python/tests/test_discovery_workflow.py all green",
    "progress_metric": "failing gate-test count",
    "known_ceiling": "none — all criteria are unit-testable with InMemoryEngine + a stub completion, no live deps",
    "re_represented": true
  },
  "acceptance_criteria": [
    { "id": "AC-1", "description": "DiscoveryWorkflow runs all 6 steps in order on the engine; get_run shows 6 completed StepRecords with the stable keys, each step's output present in run state", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_discovery_workflow.py", "gate_test": "test_runs_six_steps_in_order" },
    { "id": "AC-2", "description": "Resume: start_run with an existing run_id after 2 completed steps replays those 2 without re-invoking their handlers (assert call count) and continues from step 3", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_discovery_workflow.py", "gate_test": "test_resume_replays_completed_steps" },
    { "id": "AC-3", "description": "ResearchProvider protocol + LLMResearchProvider default; injected provider is called once and its ResearchBundle flows into the priorities step input", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_discovery_workflow.py", "gate_test": "test_research_provider_feeds_priorities" },
    { "id": "AC-4", "description": "Each step returns structured output validated against a per-step schema (matrices/scores are typed dicts/dataclasses, not free strings); malformed completion output is handled (raises a typed error, not silently passed through)", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_discovery_workflow.py", "gate_test": "test_structured_output_per_step" },
    { "id": "AC-5", "description": "Handoff hook: an appended (step_key, handler) runs after three_ws, sees prior run state, and its output persists; default handoff list is empty", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_discovery_workflow.py", "gate_test": "test_handoff_steps_appended" },
    { "id": "AC-6", "description": "Neutrality: recursive case-insensitive scan of the discovery module source finds none of databricks|dbu|uco|salesforce|sfdc|genie|reffy|solution-builder, and the module imports no vendor SDK", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_discovery_workflow.py", "gate_test": "test_module_is_vendor_neutral" },
    { "id": "AC-7", "description": "A render step turns the completed structured run state into a single markdown discovery brief containing the priorities, matrices, chosen wow ideas, discovery guide, and 3 Ws sections", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_discovery_workflow.py", "gate_test": "test_renders_markdown_brief" }
  ],
  "must_have": ["6 checkpointed steps on WorkflowEngine using engine.step with stable keys", "injectable Completion callable (no vendor SDK import in module)", "ResearchProvider Protocol + LLMResearchProvider", "structured per-step outputs (dataclasses/typed dicts) + JSON schemas", "overridable prompts/ templates dir", "handoff hook (append steps after three_ws)", "markdown render step"],
  "out_of_scope": ["DatabricksResearchProvider", "UCO split / solution-builder / reffy / GVP handoff steps", "PoC doc / arch diagram / demo plan downstream", "UI", "TypeScript engine mirror", "deployment"],
  "constraints": {
    "tech_stack": "Python 3.11+, asyncio, dataclasses, pytest; apx_agent.workflow.InMemoryEngine for tests",
    "key_files": ["python/src/apx_agent/discovery/__init__.py", "python/src/apx_agent/discovery/workflow.py", "python/src/apx_agent/discovery/steps.py", "python/src/apx_agent/discovery/research.py", "python/src/apx_agent/discovery/schemas.py", "python/src/apx_agent/discovery/render.py", "python/src/apx_agent/discovery/prompts/", "python/tests/test_discovery_workflow.py"],
    "patterns": "each techpov step = one engine.step(run_id, step_key, handler); handler renders a template from discovery/prompts/, calls the injected async Completion(prompt, schema)->dict, parses into the step's dataclass, returns it; accumulate outputs in a run-state dict passed forward; ResearchProvider is a runtime_checkable Protocol; LLMResearchProvider uses the same injected Completion; prompt templates are files loaded by step_key so they are overridable; keep the module free of any vendor import — the Completion callable is the only model seam"
  },
  "preferred_skills": [],
  "escalate_on": [
    "no vendor-neutral model-completion abstraction exists to inject AND defining a local Completion Protocol would duplicate significant executor logic",
    "WorkflowEngine.step signature cannot express feeding one step's output into the next without leaking persistence internals",
    "a techpov step genuinely cannot be made domain-neutral without losing its meaning",
    "architectural decision not covered in this PRD or the design spec"
  ],
  "loop_guards": {
    "max_iterations": 8,
    "state_hash_check": true,
    "heartbeat_interval_seconds": 30,
    "on_stuck": "pause_and_surface",
    "on_no_progress": "stop_and_escalate",
    "state_persistence": "local_disk"
  }
}
```
