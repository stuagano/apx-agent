"""customer_triage: RouterAgent routing support queries to billing, technical, and account specialists.

Top-level ``RouterAgent`` with four mutually exclusive branches (three specialists
+ ``other``). One routing decision, then the chosen branch runs — no triage
handoff round-trip (#600). The ``account_specialist`` is wired with
principal-keyed memory via ``InMemoryMemoryStore`` + ``make_memory_tools``.
Tools and prompts are defined inline; ``APX_SMOKE_MODE=1`` swaps
workspace-dependent tools for in-process stubs so the bundle deploys cleanly on
workspaces without the prerequisite UC / Genie / Vector Search resources.
"""
from __future__ import annotations

import os

from apx_agent import (
    Agent,
    InMemoryMemoryStore,
    RouterAgent,
    make_memory_tools,
    tool,
)

SMOKE_MODE = os.environ.get("APX_SMOKE_MODE", "0") == "1"


if SMOKE_MODE:

    @tool
    def classify_intent(query: str) -> str:
        """Classify a customer query as billing, technical, account, or other.

        Returns one of: ``"billing"``, ``"technical"``, ``"account"``,
        ``"other"``. Kept as a UC-shaped helper for offline checks / publish
        demos; live routing uses each specialist's ``description`` (#600).
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

    billing_tools = [get_recent_orders]
    technical_tools = [docs_search]
    account_extra_tools = [ask_account_data]

else:
    # Production-path imports kept lazy so SMOKE_MODE deploys don't import
    # langchain machinery that isn't used in the stub path.
    from apx_agent import Dependencies, genie_tool, vector_search_tool

    @tool(uc="main.agent_tools.classify_intent", grant=["agent_consumers"])
    def classify_intent(query: str) -> str:
        """Classify a customer query as billing, technical, account, or other.

        Offline / publish helper — live routing uses specialist descriptions.
        """
        q = query.lower()
        if any(w in q for w in ("bill", "invoice", "charge", "payment", "refund")):
            return "billing"
        if any(w in q for w in ("error", "bug", "broken", "crash", "not working")):
            return "technical"
        if any(w in q for w in ("password", "login", "account", "email", "username")):
            return "account"
        return "other"

    @tool
    def get_recent_orders(
        customer_id: str,
        ws: Dependencies.Workspace,
    ) -> list[dict]:
        """Return the customer's last 5 orders from the orders table.

        Runs as the calling user; UC grants on ``main.sales.orders`` apply.
        """
        from apx_agent import run_sql

        return run_sql(
            ws,
            "SELECT order_id, total, status, ordered_at FROM main.sales.orders "
            "WHERE customer_id = :cid ORDER BY ordered_at DESC LIMIT 5",
            parameters=[{"name": "cid", "value": customer_id, "type": "STRING"}],
        )

    billing_tools = [get_recent_orders]
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


# Per-request OBO identity (X-Forwarded-User) via Dependencies.Principal.
# default_principal_id keeps local/smoke runs working when no Apps header is
# present — DEPLOY BLOCKER: multi-user production must not rely on the alice
# fallback alone (every unauthenticated caller would share one memory scope).
account_memory_tools = make_memory_tools(
    store=account_memory_store,
    default_principal_id="user:alice",
    namespace_default="profile",
    _use_dep_principal=True,
)


billing_agent = Agent(
    name="billing_specialist",
    description=(
        "Handles billing inquiries: invoices, charges, refunds, payments, "
        "and order history."
    ),
    instructions=(
        "You're a billing specialist. Answer questions about invoices, charges, "
        "refunds, and payment methods. Use get_recent_orders to look up the "
        "customer's order history when relevant."
    ),
    tools=billing_tools,
)


technical_agent = Agent(
    name="technical_specialist",
    description=(
        "Handles technical support: product errors, outages, bugs, crashes, "
        "and integration troubleshooting."
    ),
    instructions=(
        "You're a technical specialist. Answer questions about product errors, "
        "outages, and integration issues. Use the docs_search tool to find "
        "relevant troubleshooting articles before answering."
    ),
    tools=technical_tools,
)


account_agent = Agent(
    name="account_specialist",
    description=(
        "Handles account access: password resets, login/email changes, "
        "notification preferences, and profile facts."
    ),
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
        "they persist across sessions.\n"
        "\n"
        "Use ask_account_data for live account-record lookups via the Genie space."
    ),
    tools=[*account_memory_tools, *account_extra_tools],
)


other_agent = Agent(
    name="other",
    description=(
        "General acknowledgments when the query is not billing, technical, or "
        "account related (greetings, off-topic, unclear intent)."
    ),
    instructions=(
        "Politely acknowledge the user's message. Say you can help with billing, "
        "technical, or account questions, and ask them to rephrase if needed. "
        "Do not invent specialist answers."
    ),
    tools=[],
)


agent = RouterAgent(
    agents=[billing_agent, technical_agent, account_agent, other_agent],
    instructions=(
        "Route each customer support query to exactly one specialist. "
        "billing_specialist for invoices, charges, refunds, and payments; "
        "technical_specialist for errors, outages, bugs, and integrations; "
        "account_specialist for password, login, email, and preferences; "
        "other for greetings or anything outside those topics."
    ),
)
