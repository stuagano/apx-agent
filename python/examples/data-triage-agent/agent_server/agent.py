"""data-triage-agent Apps-target wrapper.

The existing agent + 6-step SequentialAgent + deterministic router live in
``data_triage_agent.backend.pipeline``. ``_DeterministicRouter`` is a
custom ``BaseAgent`` subclass that ``compile_to_langgraph`` doesn't
recognize directly, so this module compiles its two underlying branches
(the investigation ``SequentialAgent`` and the general ``LlmAgent``)
separately and does the deterministic keyword routing at the request
layer.

The keyword routing preserves the original "no LLM call for routing"
property — same semantics as Model Serving via the pipeline:agent path,
just expressed differently.

Requires `DATA_INSPECTOR_URL` to point at a deployed data-inspector
sub-agent (see `../data-inspector/`).
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import invoke, stream

from apx_agent import compile_to_responses_agent

from data_triage_agent.backend.pipeline import agent as _router
from data_triage_agent.backend.pipeline import _DeterministicRouter

assert isinstance(_router, _DeterministicRouter)

MODEL = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")

# Compile each branch separately. compile_to_responses_agent recognizes
# SequentialAgent + LlmAgent directly.
_invest_invoke, _invest_stream = compile_to_responses_agent(
    _router.investigation, model=MODEL,
)
_general_invoke, _general_stream = compile_to_responses_agent(
    _router.general, model=MODEL,
)


def _route_text(request) -> str:
    """Extract the latest user message for keyword routing."""
    items = getattr(request, "input", None) or []
    for item in reversed(list(items)):
        role = getattr(item, "role", None) or (item.get("role") if isinstance(item, dict) else None)
        if role == "user":
            content = (
                getattr(item, "content", None)
                or (item.get("content") if isinstance(item, dict) else None)
                or ""
            )
            return str(content)
    return ""


@invoke()
def non_streaming(request):
    """Route to the investigation pipeline on keyword match, else general."""
    if _DeterministicRouter.matches_investigation(_route_text(request)):
        return _invest_invoke(request)
    return _general_invoke(request)


@stream()
def streaming(request):
    """Route to the investigation pipeline on keyword match, else general."""
    if _DeterministicRouter.matches_investigation(_route_text(request)):
        yield from _invest_stream(request)
    else:
        yield from _general_stream(request)
