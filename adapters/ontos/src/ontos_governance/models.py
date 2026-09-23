"""Pydantic models — the data contract between routers and providers.

These models define the shape of data flowing through the governance API.
Routers return these types; providers produce them. Neither side depends
on the other's implementation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Violations ────────────────────────────────────────────────────────────────


class ViolationSummary(BaseModel):
    total: int
    active: int
    critical: int
    high: int
    medium: int
    low: int


class Violation(BaseModel):
    violation_id: str
    resource_id: str
    resource_name: str
    resource_type: str
    policy_id: str
    policy_name: str
    severity: Literal["critical", "high", "medium", "low"]
    domain: str
    first_seen: str
    last_seen: str
    active: bool
    scan_id: str


class ViolationFilters(BaseModel):
    active: bool = True
    severity: str | None = None
    policy_id: str | None = None
    resource_id: str | None = None
    domain: str | None = None
    limit: int = 200
    offset: int = 0


# ── Scans ─────────────────────────────────────────────────────────────────────


class ScanRun(BaseModel):
    scan_id: str
    started_at: str
    finished_at: str
    resources_scanned: int
    evaluations: int
    failures: int


class PolicyBreakdown(BaseModel):
    policy_id: str
    domain: str
    severity: str
    evaluations: int
    failures: int


class ScanDetail(ScanRun):
    policy_breakdown: list[PolicyBreakdown]


# ── Resources ─────────────────────────────────────────────────────────────────


class Resource(BaseModel):
    resource_id: str
    resource_name: str
    resource_type: str
    first_seen: str
    last_seen: str
    scan_id: str
    metadata: Any = None


class Classification(BaseModel):
    class_name: str
    class_ancestors: str | None = None
    root_class: str | None = None
    classified_at: str | None = None


class ResourceViolation(BaseModel):
    violation_id: str
    policy_id: str
    policy_name: str
    severity: str
    domain: str
    first_seen: str
    last_seen: str
    active: bool


class ResourceException(BaseModel):
    exception_id: str
    policy_id: str
    approved_by: str
    justification: str
    approved_at: str
    expires_at: str | None
    active: bool
    expiry_status: str


class ResourceMetric(BaseModel):
    """One row from dq_status — latest value per metric for a resource.

    Populated when the crawler is configured with DQX metrics tables;
    empty otherwise. Values stay as strings (long-format table) —
    consumers parse numerics (e.g. quality_score) as needed.
    """

    source: str
    metric: str
    value: str | None = None
    status: str | None = None
    anomaly: bool | None = None
    checked_at: str | None = None


class ResourceDetail(Resource):
    classifications: list[Classification]
    violations: list[ResourceViolation]
    exceptions: list[ResourceException]
    metrics: list[ResourceMetric] = []


class ResourceFilters(BaseModel):
    resource_type: str | None = None
    scan_id: str | None = None
    limit: int = 200
    offset: int = 0


# ── Policies ──────────────────────────────────────────────────────────────────


class PolicyBase(BaseModel):
    policy_name: str
    applies_to: str = "*"
    domain: str = "User"
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    description: str = ""
    remediation: str = ""
    rule_json: str = Field(..., description="JSON-serialised rule tree")
    active: bool = True


class PolicyCreate(PolicyBase):
    policy_id: str | None = None


class Policy(PolicyBase):
    policy_id: str
    origin: str
    updated_at: str | None = None


class PolicyVersion(BaseModel):
    version: int
    policy_name: str
    applies_to: str
    severity: str
    active: bool
    rule_json: str
    change_type: str
    changed_by: str
    changed_at: str


class PolicyFilters(BaseModel):
    origin: str | None = None
    active: bool | None = None


# ── Exceptions ────────────────────────────────────────────────────────────────


class ExceptionRequest(BaseModel):
    resource_id: str
    policy_ids: list[str] = Field(..., min_length=1)
    justification: str = Field(..., min_length=10)
    expires_days: int | None = Field(
        default=90,
        description="Days until expiry. Null = permanent.",
        ge=1,
        le=730,
    )


class ExceptionRecord(BaseModel):
    exception_id: str
    resource_id: str
    policy_id: str
    approved_by: str
    justification: str
    approved_at: str
    expires_at: str | None
    active: bool
    expiry_status: str


class ExceptionSummary(BaseModel):
    total: int
    active: int
    permanent: int
    expired: int
    expiring_soon: int


class ExceptionFilters(BaseModel):
    active: bool = True
    expiring_soon: bool = False
    resource_id: str | None = None


# ── Ontology ──────────────────────────────────────────────────────────────────


class OntologyClass(BaseModel):
    name: str
    kind: str  # "base" | "derived"
    parent: str | None
    description: str
    matches_resource_types: list[str]
    classifier: Any = None
    ancestry: list[str]
    children: list[str]


class OntologyTreeNode(BaseModel):
    name: str
    kind: str
    description: str
    children: list[OntologyTreeNode]


OntologyTreeNode.model_rebuild()


class OntologyTree(BaseModel):
    roots: list[OntologyTreeNode]
    total_classes: int


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str]
    warnings: list[str]


# ── Grants ───────────────────────────────────────────────────────────────────


class Grant(BaseModel):
    resource_id: str
    securable_type: str
    securable_full_name: str
    grantee: str
    privilege: str
    grantor: str
    inherited_from: str


class GrantSummary(BaseModel):
    resource_id: str
    total_grants: int
    grants_by_privilege: dict[str, int]
    overprivileged_count: int
    direct_user_grant_count: int


class GrantFilters(BaseModel):
    resource_id: str | None = None
    grantee: str | None = None
    privilege: str | None = None
    securable_type: str | None = None


# ── Metastores ──────────────────────────────────────────────────────────────


class MetastoreInfo(BaseModel):
    metastore_id: str
    latest_scan: str | None = None
    resource_count: int = 0
    last_scanned: str | None = None


# ── Remediation ────────────────────────────────────────────────────────────


class RemediationFunnel(BaseModel):
    total_violations: int
    with_remediation: int
    pending_review: int
    approved: int
    applied: int
    verified: int
    verification_failed: int
    rejected: int


class AgentEffectiveness(BaseModel):
    agent_id: str
    agent_version: str
    total_proposals: int
    verified: int
    failed: int
    rejected: int
    precision_pct: float | None
    avg_confidence: float | None


class ReviewerLoad(BaseModel):
    reviewer: str
    pending_reviews: int
    total_approved: int
    total_rejected: int
    total_reassigned: int
    total_reviews: int


class ProposalSummary(BaseModel):
    proposal_id: str
    violation_id: str
    resource_id: str
    resource_name: str
    resource_type: str
    policy_id: str
    policy_name: str
    severity: Literal["critical", "high", "medium", "low"]
    domain: str
    agent_id: str
    agent_version: str
    status: str
    confidence: float
    proposed_sql: str
    created_at: str


class ReviewRecord(BaseModel):
    review_id: str
    proposal_id: str
    reviewer: str
    decision: str
    reasoning: str
    reassigned_to: str | None
    reviewed_at: str


class ProposalDetail(ProposalSummary):
    context_json: str
    citations: str
    pre_state: str
    proposed_state: str
    review_history: list[ReviewRecord]


class ProposalFilters(BaseModel):
    status: Literal[
        "pending_review", "approved", "applied",
        "verified", "rejected", ""
    ] = "pending_review"
    limit: int = Field(default=200, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class ReviewAction(BaseModel):
    decision: Literal["approved", "rejected", "reassigned"]
    reasoning: str = ""
    reassigned_to: str | None = None
