"""customer_triage agent — Apps-target shape.

This is the Databricks-Apps version of the customer_triage worked example.
It mirrors the structure of ``examples/memory_demo/agent_server/agent.py``:

  * Stores are seeded at module load (executed once per Apps worker).
  * The full agent topology — four sub-agents under a ``HandoffAgent`` —
    is defined inline. The original ``examples/customer_triage/agent.py``
    is intentionally NOT imported here. The bundle's ``source_code_path``
    is ``./.build`` and the build step does not copy that file, so an
    import would fail at startup in the App container. Mirroring the
    memory_demo working layout (duplication, not import) is the safe
    choice — and it lets the original file keep its Model-Serving shape
    untouched.

  * ``APX_SMOKE_MODE=1`` (set in ``databricks.yml`` env) swaps out tools
    that depend on workspace resources (UC functions, Genie spaces,
    Vector Search indexes) for in-process stubs so the deploy doesn't
    require any prerequisite resources to exist on fe-stable. The agent
    topology — HandoffAgent + sub-agents + memory wiring — is unchanged.

  * The memory wiring on ``account_specialist`` is preserved end-to-end:
    same ``InMemoryMemoryStore``, same seeded memories for ``user:alice``
    and ``user:bob``, same ``make_memory_tools`` call with
    ``default_principal_id="user:alice"``. The smoke test verifies that
    after triage hands off to account_specialist, the ``recall`` tool is
    invoked against the seeded store.
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import invoke, stream

from apx_agent import (
    Agent,
    HandoffAgent,
    InMemoryMemoryStore,
    compile_to_responses_agent,
    make_memory_tools,
    tool,
)

# ---------------------------------------------------------------------------
# Mode + model resolution.
# ---------------------------------------------------------------------------


SMOKE_MODE = os.environ.get("APX_SMOKE_MODE", "0") == "1"
MODEL = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Tools — branched on SMOKE_MODE at the tool-list level (per advisor design).
#
# In smoke mode every tool is a plain @tool callable with no external
# dependency. In production mode the original UC / Genie / Vector Search
# variants would be wired here (mirroring examples/customer_triage/agent.py).
# ---------------------------------------------------------------------------


if SMOKE_MODE:

    @tool
    def classify_intent(query: str) -> str:
        """Classify a customer query as billing, technical, account, or other.

        Returns one of: "billing", "technical", "account", "other".
        """
        q = query.lower()
        if any(w in q for w in ("bill", "invoice", "charge", "payment", "refund")):
            return "billing"
        if any(w in q for w in ("error", "bug", "broken", "crash", "not working")):
            return "technical"
        if any(w in q for w in ("password", "login", "account", "email", "username",
                                "preference", "notification", "channel")):
            return "account"
        return "other"

    @tool
    def get_recent_orders(customer_id: str) -> list[dict]:
        """Return a canned list of the customer's last 5 orders (smoke stub).

        Real deployments wire this against ``main.sales.orders`` via
        ``Dependencies.Workspace`` + ``run_sql``.
        """
        return [
            {"order_id": f"ord-{customer_id}-001", "total": 42.50,
             "status": "shipped", "ordered_at": "2026-05-12"},
            {"order_id": f"ord-{customer_id}-002", "total": 199.00,
             "status": "delivered", "ordered_at": "2026-05-05"},
            {"order_id": f"ord-{customer_id}-003", "total": 17.99,
             "status": "delivered", "ordered_at": "2026-04-28"},
        ]

    @tool
    def format_address(street: str, city: str, region: str, postal_code: str) -> str:
        """Format a postal address into a canonical multi-line string."""
        return f"{street}\n{city}, {region} {postal_code}"

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

    @tool
    def ask_account_data(question: str) -> str:
        """Answer a natural-language question about a customer's account (smoke stub).

        Real deployments use ``genie_tool($ACCOUNT_GENIE_SPACE_ID)``.
        """
        return (
            "Account record (smoke stub): plan=standard, opened 2024-08-14, "
            "primary contact on file. For exact data, query the production "
            "Genie space."
        )

    billing_tools = [get_recent_orders, format_address]
    technical_tools = [docs_search]
    account_extra_tools = [ask_account_data]

else:
    # Production-path imports kept lazy so SMOKE_MODE deploys don't import
    # langchain machinery that isn't used in the stub path.
    from apx_agent import Dependencies, genie_tool, vector_search_tool

    @tool(uc="main.agent_tools.classify_intent", grant=["agent_consumers"])
    def classify_intent(query: str) -> str:
        """Classify a customer query as billing, technical, account, or other."""
        q = query.lower()
        if any(w in q for w in ("bill", "invoice", "charge", "payment", "refund")):
            return "billing"
        if any(w in q for w in ("error", "bug", "broken", "crash", "not working")):
            return "technical"
        if any(w in q for w in ("password", "login", "account", "email", "username")):
            return "account"
        return "other"

    @tool(uc="main.agent_tools.format_address", grant=["agent_consumers"])
    def format_address(street: str, city: str, region: str, postal_code: str) -> str:
        """Format a postal address into a canonical multi-line string."""
        return f"{street}\n{city}, {region} {postal_code}"

    @tool
    def get_recent_orders(
        customer_id: str,
        ws: Dependencies.Workspace,
    ) -> list[dict]:
        """Return the customer's last 5 orders from the orders table."""
        from apx_agent import run_sql
        return run_sql(
            ws,
            "SELECT order_id, total, status, ordered_at FROM main.sales.orders "
            "WHERE customer_id = :cid ORDER BY ordered_at DESC LIMIT 5",
            parameters=[{"name": "cid", "value": customer_id, "type": "STRING"}],
        )

    billing_tools = [get_recent_orders, format_address]
    technical_tools = [
        vector_search_tool(
            "main.support.docs_index",
            columns=["doc_id", "title", "content", "url"],
            num_results=5,
            name="docs_search",
            description="Search the support documentation index.",
        ),
    ]
    account_extra_tools = [
        genie_tool(
            os.environ.get("ACCOUNT_GENIE_SPACE_ID", "$ACCOUNT_GENIE_SPACE_ID"),
            name="ask_account_data",
            description="Ask a natural-language question about a customer's account.",
        ),
    ]


# ---------------------------------------------------------------------------
# Memory store — seeded once per Apps worker. Same wiring as the original
# Model-Serving example.
# ---------------------------------------------------------------------------


account_memory_store = InMemoryMemoryStore()


_SEED_MEMORIES: dict[str, list[dict[str, object]]] = {
    "user:alice": [
        {"content": "Prefers email over SMS for account notifications.",
         "tags": ("preference", "channel")},
        {"content": "Primary email is alice@example.com; recovery is alice.r@example.com.",
         "tags": ("profile", "contact")},
        {"content": "MFA reset on 2026-04-12 — used SMS to +1-555-0144.",
         "tags": ("episodic", "security")},
    ],
    "user:bob": [
        {"content": "Prefers Spanish-language support replies.",
         "tags": ("preference", "language")},
        {"content": "Primary email is bob@example.com; no recovery email on file.",
         "tags": ("profile", "contact")},
        {"content": "Password reset on 2026-05-01 — completed self-serve.",
         "tags": ("episodic", "security")},
    ],
}
for _principal, _seeds in _SEED_MEMORIES.items():
    for _seed in _seeds:
        account_memory_store.add({
            "principal_id": _principal,
            "namespace": "profile",
            "content": _seed["content"],
            "tags": list(_seed["tags"]),  # type: ignore[arg-type]
        })


# default_principal_id is load-bearing for the smoke test — no per-request
# principal resolution wired here, so every caller gets alice's memories.
# That's what makes the across-handoff memory verification deterministic.
account_memory_tools = make_memory_tools(
    store=account_memory_store,
    default_principal_id="user:alice",
    namespace_default="profile",
)


# ---------------------------------------------------------------------------
# Specialist agents.
# ---------------------------------------------------------------------------


billing_agent = Agent(
    name="billing_specialist",
    instructions=(
        "You're a billing specialist. Answer questions about invoices, charges, "
        "refunds, and payment methods. Use get_recent_orders to look up the "
        "customer's order history when relevant."
    ),
    tools=billing_tools,
)


technical_agent = Agent(
    name="technical_specialist",
    instructions=(
        "You're a technical specialist. Answer questions about product errors, "
        "outages, and integration issues. Use the docs_search tool to find "
        "relevant troubleshooting articles before answering."
    ),
    tools=technical_tools,
)


account_agent = Agent(
    name="account_specialist",
    instructions=(
        "You're an account specialist. Help with password resets, email changes, "
        "and account access.\n"
        "\n"
        "Memory: call `recall` first with a query that captures what you want "
        "to know about the user (e.g. 'preferred notification channel', "
        "'recovery email on file'). Use the returned facts to personalize your "
        "answer. When the user shares a new preference or fact worth keeping, "
        "call `remember` with content that future turns will benefit from. "
        "Memories are keyed by the calling user, not by this conversation — "
        "they persist across handoffs and across sessions.\n"
        "\n"
        "Use ask_account_data for live account-record lookups via the Genie space."
    ),
    tools=[*account_memory_tools, *account_extra_tools],
)


# ---------------------------------------------------------------------------
# Top-level triage agent.
# ---------------------------------------------------------------------------


triage_classifier = Agent(
    name="triage",
    instructions=(
        "You're a customer support triage agent. First, call classify_intent on "
        "the user's question. Then call exactly one of the transfer tools based "
        "on the result:\n"
        "  billing   -> transfer_to_billing_specialist\n"
        "  technical -> transfer_to_technical_specialist\n"
        "  account   -> transfer_to_account_specialist\n"
        "If intent is 'other', answer directly with a polite acknowledgment and "
        "do not transfer."
    ),
    tools=[classify_intent],
)


agent = HandoffAgent(
    agents={
        "triage": triage_classifier,
        "billing_specialist": billing_agent,
        "technical_specialist": technical_agent,
        "account_specialist": account_agent,
    },
    start="triage",
)


# ---------------------------------------------------------------------------
# Compile to the ResponsesAgent contract and register Apps entry points.
# ---------------------------------------------------------------------------


_invoke_fn, _stream_fn = compile_to_responses_agent(agent, model=MODEL)


@invoke()
def non_streaming(request):
    """Synchronous request handler — Databricks Apps /invocations target."""
    return _invoke_fn(request)


@stream()
def streaming(request):
    """Streaming request handler — yields ResponsesAgentStreamEvent chunks."""
    yield from _stream_fn(request)
