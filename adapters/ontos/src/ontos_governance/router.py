"""Root governance router — mounts all sub-routers.

Integration with Ontos (or any FastAPI app):

    from ontos_governance import register_routes
    register_routes(app)   # mounts at /api/governance/*

With a custom provider:

    from ontos_governance import register_routes
    from ontos_governance.providers import GovernanceProvider

    provider = GovernanceProvider(catalog="my_catalog", schema="my_schema", ...)
    register_routes(app, provider=provider)
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from ontos_governance.provider import GovernanceProvider
from ontos_governance.providers.governance import GovernanceProvider as DefaultGovernanceProvider
from ontos_governance.routers._deps import get_provider
from ontos_governance.routers.exceptions import router as exceptions_router
from ontos_governance.routers.grants import router as grants_router
from ontos_governance.routers.ontology import router as ontology_router
from ontos_governance.routers.policies import router as policies_router
from ontos_governance.routers.remediation import router as remediation_router
from ontos_governance.routers.violations import router as violations_router

root_router = APIRouter()
root_router.include_router(violations_router)
root_router.include_router(policies_router)
root_router.include_router(exceptions_router)
root_router.include_router(grants_router)
root_router.include_router(ontology_router)
root_router.include_router(remediation_router)


@root_router.get("/health")
def health():
    """Liveness probe."""
    return {"status": "ok", "module": "governance"}


def register_routes(
    app: FastAPI,
    *,
    provider: GovernanceProvider | None = None,
    prefix: str = "/api/governance",
) -> None:
    """Mount governance routes into a FastAPI application.

    This is the primary integration point. Call once at startup:

        register_routes(app)                       # auto-configure from env
        register_routes(app, provider=my_provider)  # explicit provider

    Args:
        app: The FastAPI application to mount into.
        provider: A GovernanceProvider implementation. Defaults to
            ``ontos_governance.providers.GovernanceProvider.from_env()``.
        prefix: URL prefix for all governance routes.
    """
    if provider is None:
        provider = DefaultGovernanceProvider.from_env()

    app.dependency_overrides[get_provider] = lambda: provider
    app.include_router(root_router, prefix=prefix)
