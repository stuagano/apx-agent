from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class AfrApplication(BaseModel):
    applicant_name: str = Field(..., description="Applicant full name — required")
    address: str = Field("", description="Service address")
    email: str = Field("", description="Email — used for maiden-name matching")
    account_number: str = Field("", description="Utility account number — exact match boosts confidence")
    tenant_id: str = Field("", description="Utility tenant ID — restricts search to one utility")


class EnrollmentDecision(BaseModel):
    matched: bool
    account_id: str | None
    category: str = Field(..., description="EXACT | HIGH_CONFIDENCE | LOW_CONFIDENCE | NO_MATCH")
    rationale: str
    confidence: float
    candidates_reviewed: int
