"""Exceptions router — approve, revoke, and review policy exceptions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ontos_governance.models import ExceptionFilters, ExceptionRequest
from ontos_governance.provider import GovernanceProvider
from ontos_governance.routers._deps import get_current_user, get_provider

router = APIRouter(prefix="/exceptions", tags=["exceptions"])


@router.get("")
def list_exceptions(
    active: bool = True,
    expiring_soon: bool = False,
    resource_id: str | None = None,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.list_exceptions(ExceptionFilters(
        active=active, expiring_soon=expiring_soon, resource_id=resource_id,
    ))


@router.get("/summary")
def exceptions_summary(provider: GovernanceProvider = Depends(get_provider)):
    return provider.exceptions_summary()


@router.get("/resource/{resource_id}")
def exceptions_for_resource(
    resource_id: str,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.exceptions_for_resource(resource_id)


@router.post("", status_code=201)
def approve_exceptions(
    body: ExceptionRequest,
    user: str = Depends(get_current_user),
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.approve_exceptions(
        resource_id=body.resource_id,
        policy_ids=body.policy_ids,
        justification=body.justification,
        expires_days=body.expires_days,
        approved_by=user,
    )


@router.delete("/{exception_id}")
def revoke_exception(
    exception_id: str,
    user: str = Depends(get_current_user),
    provider: GovernanceProvider = Depends(get_provider),
):
    try:
        return provider.revoke_exception(exception_id, revoked_by=user)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/bulk-revoke-expired")
def bulk_revoke_expired(
    user: str = Depends(get_current_user),
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.bulk_revoke_expired(revoked_by=user)
