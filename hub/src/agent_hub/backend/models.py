from __future__ import annotations

import os
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, HttpUrl, TypeAdapter, field_validator

# ---------------------------------------------------------------------------
# Trusted-host allowlist
#
# The hub forwards the caller's Databricks OBO token to the target agent when
# invoking it.  To prevent credential exfiltration / SSRF, the token is only
# ever sent to hosts on this allowlist:
#   * any *.databricksapps.com host (the Databricks Apps domain), and
#   * an optional operator-configured workspace host (DATABRICKS_HOST) for
#     agents served outside the Apps domain.
# This is enforced at registration time AND at invoke time (the authoritative
# gate, since seed/auto-registered agents bypass the register path).
# ---------------------------------------------------------------------------

_APPS_HOST_SUFFIX = ".databricksapps.com"

_HttpUrlAdapter = TypeAdapter(HttpUrl)


def _allowed_host() -> str | None:
    """Return the optional operator-configured trusted host, if any."""
    raw = os.environ.get("DATABRICKS_HOST", "").strip()
    if not raw:
        return None
    # Accept either a bare host or a full URL.
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").lower() or None


def is_trusted_agent_url(url: str) -> bool:
    """True if ``url`` is an https URL on the trusted-host allowlist.

    Parses the URL and matches on the *hostname* (never a raw-string suffix
    test, which would accept ``evil-databricksapps.com`` or a userinfo trick
    like ``https://databricksapps.com@evil.com``).
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
    if host == "databricksapps.com" or host.endswith(_APPS_HOST_SUFFIX):
        return True
    allowed = _allowed_host()
    return allowed is not None and host == allowed


class AgentTool(BaseModel):
    name: str
    description: str


class AgentCard(BaseModel):
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
    input: str


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls) -> "VersionOut":
        try:
            v = version("agent-hub")
        except PackageNotFoundError:
            v = "dev"
        return cls(version=v)
