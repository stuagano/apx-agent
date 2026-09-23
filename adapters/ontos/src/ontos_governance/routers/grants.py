"""Grants router — read-only view of grant resources from Governance inventory.

Thin wrapper over GovernanceProvider. All data logic lives in the provider.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ontos_governance.provider import GovernanceProvider
from ontos_governance.routers._deps import get_provider

router = APIRouter(tags=["governance"])


@router.get("/grants")
def list_grants(
    resource_id: str | None = None,
    grantee: str | None = None,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.list_grants(resource_id=resource_id, grantee=grantee)


@router.get("/grants/summary/{resource_id:path}")
def grant_summary(
    resource_id: str,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.grant_summary(resource_id)
