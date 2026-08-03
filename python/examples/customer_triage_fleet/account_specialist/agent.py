"""account_specialist: Handles account access, password resets, preferences.

Standalone apx-agent app with principal-keyed semantic memory via
``InMemoryMemoryStore`` + ``make_memory_tools``. Memory is local to this
specialist's process — callers don't need to know about it.

APX_SMOKE_MODE=1 swaps workspace-dependent tools for in-process stubs.
"""
from __future__ import annotations

import os

from apx_agent import Agent, InMemoryMemoryStore, make_memory_tools, tool

SMOKE_MODE = os.environ.get("APX_SMOKE_MODE", "0") == "1"


if SMOKE_MODE:

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

    account_extra_tools = [ask_account_data]

else:
    from apx_agent import genie_tool

    account_extra_tools = [
        genie_tool(
            os.environ.get("ACCOUNT_GENIE_SPACE_ID", "$ACCOUNT_GENIE_SPACE_ID"),
            name="ask_account_data",
            description="Ask a natural-language question about a customer's account.",
        ),
    ]


# --- Memory ---

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

account_memory_tools = make_memory_tools(
    store=account_memory_store,
    default_principal_id="user:alice",
    namespace_default="profile",
)


agent = Agent(
    name="account_specialist",
    description=(
        "Handles account access: password resets, email changes, and login "
        "issues, personalized from remembered user preferences. Call for "
        "questions about accessing or changing account settings. Returns account "
        "guidance and live account-record lookups."
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
