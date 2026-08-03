"""technical_specialist: Handles technical support, errors, and integration issues.

Standalone apx-agent app — serves its own A2A card, callable by any orchestrator
via ``sub_agents=[$TECHNICAL_SPECIALIST_URL]``. APX_SMOKE_MODE=1 swaps
workspace-dependent tools for in-process stubs.
"""
from __future__ import annotations

import os

from apx_agent import Agent, tool

SMOKE_MODE = os.environ.get("APX_SMOKE_MODE", "0") == "1"


if SMOKE_MODE:

    @tool
    def docs_search(query: str) -> list[dict]:
        """Search the support docs index (smoke stub).

        Real deployments use ``vector_search_tool`` against
        ``main.support.docs_index``.
        """
        return [
            {"doc_id": "kb-001", "title": "Troubleshooting common errors",
             "url": "https://example.com/kb/001",
             "snippet": "If the app fails to start, check your connection settings."},
            {"doc_id": "kb-014", "title": "Resetting your password",
             "url": "https://example.com/kb/014",
             "snippet": "Use the 'Forgot password' link on the login page."},
        ]

    technical_tools = [docs_search]

else:
    from apx_agent import vector_search_tool

    technical_tools = [
        vector_search_tool(
            "main.support.docs_index",
            columns=["doc_id", "title", "content", "url"],
            num_results=5,
            name="docs_search",
            description="Search the support documentation index.",
        ),
    ]


agent = Agent(
    name="technical_specialist",
    description=(
        "Handles technical support: product errors, outages, and integration "
        "issues. Call when the user reports something broken, an error message, "
        "or a configuration problem. Returns troubleshooting guidance backed by "
        "the docs index."
    ),
    instructions=(
        "You're a technical specialist. Answer questions about product errors, "
        "outages, and integration issues. Use the docs_search tool to find "
        "relevant troubleshooting articles before answering."
    ),
    tools=technical_tools,
)
