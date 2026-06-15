"""Claim-vs-reality checks on the few-shot example tools (via the vendored ctk kit).

``save_example`` returns a success string the LLM trusts — ``"Saved example <id>."`` —
and ``remove_example`` likewise reports ``"Removed example <id>."``. The existing
``test_example_tools.py`` seeds the store *directly* with ``store.add(...)`` before
exercising ``find_examples``, so the tools' own write claims are never reconciled
against reality. A ``save_example`` that returned its success string without
persisting (or a ``remove_example`` that reported a delete that didn't happen)
would slip past a string-only assertion but fails here: the full
save -> find -> remove -> find lifecycle is driven through the tool interface,
and ``claim_vs_reality`` reconciles each reported success against an independent
read-back.

Uses ``ctk`` (python/.ctk, on the path via ``pythonpath`` in pyproject) — the
claim-vs-reality pattern is what that kit is for.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from apx_agent import InMemoryExampleStore, make_example_tools
from ctk import claim_vs_reality, expect


def _find_tool(tools: list[Any], name: str) -> Any:
    for t in tools:
        if t.__name__ == name:
            return t
    raise AssertionError(f"tool not found: {name} (have: {[t.__name__ for t in tools]})")


@pytest.mark.unit
def test_example_tool_lifecycle_claims_match_reality() -> None:
    store = InMemoryExampleStore()
    tools = make_example_tools(store=store, default_agent_id="agent-1")
    save = _find_tool(tools, "save_example")
    find = _find_tool(tools, "find_examples")
    remove = _find_tool(tools, "remove_example")

    # 1. The write tool CLAIMS success and reports an id.
    claim = save(input="How many employees had a mismatch?", output="Three did.")
    expect(claim).nonempty().contains("Saved example").verify()
    m = re.search(r"Saved example (\S+?)\.", claim)
    assert m, f"save_example did not report an id: {claim!r}"
    example_id = m.group(1)

    # ...claim-vs-reality: the example must actually be retrievable through the
    # SAME tool interface, not merely reported as saved. A save that returned
    # the success string without persisting trips this.
    claim_vs_reality(
        claimed_success=True,
        verifier=lambda: expect(find(query="mismatch")).contains("Three did.").verify(),
        claim_label="save_example",
    )

    # 2. The delete tool CLAIMS success...
    removed = remove(id=example_id)
    expect(removed).contains("Removed example").verify()

    # ...claim-vs-reality: after a reported delete, the content must be gone.
    claim_vs_reality(
        claimed_success=True,
        verifier=lambda: expect(find(query="mismatch")).not_matches("Three did.").verify(),
        claim_label="remove_example",
    )
