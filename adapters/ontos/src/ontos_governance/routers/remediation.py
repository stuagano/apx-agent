"""Remediation router — review queue + dashboard endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ontos_governance.models import ProposalFilters, ReviewAction
from ontos_governance.provider import GovernanceProvider
from ontos_governance.routers._deps import get_current_user, get_provider

router = APIRouter(prefix="/remediation", tags=["remediation"])


@router.get("/funnel")
def remediation_funnel(provider: GovernanceProvider = Depends(get_provider)):
    return provider.remediation_funnel()


@router.get("/agents")
def agent_effectiveness(provider: GovernanceProvider = Depends(get_provider)):
    return provider.agent_effectiveness()


@router.get("/reviewer-load")
def reviewer_load(provider: GovernanceProvider = Depends(get_provider)):
    return provider.reviewer_load()


@router.get("/proposals")
def list_proposals(
    status: str = "pending_review",
    limit: int = 200,
    offset: int = 0,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.list_proposals(ProposalFilters(
        status=status, limit=limit, offset=offset,
    ))


@router.get("/proposals/{proposal_id}")
def get_proposal(
    proposal_id: str,
    provider: GovernanceProvider = Depends(get_provider),
):
    try:
        return provider.get_proposal(proposal_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")


@router.post("/proposals/{proposal_id}/review")
def submit_review(
    proposal_id: str,
    body: ReviewAction,
    user: str = Depends(get_current_user),
    provider: GovernanceProvider = Depends(get_provider),
):
    if body.decision == "reassigned" and not body.reassigned_to:
        raise HTTPException(
            status_code=422,
            detail="reassigned_to is required when decision is 'reassigned'",
        )
    try:
        return provider.submit_review(
            proposal_id,
            body.decision,
            body.reasoning,
            reassigned_to=body.reassigned_to,
            reviewer=user,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
