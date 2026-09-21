"""pe55-support-agent: one Agent for Brickfood PE55 / BrickReady.

PE55 asks for a support agent that answers from customer reviews (RLS by
region), product docs, and a UC-volume policy file that must be re-read on
every turn. This example maps that CUJ onto shipped apx primitives:

* ``sql_tool`` — warehouse-scoped SQL as the caller (OBO). UC row filters
  on ``agent_cuj.customer_support.user_reviews`` do the region cut; the
  agent does not invent a region WHERE clause.
* ``vector_search_tool`` — product docs as the caller.
* ``load_latest_policies`` — custom ``@tool`` because no volume-read
  factory or ``uc_volume`` ResourceSpec exists. Downloads
  ``latest_policies.txt`` via the Files API on the user client.

``APX_SMOKE_MODE=1`` swaps the three workspace tools for in-process stubs so
CI and ``create_app`` do not need go/e2dogfood. ``agent:agent`` is always a
real ``Agent`` object (never ``None``).
"""
from __future__ import annotations

import os

from apx_agent import (
    Agent,
    Dependencies,
    ResourceSpec,
    attach_resources,
    require_user_api_scopes,
    sql_tool,
    tool,
    vector_search_tool,
)

# app.yml and databricks.yml both default APX_SMOKE_MODE to "0" for live deploys.
DEFAULT_APX_SMOKE_MODE = "0"
SMOKE_MODE = os.environ.get("APX_SMOKE_MODE", DEFAULT_APX_SMOKE_MODE) == "1"

REVIEWS_TABLE = "agent_cuj.customer_support.user_reviews"
PRODUCT_DOCS_INDEX = "agent_cuj.knowledge.product_docs"
POLICY_PATH = (
    "/Volumes/agent_cuj/customer_support/policy_docs/latest_policies.txt"
)
REGION_CHECK_SQL = (
    "SELECT current_user() as user, "
    "agent_cuj.customer_support.get_user_regions() as allowed_regions"
)

SMOKE_POLICY = (
    "Support policy (smoke stub — not the live volume).\n"
    "- Refunds: 30 days with receipt.\n"
    "- Shipping: 5–7 business days domestic.\n"
    "- Stay inside this policy text when answering.\n"
)

INSTRUCTIONS = """You are a customer-support agent for Brickfood PE55.

Call `load_latest_policies` first every turn and stay inside that policy
text. Answer from customer reviews and product documentation only.

Reviews are region-filtered by Unity Catalog row-level security when you
query them as the calling user. Do not invent a region filter. Do not
query as a service principal.

If a tool returns an error or empty result, say so — do not guess.
"""


def _live_policy_tool():
    @tool
    def load_latest_policies(ws: Dependencies.UserClient) -> str:
        """Call first every turn. Returns the current support policy text.

        Downloads ``/Volumes/agent_cuj/customer_support/policy_docs/latest_policies.txt``
        as the calling user. Returns a usable error string if the file is
        missing or unreadable — does not raise for not-found.
        """
        try:
            raw = ws.files.download(POLICY_PATH).contents.read()
        except Exception as exc:
            return f"Could not load latest policies from {POLICY_PATH}: {exc}"
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw)

    require_user_api_scopes(load_latest_policies, ["files"])
    return load_latest_policies


def _smoke_tools() -> list:
    @tool
    def load_latest_policies() -> str:
        """Call first every turn. Returns the current support policy text.

        Smoke stub — live deploys download the UC volume file as the caller.
        """
        return SMOKE_POLICY

    @tool
    def run_sql(query: str) -> dict:
        """Query customer reviews (smoke stub).

        Live deploys use ``sql_tool`` against ``agent_cuj.customer_support.user_reviews``.
        """
        del query
        return {
            "row_count": 2,
            "truncated": False,
            "rows": [
                {
                    "region": "US-West",
                    "rating": 2,
                    "feedback": "Refund took too long after a late shipment.",
                },
                {
                    "region": "US-West",
                    "rating": 4,
                    "feedback": "Shipping was on time this week.",
                },
            ],
        }

    @tool
    def vector_search(query: str) -> list[dict]:
        """Search product documentation (smoke stub).

        Live deploys use ``vector_search_tool('agent_cuj.knowledge.product_docs')``.
        """
        del query
        return [
            {
                "title": "Refund policy",
                "snippet": "Refunds within 30 days with receipt.",
            },
            {
                "title": "Shipping policy",
                "snippet": "Standard shipping is 5–7 business days.",
            },
        ]

    return [load_latest_policies, run_sql, vector_search]


def _live_tools() -> list:
    reviews = sql_tool(
        name="run_sql",
        description=(
            "Query agent_cuj.customer_support.user_reviews as the calling "
            "user. UC row-level security already limits rows to the "
            "caller's region. Do not add a region filter. Returns up to "
            "1000 rows."
        ),
    )
    attach_resources(
        reviews,
        [ResourceSpec("uc_table", REVIEWS_TABLE)],
    )
    docs = vector_search_tool(
        PRODUCT_DOCS_INDEX,
        name="vector_search",
        description=(
            "Search agent_cuj.knowledge.product_docs for FAQs and help "
            "articles matching a natural-language query. Returns up to 5 "
            "hits as the calling user."
        ),
        num_results=5,
    )
    return [_live_policy_tool(), reviews, docs]


def create_support_agent(*, smoke: bool = False) -> Agent:
    """Build the PE55 support agent. Always returns a real Agent object."""
    return Agent(
        name="pe55_support_agent",
        instructions=INSTRUCTIONS,
        tools=_smoke_tools() if smoke else _live_tools(),
    )


agent = create_support_agent(smoke=SMOKE_MODE)
