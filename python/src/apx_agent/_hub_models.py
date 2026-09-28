"""Hub agent card models ported from the standalone hub deployable.

Defines the Pydantic models used for agent registration, invocation, and
discovery in the folded hub-into-dev-ui architecture.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, HttpUrl, TypeAdapter, field_validator

_HttpUrlAdapter = TypeAdapter(HttpUrl)


def is_trusted_agent_url(url: str) -> bool:
    """True if ``url`` is an https URL on the trusted-host allowlist.

    Parses the URL and matches on the *hostname* (never a raw-string suffix
    test, which would accept ``evil-databricksapps.com`` or a userinfo trick
    like ``https://databricksapps.com@evil.com``).

    The allowlist check is delegated to :func:`apx_agent._ui_probe._is_trusted_agent_host`,
    the single source of truth for host validation across register and wire paths.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # Use the shared allowlist check from _ui_probe.
    from ._ui_probe import _is_trusted_agent_host

    return _is_trusted_agent_host(host)


class AgentTool(BaseModel):
    """Tool exposed by an agent."""

    name: str
    description: str


class AgentCard(BaseModel):
    """Registration card for an A2A agent."""

    id: str
    name: str
    display_name: str
    description: str
    status: Literal["live", "unreachable", "stub", "planned"] = "stub"
    url: str
    tools: list[AgentTool]
    tags: list[str] = []
    mcp_endpoint: str | None = None
    last_seen: datetime | None = None
    supports_invoke: bool = False


class RegisterRequest(BaseModel):
    """Request to register an agent URL with the hub."""

    url: str
    tags: list[str] = []

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        # Validate as a well-formed HTTP(S) URL...
        try:
            _HttpUrlAdapter.validate_python(v)
        except Exception as exc:
            raise ValueError(f"Invalid agent URL: {v}") from exc
        # ...and reject any host not on the trusted-host allowlist, so a
        # registrant cannot point the hub (and the forwarded OBO token) at an
        # arbitrary attacker-controlled host.
        if not is_trusted_agent_url(v):
            raise ValueError(
                "Agent URL host is not on the trusted allowlist "
                "(*.databricksapps.com or the configured workspace host)"
            )
        return v


class InvokeRequest(BaseModel):
    """Request to invoke an agent."""

    input: str
