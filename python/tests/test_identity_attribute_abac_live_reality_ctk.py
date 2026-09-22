"""Opt-in LIVE capability proof (#755): identity-attribute ABAC across A → B.

Proves the platform half of the contract: the same governed operation on the
same UC object, reached through a real ``RemoteDatabricksAgent`` A → B call,
yields the native allowed outcome for one controlled user and the native
denial for the other, because UC evaluates each user's own identity
attributes from their forwarded credential.

This test never runs in default CI. It skips — with an explicit UNVERIFIED
reason — unless the operator provides all of:

  APX_ABAC_WORKSPACE_HOST    workspace host (https://...) used for the
                             account-SCIM ``/Me`` attribute read-back
  APX_ABAC_ALLOWED_USER_TOKEN  access token of the user expected to be allowed
  APX_ABAC_DENIED_USER_TOKEN   access token of the user expected to be denied
  APX_ABAC_REMOTE_CARD_URL   card URL of the remote app whose governed tool
                             queries the identity-attribute-protected object
  APX_ABAC_QUERY             (optional) the natural-language query to send;
                             defaults to a generic protected-table read

Setup prerequisites (operator-owned, platform-side): the identity-attributes
Beta preview enabled for the account, IdP-provisioned attribute values that
intentionally differ between the two users, and a UC ABAC policy whose
allowed/denied outcome keys off that attribute. The attribute control list is
console-only and values are IdP-sourced, so no test can provision this state
programmatically — when it cannot be read back, this test reports UNVERIFIED
rather than inventing a fallback.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

pytest.importorskip("httpx")

DENIAL_SIGNALS = ("denied", "permission", "not authorized", "unauthorized", "forbidden")

DEFAULT_QUERY = "Read the identity-attribute-protected table and summarize one row."


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _required_env() -> dict[str, str]:
    required = (
        "APX_ABAC_WORKSPACE_HOST",
        "APX_ABAC_ALLOWED_USER_TOKEN",
        "APX_ABAC_DENIED_USER_TOKEN",
        "APX_ABAC_REMOTE_CARD_URL",
    )
    values = {name: _env(name) for name in required}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        pytest.skip(
            "APX-ABAC-PROOF UNVERIFIED (not configured): set "
            + ", ".join(missing)
            + " — see docs/reference/identity-attribute-abac.md"
        )
    return {name: value for name, value in values.items() if value is not None}


def _read_own_identity_attributes(host: str, token: str) -> dict[str, Any] | None:
    """Read the user's own identity attributes via account-SCIM-for-workspaces.

    Returns the attribute mapping when the preview is enabled and values are
    provisioned; ``None`` when the surface or the attributes are unavailable.
    """
    import httpx

    resp = httpx.get(
        f"{host.rstrip('/')}/api/2.0/account/scim/v2/Me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if resp.status_code != 200:
        return None
    body = resp.json()
    attributes = body.get("identityAttributes") or body.get("identity_attributes")
    if not isinstance(attributes, dict) or not attributes:
        return None
    return attributes


def _denial_observed(text: str) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in DENIAL_SIGNALS)


async def _run_governed_query(card_url: str, user_token: str, query: str) -> str:
    """Drive one governed query through the real RemoteDatabricksAgent wire.

    Uses the class's real trusted-origin header gate and its real HTTP POST
    path. The SDK-supervisor path is bypassed deliberately: it binds ambient
    credentials, which cannot deterministically represent the two controlled
    users this proof compares.
    """
    from apx_agent._remote import RemoteDatabricksAgent

    agent = await RemoteDatabricksAgent.from_card_url(card_url)
    incoming = {
        "Authorization": f"Bearer {user_token}",
        "X-Forwarded-Access-Token": user_token,
    }
    return await agent._call_via_http(
        [{"role": "user", "content": query}],
        agent._obo_headers(incoming),
    )


@pytest.mark.asyncio
async def test_identity_attribute_abac_allowed_vs_denied_live() -> None:
    env = _required_env()
    host = env["APX_ABAC_WORKSPACE_HOST"]
    allowed_token = env["APX_ABAC_ALLOWED_USER_TOKEN"]
    denied_token = env["APX_ABAC_DENIED_USER_TOKEN"]
    card_url = env["APX_ABAC_REMOTE_CARD_URL"]
    query = _env("APX_ABAC_QUERY") or DEFAULT_QUERY

    allowed_attributes = _read_own_identity_attributes(host, allowed_token)
    denied_attributes = _read_own_identity_attributes(host, denied_token)
    if allowed_attributes is None or denied_attributes is None:
        pytest.skip(
            "APX-ABAC-PROOF UNVERIFIED: identity attributes not readable via "
            "account-SCIM /Me for both controlled users (preview disabled, "
            "attributes unprovisioned, or endpoint unavailable). Per #755, no "
            "fallback is invented."
        )
    assert allowed_attributes is not None and denied_attributes is not None

    if allowed_attributes == denied_attributes:
        pytest.skip(
            "APX-ABAC-PROOF UNVERIFIED: the two controlled users have identical "
            "identity attributes, so the policy cannot produce opposite outcomes."
        )

    attribute_values = {
        str(value)
        for attributes in (allowed_attributes, denied_attributes)
        for value in attributes.values()
        if value
    }

    allowed_text = await _run_governed_query(card_url, allowed_token, query)
    denied_error: str | None = None
    denied_text = ""
    try:
        denied_text = await _run_governed_query(card_url, denied_token, query)
    except Exception as exc:  # native denial surfacing as an error is valid
        denied_error = str(exc)

    assert allowed_text.strip() and not _denial_observed(allowed_text), (
        "APX-ABAC-PROOF FAILED: the allowed user did not get a clean governed "
        f"result. Got: {allowed_text[:400]}"
    )

    denied_observed = denied_error is not None or _denial_observed(denied_text)
    assert denied_observed, (
        "APX-ABAC-PROOF FAILED: the denied user received the same clean outcome "
        "as the allowed user — the identity-attribute policy was not enforced "
        "across the remote hop. "
        f"allowed[:200]={allowed_text[:200]!r} denied[:200]={denied_text[:200]!r}"
    )

    for value in attribute_values:
        assert value not in allowed_text, (
            "APX-ABAC-PROOF FAILED: a raw identity-attribute value appeared in "
            "the allowed user's response payload."
        )
        assert value not in denied_text and (
            denied_error is None or value not in denied_error
        ), (
            "APX-ABAC-PROOF FAILED: a raw identity-attribute value appeared in "
            "the denied user's outcome."
        )
