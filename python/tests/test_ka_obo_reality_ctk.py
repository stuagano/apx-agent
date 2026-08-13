"""Claim-vs-reality (ctk): the KA tool returns a *grounded* answer with a source
citation, and does so as the calling user (OBO) — PRD AC-5.

Two tiers:

* ``test_ka_grounded_contract_mocked`` — cheap, always runs. Proves the tool's
  read-back contract: given a KA response, the tool yields a non-empty answer
  AND ≥1 citation carrying a ``doc_uri`` (not merely that the SDK was called).
* ``test_live_ka_grounded_obo`` — the live integration gate. Skips unless a real
  KA endpoint + profile are supplied; then queries the deployed KA as the
  profile's user and asserts a non-empty grounded answer with a 10-K citation.
  Requires Phase 0 KA on fe-stable + user authorization (OBO Public Preview).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from apx_agent import knowledge_assistant_tool


def _ka_response(answer: str, citations: list[dict] | None) -> dict:
    """KA payload in the Responses API shape returned by /invocations."""
    return {
        "object": "response",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": answer, "annotations": []}],
            }
        ],
        "citations": citations,
    }


@pytest.mark.asyncio
async def test_ka_grounded_contract_mocked():
    """Cheap reality check: the grounded-result contract holds end-to-end."""
    ws = MagicMock()
    ws.api_client.do.return_value = _ka_response(
        "Apple's FY2023 net revenue was $383.3B.",
        [{"doc_uri": "s3://filings/AAPL-10-K-2023.pdf", "text": "Total net sales…"}],
    )
    tool = knowledge_assistant_tool("ka-10k")
    result = await tool(question="What was Apple's FY2023 revenue?", ws=ws)

    assert result["answer"].strip(), "KA answer must be non-empty"
    assert len(result["citations"]) >= 1, "grounded answer must carry ≥1 citation"
    assert result["citations"][0]["doc_uri"], "citation must reference a source doc_uri"


@pytest.mark.asyncio
async def test_live_ka_grounded_obo():
    """AC-5 live gate: real KA on 10-Ks returns a grounded, cited answer as the
    user. Skips unless APX_KA_ENDPOINT_NAME + APX_CAPS_PROFILE are set."""
    endpoint = os.environ.get("APX_KA_ENDPOINT_NAME")
    profile = os.environ.get("APX_CAPS_PROFILE")
    if not (endpoint and profile):
        pytest.skip("live KA gate: set APX_KA_ENDPOINT_NAME + APX_CAPS_PROFILE (Phase 0 + fe-stable OBO)")

    from databricks.sdk import WorkspaceClient

    ws = WorkspaceClient(profile=profile)
    tool = knowledge_assistant_tool(endpoint)
    result = await tool(question="What risk factors does Apple disclose in its 10-K?", ws=ws)

    assert "error" not in result, f"live KA query failed: {result.get('error')}"
    assert result["answer"].strip(), "live KA returned an empty answer"
    assert len(result["citations"]) >= 1, "live KA answer carried no citation"
    assert any(c.get("doc_uri") for c in result["citations"]), "no citation had a source doc_uri"
