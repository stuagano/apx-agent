from __future__ import annotations

from fastapi import APIRouter

from apx_agent import Dependencies

from .models import AfrApplication, EnrollmentDecision
from .core.search_client import search_accounts
from .core.evaluator import evaluate_candidates, log_decision

router = APIRouter(prefix="/api")


@router.post("/enroll", response_model=EnrollmentDecision, operation_id="enroll")
def enroll(application: AfrApplication, ws: Dependencies.Workspace):
    """Submit an AFR application for deterministic account matching.

    Pipeline: search (via account-search-service or local VS/SQL) → evaluate → log decision.
    No LLM call — suitable for batch AFR queue processing. For ambiguous edge cases that need
    LLM reasoning, deploy entity-resolution-agent and use the /api/chat interface instead.
    """
    search_result = search_accounts(
        name=application.applicant_name,
        address=application.address,
        email=application.email,
        tenant_id=application.tenant_id,
        account_number=application.account_number,
        ws=ws,
    )

    applicant_dict = {
        "name": search_result.get("normalized_name", application.applicant_name.strip().title()),
        "address": search_result.get("normalized_address", application.address.strip().title()),
        "account_number": application.account_number.strip(),
        "email": application.email,
    }
    evaluation = evaluate_candidates(
        applicant=applicant_dict,
        candidates=search_result.get("candidates", []),
        ws=ws,
    )
    decision = evaluation["decision"]

    log_decision(decision={**decision, "applicant_name": applicant_dict["name"]}, ws=ws)

    return EnrollmentDecision(
        matched=decision["matched"],
        account_id=decision.get("account_id"),
        category=decision["category"],
        rationale=decision["rationale"],
        confidence=decision["confidence"],
        candidates_reviewed=decision["candidates_reviewed"],
    )
