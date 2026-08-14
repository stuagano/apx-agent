"""Gate tests for the domain-agnostic DiscoveryWorkflow (see prd_discovery-workflow.md)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import apx_agent.discovery as discovery
from apx_agent.discovery import (
    DiscoveryWorkflow,
    MalformedStepOutput,
    Priorities,
    ResearchBundle,
    STEP_KEYS,
    ValueMatrices,
    render_markdown_brief,
)
from apx_agent.discovery.schemas import MatrixRow
from apx_agent.workflow import InMemoryEngine

pytestmark = pytest.mark.asyncio

# Valid canned completion output keyed by the step schema's required-key set.
CANNED: dict[frozenset[str], dict[str, Any]] = {
    frozenset(["findings"]): {"findings": ["Acme is a retailer", "margin pressure is rising"]},
    frozenset(["industry", "priorities"]): {"industry": "Retail", "priorities": ["cut costs", "grow revenue"]},
    frozenset(["rows"]): {"rows": [{"outcome": "faster reporting", "capability": "unified data", "enabler": "the platform"}]},
    frozenset(["cells"]): {"cells": [{"outcome": "faster reporting", "value_score": 8, "effort_score": 3}]},
    frozenset(["chosen", "rationale"]): {"chosen": ["faster reporting"], "rationale": "best value-to-effort ratio"},
    frozenset(["questions", "value_equation"]): {"questions": ["how long does a report take today?"], "value_equation": "hours saved x analyst rate"},
    frozenset(["why_change", "why_now", "why_us", "gaps"]): {"why_change": "manual reporting", "why_now": "budget cycle", "why_us": "native fit", "gaps": ["data access unknown"]},
}


class StubCompletion:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(prompt)
        return dict(CANNED[frozenset(schema["required"])])


class CountingResearch:
    def __init__(self) -> None:
        self.calls = 0

    async def research(self, customer: str, persona: str | None = None) -> ResearchBundle:
        self.calls += 1
        return ResearchBundle(customer=customer, persona=persona, findings=["RESEARCH-MARKER for " + customer])


async def _return(value: Any):
    return value


async def test_runs_six_steps_in_order() -> None:
    engine = InMemoryEngine()
    wf = DiscoveryWorkflow(engine, StubCompletion())
    rid = await wf.run("Acme")

    snap = await engine.get_run(rid)
    assert snap is not None
    assert [s.step_key for s in snap.steps] == STEP_KEYS
    assert all(s.status == "completed" for s in snap.steps)
    assert all(s.output is not None for s in snap.steps)


async def test_resume_replays_completed_steps() -> None:
    engine = InMemoryEngine()
    rid = await engine.start_run("discovery", {"customer": "Acme", "persona": None})
    # Seed the first two steps as if a prior session had completed them, then died.
    await engine.step(rid, "priorities", lambda: _return(Priorities("Retail", ["cut costs"])))
    await engine.step(rid, "value_matrices", lambda: _return(ValueMatrices([MatrixRow("o", "c", "e")])))

    completion = StubCompletion()
    research = CountingResearch()
    wf = DiscoveryWorkflow(engine, completion, research_provider=research)
    await wf.run("Acme", run_id=rid)

    # priorities + value_matrices replayed from cache: neither their completion
    # nor the research provider re-fires. Only steps 3-6 invoke the completion.
    assert completion.calls == 4
    assert research.calls == 0
    snap = await engine.get_run(rid)
    assert snap is not None
    assert [s.step_key for s in snap.steps] == STEP_KEYS


async def test_research_provider_feeds_priorities() -> None:
    engine = InMemoryEngine()
    completion = StubCompletion()
    research = CountingResearch()
    wf = DiscoveryWorkflow(engine, completion, research_provider=research)
    await wf.run("Acme")

    assert research.calls == 1
    # The bundle flows into the priorities prompt (first completion call).
    assert "RESEARCH-MARKER for Acme" in completion.prompts[0]


async def test_structured_output_per_step() -> None:
    engine = InMemoryEngine()
    wf = DiscoveryWorkflow(engine, StubCompletion())
    rid = await wf.run("Acme")

    snap = await engine.get_run(rid)
    assert snap is not None
    by_key = {s.step_key: s.output for s in snap.steps}
    assert isinstance(by_key["priorities"], Priorities)
    assert isinstance(by_key["value_matrices"], ValueMatrices)
    assert isinstance(by_key["value_matrices"].rows[0], MatrixRow)

    # Malformed completion output raises a typed error, not silent passthrough.
    with pytest.raises(MalformedStepOutput):
        Priorities.from_dict({"industry": "Retail"})

    class BadCompletion:
        async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            return {}

    with pytest.raises(MalformedStepOutput):
        await DiscoveryWorkflow(InMemoryEngine(), BadCompletion()).run("Acme")


async def test_handoff_steps_appended() -> None:
    engine = InMemoryEngine()
    seen: dict[str, Any] = {}

    async def extra_handler(state: dict[str, Any]) -> dict[str, str]:
        seen.update(state)
        return {"followup": "scheduled"}

    wf = DiscoveryWorkflow(engine, StubCompletion(), handoff_steps=[("followup", extra_handler)])
    rid = await wf.run("Acme")

    snap = await engine.get_run(rid)
    assert snap is not None
    assert [s.step_key for s in snap.steps] == [*STEP_KEYS, "followup"]
    assert snap.steps[-1].output == {"followup": "scheduled"}
    # The handoff handler saw the completed prior run state.
    assert set(STEP_KEYS).issubset(seen.keys())

    # Default handoff list is empty: exactly the 6 core steps.
    wf2 = DiscoveryWorkflow(engine, StubCompletion())
    assert wf2.handoff_steps == []


async def test_module_is_vendor_neutral() -> None:
    forbidden = ["databricks", "dbu", "uco", "salesforce", "sfdc", "genie", "reffy", "solution-builder"]
    assert discovery.__file__ is not None
    root = Path(discovery.__file__).parent
    sources = list(root.rglob("*.py")) + list(root.rglob("*.md"))
    assert sources, "expected discovery source files"
    for path in sources:
        text = path.read_text(encoding="utf-8").lower()
        for term in forbidden:
            assert term not in text, f"vendor term '{term}' found in {path.name}"
    # No vendor SDK imported by the module.
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import databricks" not in text
        assert "from databricks" not in text


async def test_renders_markdown_brief() -> None:
    engine = InMemoryEngine()
    wf = DiscoveryWorkflow(engine, StubCompletion())
    rid = await wf.run("Acme")

    snap = await engine.get_run(rid)
    assert snap is not None
    brief = snap.output  # the workflow's render step feeds finish_run
    assert isinstance(brief, str)
    for heading in ["Strategic Priorities", "Value Matrices", "Heat Map", "Wow Selection", "Discovery Guide", "3 Ws"]:
        assert heading in brief
    assert "Retail" in brief
    assert "faster reporting" in brief

    # render_markdown_brief also works standalone over reconstructed state.
    state = {s.step_key: s.output for s in snap.steps}
    assert render_markdown_brief(state, "Acme").startswith("# Discovery Brief: Acme")
