"""
Model seam + research provider for the discovery workflow.

``Completion`` is the ONLY model seam in this package: an injectable async
callable ``(prompt, schema) -> dict``. No vendor SDK is imported here — a
caller wires whatever model they like behind this Protocol, and tests stub it.

``ResearchProvider`` supplies the one input step 1 needs: customer research.
The default ``LLMResearchProvider`` gets it from the same injected completion.
A domain agent can register its own provider (e.g. one backed by live account
data) without touching this module.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .schemas import ResearchBundle


@runtime_checkable
class Completion(Protocol):
    """Async model call: render a prompt, return structured JSON matching schema."""

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class ResearchProvider(Protocol):
    async def research(self, customer: str, persona: str | None) -> ResearchBundle: ...


_RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"],
}


class LLMResearchProvider:
    """Default provider: researches the customer via the injected completion."""

    def __init__(self, completion: Completion):
        self._completion = completion

    async def research(self, customer: str, persona: str | None = None) -> ResearchBundle:
        focus = f" Focus on the priorities of the {persona}." if persona else ""
        prompt = (
            f"Research the organization '{customer}'.{focus} Summarize its industry, "
            "strategic priorities, and current initiatives as a list of findings. "
            'Return JSON with "findings" (array of strings).'
        )
        raw = await self._completion(prompt, _RESEARCH_SCHEMA)
        findings = raw["findings"] if "findings" in raw else []
        if not isinstance(findings, list):
            findings = [str(findings)]
        return ResearchBundle(customer=customer, persona=persona, findings=[str(f) for f in findings])
