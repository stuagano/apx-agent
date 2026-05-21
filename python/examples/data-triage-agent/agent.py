"""data-triage-agent — root agent (ADK-style top-level definition).

The deterministic-router + 6-step ``SequentialAgent`` investigation
topology lives at ``data_triage_agent.backend.pipeline``. This module is
the canonical top-level import point that:

  * ``agent_server/start_server.py`` (Databricks Apps deploy) consumes
    when it compiles + serves the two underlying branches.
  * ``apx deploy --target model-serving`` consumes if/when the agent is
    published to a Mosaic AI serving endpoint.

The router exposes ``.investigation`` and ``.general`` as public
attributes — ``agent_server/start_server.py`` uses those to compile each
branch separately via ``compile_to_responses_agent`` (which doesn't
recognize the custom ``_DeterministicRouter`` ``BaseAgent`` subclass
directly), and does the keyword routing at the request layer. This
preserves the "no LLM call for routing" property of the underlying
pipeline.

Requires the ``DATA_INSPECTOR_URL`` env var to point at a deployed
data-inspector sub-agent (see ``../data-inspector/``).
"""

from __future__ import annotations

from data_triage_agent.backend.pipeline import agent  # noqa: F401
