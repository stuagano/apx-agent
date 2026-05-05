from typing import Any

from pydantic import BaseModel, Field
from ... import __version__


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls) -> "VersionOut":
        return cls(version=__version__)


class AfrApplication(BaseModel):
    """Normalized AFR application record passed to the agent."""
    applicant_name: str
    address: str = ""
    account_number: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)  # original unprocessed fields


class Candidate(BaseModel):
    """One potential match returned by a search tool."""
    account_id: str
    name: str
    address: str = ""
    account_number: str = ""
    score: float  # 0.0–1.0 similarity


class EnrollmentDecision(BaseModel):
    """Final output written to afr_processing."""
    matched: bool
    account_id: str | None = None  # canonical match, if any
    category: str  # "EXACT", "HIGH_CONFIDENCE", "LOW_CONFIDENCE", "NO_MATCH"
    rationale: str
    confidence: float  # 0.0–1.0
    candidates_reviewed: int
