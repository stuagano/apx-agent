from databricks.sdk.service.iam import User as UserOut
from fastapi import APIRouter

from apx_agent import Dependencies

from .models import AfrApplication, EnrollmentDecision, VersionOut

router = APIRouter(prefix="/api")


@router.get("/version", response_model=VersionOut, operation_id="version")
async def version():
    return VersionOut.from_metadata()


@router.get("/current-user", response_model=UserOut, operation_id="currentUser")
def me(user_ws: Dependencies.UserClient):
    return user_ws.current_user.me()


@router.post("/enroll", response_model=EnrollmentDecision, operation_id="enroll")
def enroll(application: AfrApplication, ws: Dependencies.Workspace):
    """Submit an AFR application for account matching.

    Deterministic path: normalize → vector/SQL search → evaluate → log.
    Suitable for batch AFR queue processing. For LLM-powered reasoning on
    ambiguous edge cases, use the /api/chat interface instead.
    """
    from .core.supervisor import normalize_record, sql_search, vector_search
    from .core.evaluator import evaluate_candidates, log_decision

    normalized = normalize_record(
        name=application.applicant_name,
        address=application.address,
        account_number=application.account_number,
        ws=ws,
    )

    if normalized["strategy"] == "vector":
        search_result = vector_search(
            applicant_name=normalized["name"],
            address=normalized["address"],
            email=application.email,
            tenant_id=application.tenant_id,
            ws=ws,
        )
    else:
        search_result = sql_search(
            name=normalized["name"],
            address=normalized["address"],
            ws=ws,
        )

    applicant_dict = {
        "name": normalized["name"],
        "address": normalized["address"],
        "account_number": normalized["account_number"],
        "email": application.email,
    }
    evaluation = evaluate_candidates(
        applicant=applicant_dict,
        candidates=search_result.get("candidates", []),
        ws=ws,
    )
    decision = evaluation["decision"]

    log_decision(decision={**decision, "applicant_name": normalized["name"]}, ws=ws)

    return EnrollmentDecision(
        matched=decision["matched"],
        account_id=decision.get("account_id"),
        category=decision["category"],
        rationale=decision["rationale"],
        confidence=decision["confidence"],
        candidates_reviewed=decision["candidates_reviewed"],
    )
