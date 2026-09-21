"""sec-10k-agent: two-stage SequentialAgent over a Knowledge Assistant.

Stage 1 (`sec_10k_research`) must call `ask_knowledge_assistant` against an
Agent Bricks Knowledge Assistant that already indexes 10-K filings. Stage 2
(`sec_10k_brief`) rewrites the grounded answer and lists the `doc_uri`
citations. Neither stage invents sources; the brief fails closed if the KA
returns an error or no citations.

The KA serving-endpoint name comes from `APX_KA_ENDPOINT_NAME`. Do not
hardcode it, and do not ingest EDGAR here — the endpoint is a prerequisite.
"""
from __future__ import annotations

import os

from apx_agent import Agent, SequentialAgent, knowledge_assistant_tool

RESEARCH_INSTRUCTIONS = """\
You are the 10-K research stage.

You MUST call `ask_knowledge_assistant` with the user's question. Do not
answer from prior knowledge and do not invent citations, filing dates, or
figures.

Return the tool's grounded `answer` plus its `citations` (`doc_uri` values)
verbatim enough that the next stage can quote them. If the tool returns an
`error` field, surface that error and stop — do not guess.
"""

BRIEF_INSTRUCTIONS = """\
You are the 10-K brief stage. You have no tools.

Rewrite the previous stage's grounded answer as a short investor brief:
what the filing says, then a bullet list of every `doc_uri` citation.

Fail closed: if the previous stage reported an error, returned no
citations, or the answer is not grounded in the knowledge assistant,
say so and do not invent sources or numbers.
"""


def create_sec_10k_agent(endpoint_name: str) -> SequentialAgent:
    """Build the two-stage 10-K flow bound to one Knowledge Assistant endpoint."""
    if not str(endpoint_name).strip():
        raise RuntimeError(
            "APX_KA_ENDPOINT_NAME is required — pass the Agent Bricks "
            "Knowledge Assistant serving-endpoint name (never hardcode it)."
        )
    research = Agent(
        name="sec_10k_research",
        instructions=RESEARCH_INSTRUCTIONS,
        tools=[knowledge_assistant_tool(endpoint_name)],
    )
    brief = Agent(
        name="sec_10k_brief",
        instructions=BRIEF_INSTRUCTIONS,
        tools=[],
    )
    return SequentialAgent(
        name="sec_10k_agent",
        agents=[research, brief],
        instructions=(
            "Research a 10-K question against the knowledge assistant, then "
            "write a cited brief. Do not invent citations."
        ),
    )


def get_agent() -> SequentialAgent:
    """Resolve `APX_KA_ENDPOINT_NAME` and return the sequential flow."""
    endpoint = os.environ.get("APX_KA_ENDPOINT_NAME")
    if endpoint is None or not endpoint.strip():
        raise RuntimeError(
            "APX_KA_ENDPOINT_NAME is required — set it to the Agent Bricks "
            "Knowledge Assistant serving-endpoint name."
        )
    return create_sec_10k_agent(endpoint.strip())


def _agent_for_import() -> SequentialAgent:
    """Always return a SequentialAgent so ``agent:agent`` is loadable.

    AppKit staging and ``create_app`` import this module without a live KA.
    ``get_agent()`` still fails closed when the env is blank.
    """
    endpoint = os.environ.get("APX_KA_ENDPOINT_NAME")
    if endpoint is not None and endpoint.strip():
        return create_sec_10k_agent(endpoint.strip())
    return create_sec_10k_agent("$APX_KA_ENDPOINT_NAME")


agent = _agent_for_import()
