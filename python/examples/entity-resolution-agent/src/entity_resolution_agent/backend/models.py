from pydantic import BaseModel
from .. import __version__


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls):
        return cls(version=__version__)


class AfrApplication(BaseModel):
    """Normalized AFR application record passed to the agent."""
    applicant_name: str
    address: str = ""
    email: str = ""
    account_number: str = ""
    tenant_id: str = ""
    raw: dict = {}


class Candidate(BaseModel):
    """One potential match returned by a search tool."""
    account_id: str
    name: str
    address: str = ""
    account_number: str = ""
    score: float


class EnrollmentDecision(BaseModel):
    """Final output written to afr_processing."""
    matched: bool
    account_id: str | None = None
    category: str  # "EXACT", "HIGH_CONFIDENCE", "LOW_CONFIDENCE", "NO_MATCH"
    rationale: str
    confidence: float
    candidates_reviewed: int
