"""Shared FastAPI dependencies for governance routers."""

from __future__ import annotations

from fastapi import Request

from ontos_governance.provider import GovernanceProvider


def get_provider() -> GovernanceProvider:
    """Overridden at registration time by ``register_routes()``."""
    raise RuntimeError(
        "GovernanceProvider not configured. "
        "Call register_routes(app) or set the dependency override."
    )


def get_current_user(request: Request) -> str:
    """Extract caller identity from Databricks Apps forwarded token header."""
    return request.headers.get("x-forwarded-user", "unknown")
