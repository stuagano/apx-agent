"""Violations router — read-only view of violations, scans, and resources.

Thin wrapper over GovernanceProvider. All data logic lives in the provider.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ontos_governance.models import ResourceFilters, ViolationFilters
from ontos_governance.provider import GovernanceProvider
from ontos_governance.routers._deps import get_provider

router = APIRouter(tags=["governance"])


# ── Violations ────────────────────────────────────────────────────────────────


@router.get("/violations/summary")
def violations_summary(provider: GovernanceProvider = Depends(get_provider)):
    return provider.violations_summary()


@router.get("/violations")
def list_violations(
    active: bool = True,
    severity: str | None = None,
    policy_id: str | None = None,
    resource_id: str | None = None,
    domain: str | None = None,
    limit: int = 200,
    offset: int = 0,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.list_violations(ViolationFilters(
        active=active, severity=severity, policy_id=policy_id,
        resource_id=resource_id, domain=domain, limit=limit, offset=offset,
    ))


# ── Scans ─────────────────────────────────────────────────────────────────────


@router.get("/scans")
def list_scans(limit: int = 50, provider: GovernanceProvider = Depends(get_provider)):
    return provider.list_scans(limit=limit)


@router.get("/scans/{scan_id}")
def get_scan(scan_id: str, provider: GovernanceProvider = Depends(get_provider)):
    try:
        return provider.get_scan(scan_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Scan {scan_id} not found")


# ── Resources ─────────────────────────────────────────────────────────────────


@router.get("/resources")
def list_resources(
    resource_type: str | None = None,
    scan_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.list_resources(ResourceFilters(
        resource_type=resource_type, scan_id=scan_id, limit=limit, offset=offset,
    ))


@router.get("/resources/{resource_id}")
def get_resource(
    resource_id: str,
    provider: GovernanceProvider = Depends(get_provider),
):
    try:
        return provider.get_resource(resource_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Resource {resource_id} not found")
