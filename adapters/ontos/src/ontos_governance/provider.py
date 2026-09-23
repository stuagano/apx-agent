"""GovernanceProvider — the contract between Ontos and any governance backend.

This protocol defines what a governance data provider must expose. The
default implementation (GovernanceProvider) reads from Delta tables written
by the Governance Monitoring scanner. Alternative backends can implement this protocol
for different storage or data sources.

Usage in Ontos:

    from ontos_governance import register_routes
    from ontos_governance.providers import GovernanceProvider

    provider = GovernanceProvider.from_env()
    register_routes(app, provider=provider)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ontos_governance.models import (
    ExceptionFilters,
    ExceptionRecord,
    ExceptionSummary,
    Grant,
    GrantSummary,
    OntologyClass,
    OntologyTree,
    Policy,
    PolicyBase,
    PolicyFilters,
    PolicyVersion,
    ProposalFilters,
    Resource,
    ResourceDetail,
    ResourceFilters,
    ScanDetail,
    ScanRun,
    ValidationResult,
    Violation,
    ViolationFilters,
    ViolationSummary,
)


@runtime_checkable
class GovernanceProvider(Protocol):
    """Contract for governance data backends.

    Implementations must provide all methods below. Methods are grouped
    by the governance domain they serve.
    """

    # ── Metastores ──────────────────────────────────────────────────────────

    def list_metastores(self) -> list[dict]:
        """List all metastores with latest scan timestamp and resource count."""
        ...

    def set_active_metastore(self, metastore_id: str | None) -> None:
        """Set the active metastore filter. None clears the filter."""
        ...

    # ── Violations (read-only) ────────────────────────────────────────────

    def violations_summary(self, *, metastore_id: str | None = None) -> ViolationSummary:
        """Counts by severity and active status."""
        ...

    def list_violations(self, filters: ViolationFilters, *, metastore_id: str | None = None) -> list[Violation]:
        """List violations with optional filters, ordered by severity."""
        ...

    # ── Scans (read-only) ─────────────────────────────────────────────────

    def list_scans(self, limit: int = 50) -> list[ScanRun]:
        """Recent scan runs with timing and result counts."""
        ...

    def get_scan(self, scan_id: str) -> ScanDetail:
        """Single scan detail with per-policy breakdown."""
        ...

    # ── Resources (read-only) ─────────────────────────────────────────────

    def list_resources(self, filters: ResourceFilters, *, metastore_id: str | None = None) -> list[Resource]:
        """List resources from inventory, defaulting to latest scan."""
        ...

    def get_resource(self, resource_id: str) -> ResourceDetail:
        """Full resource detail: inventory + classifications + violations + exceptions
        + quality metrics (dq_status, when present).
        """
        ...

    # ── Policies (CRUD) ───────────────────────────────────────────────────

    def list_policies(self, filters: PolicyFilters) -> list[Policy]:
        """List all policies, optionally filtered by origin or active status."""
        ...

    def get_policy(self, policy_id: str) -> Policy:
        """Get a single policy by ID."""
        ...

    def create_policy(
        self,
        body: PolicyBase,
        *,
        policy_id: str | None = None,
        author: str = "unknown",
    ) -> Policy:
        """Create a user-origin policy. YAML policies cannot be created here."""
        ...

    def update_policy(
        self,
        policy_id: str,
        body: PolicyBase,
        *,
        author: str = "unknown",
    ) -> Policy:
        """Update a user-origin policy. YAML policies return 403."""
        ...

    def policy_history(self, policy_id: str) -> list[PolicyVersion]:
        """Full audit trail for a policy — all versions."""
        ...

    def list_applies_to_classes(self) -> list[str]:
        """Distinct applies_to values from the policies table (for UI dropdowns)."""
        ...

    # ── Exceptions (CRUD) ─────────────────────────────────────────────────

    def list_exceptions(self, filters: ExceptionFilters) -> list[ExceptionRecord]:
        """List exceptions with optional filters."""
        ...

    def exceptions_summary(self) -> ExceptionSummary:
        """Counts by status — for dashboard badges."""
        ...

    def exceptions_for_resource(self, resource_id: str) -> list[ExceptionRecord]:
        """All exceptions (active + inactive) for a specific resource."""
        ...

    def approve_exceptions(
        self,
        resource_id: str,
        policy_ids: list[str],
        justification: str,
        expires_days: int | None,
        *,
        approved_by: str = "unknown",
    ) -> dict:
        """Approve one or more policy exceptions for a resource."""
        ...

    def revoke_exception(self, exception_id: str, *, revoked_by: str = "unknown") -> dict:
        """Revoke an active exception."""
        ...

    def bulk_revoke_expired(self, *, revoked_by: str = "unknown") -> dict:
        """Revoke all exceptions past their expiry date."""
        ...

    # ── Grants (read-only) ───────────────────────────────────────────────

    def list_grants(
        self,
        resource_id: str | None = None,
        grantee: str | None = None,
    ) -> list[Grant]:
        """List grants, optionally filtered by resource or grantee."""
        ...

    def grant_summary(self, resource_id: str) -> GrantSummary:
        """Summary of grants for a specific resource: total, by privilege type, overprivileged count."""
        ...

    # ── Ontology (read-only) ──────────────────────────────────────────────

    def list_ontology_classes(self, *, kind: str | None = None) -> list[OntologyClass]:
        """List all resource classes, optionally filtered by kind."""
        ...

    def get_ontology_class(self, class_name: str) -> OntologyClass:
        """Get a single class with ancestry chain and direct children."""
        ...

    def ontology_tree(self) -> OntologyTree:
        """Full class hierarchy as a nested tree for UI rendering."""
        ...

    def validate_ontology(self) -> ValidationResult:
        """Validate the deployed ontology YAML. Returns errors and warnings."""
        ...

    # ── Remediation (read + review) ──────────────────────────────────────

    def remediation_funnel(self) -> dict:
        """Funnel counts: violations → proposed → pending → applied → verified."""
        ...

    def agent_effectiveness(self) -> list[dict]:
        """Per-agent scorecard: proposals, verified, rejected, precision."""
        ...

    def reviewer_load(self) -> list[dict]:
        """Per-reviewer workload: pending, approved, rejected counts."""
        ...

    def list_proposals(self, filters: "ProposalFilters") -> list[dict]:
        """List proposals with enriched violation/policy data."""
        ...

    def get_proposal(self, proposal_id: str) -> dict:
        """Single proposal with full context and review history."""
        ...

    def submit_review(
        self,
        proposal_id: str,
        decision: str,
        reasoning: str,
        *,
        reassigned_to: str | None = None,
        reviewer: str = "unknown",
    ) -> dict:
        """Submit a review decision (approve/reject/reassign)."""
        ...
