"""
DiscoveryWorkflow — a durable, resumable 6-step tech-to-biz PoV chain.

Each step is one checkpointed ``engine.step`` call with a stable key. A handler
gathers research (step 1 only), renders its prompt template against accumulated
run state, calls the injected completion, and parses the structured result. The
engine persists each output, so re-opening a run replays completed steps and
continues from the first uncompleted one.

Vendor-neutral: the only model dependency is the injected ``Completion``
callable; the engine is injected too. No vendor SDK is imported.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .render import render_markdown_brief
from .research import Completion, LLMResearchProvider, ResearchProvider
from .steps import DEFAULT_PROMPTS_DIR, PARSERS, SCHEMAS, STEP_KEYS, render_prompt

if TYPE_CHECKING:  # avoid a runtime import chain that transitively pulls vendor SDKs
    from apx_agent.workflow.engine import WorkflowEngine

WORKFLOW_NAME = "discovery"

# A handoff step reads the accumulated run state and returns its output.
HandoffHandler = Callable[[dict[str, Any]], Awaitable[Any]]


class DiscoveryWorkflow:
    def __init__(
        self,
        engine: WorkflowEngine,
        completion: Completion,
        research_provider: ResearchProvider | None = None,
        handoff_steps: list[tuple[str, HandoffHandler]] | None = None,
        prompts_dir: Path | None = None,
    ):
        self.engine = engine
        self.completion = completion
        if research_provider is None:
            research_provider = LLMResearchProvider(completion)
        self.research_provider = research_provider
        if handoff_steps is None:
            handoff_steps = []
        self.handoff_steps = handoff_steps
        if prompts_dir is None:
            prompts_dir = DEFAULT_PROMPTS_DIR
        self.prompts_dir = prompts_dir

    def _handler(self, step_key: str, customer: str, persona: str | None, state: dict[str, Any]):
        async def handler() -> Any:
            context: dict[str, Any] = {"customer": customer, "persona": persona or "", **state}
            if step_key == "priorities":
                context["research"] = await self.research_provider.research(customer, persona)
            prompt = render_prompt(step_key, context, self.prompts_dir)
            raw = await self.completion(prompt, SCHEMAS[step_key])
            return PARSERS[step_key](raw)

        return handler

    async def run(self, customer: str, persona: str | None = None, run_id: str | None = None) -> str:
        """Run (or resume) the full chain; returns the run_id. State is persisted per step."""
        rid = await self.engine.start_run(
            WORKFLOW_NAME, {"customer": customer, "persona": persona}, run_id=run_id
        )
        state: dict[str, Any] = {}
        for step_key in STEP_KEYS:
            state[step_key] = await self.engine.step(rid, step_key, self._handler(step_key, customer, persona, state))

        for step_key, handler in self.handoff_steps:
            snapshot = dict(state)
            state[step_key] = await self.engine.step(rid, step_key, lambda h=handler, s=snapshot: h(s))

        await self.engine.finish_run(rid, "completed", render_markdown_brief(state, customer))
        return rid
