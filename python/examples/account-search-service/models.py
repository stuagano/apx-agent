from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    applicant_name: str = Field(..., description="Applicant full name — raw, will be normalized")
    address: str = Field("", description="Service address — strongly recommended")
    email: str = Field("", description="Email address — used for maiden-name index query")
    tenant_id: str = Field("", description="Utility tenant ID — restricts search to one utility")
    k: int = Field(10, description="Candidates to retrieve per index before deduplication")


class Candidate(BaseModel):
    account_id: str
    name: str
    address: str
    account_number: str
    score: float | None = None


class SearchResponse(BaseModel):
    candidates: list[dict[str, Any]]
    count: int
    strategy: str = Field(..., description="'vector' or 'sql'")
    source: str = Field("live", description="'live' or 'demo'")
    normalized_name: str = ""
    normalized_address: str = ""
