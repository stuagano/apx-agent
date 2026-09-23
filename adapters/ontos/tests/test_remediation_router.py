# adapters/ontos/tests/test_remediation_router.py
"""Unit tests for remediation review API — provider methods + router.

Run with: pytest adapters/ontos/tests/test_remediation_router.py -v
"""
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# Ensure the adapter package is importable
_ADAPTER_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT / "src"))

# Stub out databricks.sql before any ontos_governance imports trigger it
if "databricks.sql" not in sys.modules:
    _db_sql = ModuleType("databricks.sql")
    _db_sql.connect = MagicMock()
    sys.modules["databricks.sql"] = _db_sql
    if "databricks" in sys.modules:
        sys.modules["databricks"].sql = _db_sql

# Clear any previously cached ontos_governance modules so the stub takes effect
for _mod_name in list(sys.modules.keys()):
    if _mod_name.startswith("ontos_governance"):
        del sys.modules[_mod_name]

from fastapi import FastAPI
from fastapi.testclient import TestClient
from ontos_governance.routers._deps import get_provider
from ontos_governance.routers.remediation import router

# ── Fixtures ────────────────────────────────────────────────────────────────

def _make_mock_provider():
    """Create a mock provider with canned remediation data."""
    provider = MagicMock()

    provider.remediation_funnel.return_value = {
        "total_violations": 247,
        "with_remediation": 84,
        "pending_review": 12,
        "approved": 8,
        "applied": 56,
        "verified": 48,
        "verification_failed": 3,
        "rejected": 5,
    }

    provider.agent_effectiveness.return_value = [
        {
            "agent_id": "steward-agent",
            "agent_version": "1.0.0",
            "total_proposals": 84,
            "verified": 48,
            "failed": 3,
            "rejected": 5,
            "precision_pct": 94.1,
            "avg_confidence": 0.87,
        }
    ]

    provider.reviewer_load.return_value = [
        {
            "reviewer": "alice@co.com",
            "pending_reviews": 7,
            "total_approved": 20,
            "total_rejected": 3,
            "total_reassigned": 1,
            "total_reviews": 24,
        }
    ]

    provider.list_proposals.return_value = [
        {
            "proposal_id": "prop-001",
            "violation_id": "v-001",
            "resource_id": "catalog.schema.table_a",
            "resource_name": "table_a",
            "resource_type": "table",
            "policy_id": "POL-SEC-003",
            "policy_name": "PII tables must have a data steward",
            "severity": "critical",
            "domain": "Security",
            "agent_id": "steward-agent",
            "agent_version": "1.0.0",
            "status": "pending_review",
            "confidence": 0.9,
            "proposed_sql": "ALTER TABLE catalog.schema.table_a SET TAGS ('data_steward' = 'jane')",
            "created_at": "2026-04-16T10:00:00Z",
        }
    ]

    provider.get_proposal.return_value = {
        "proposal_id": "prop-001",
        "violation_id": "v-001",
        "resource_id": "catalog.schema.table_a",
        "resource_name": "table_a",
        "resource_type": "table",
        "policy_id": "POL-SEC-003",
        "policy_name": "PII tables must have a data steward",
        "severity": "critical",
        "domain": "Security",
        "agent_id": "steward-agent",
        "agent_version": "1.0.0",
        "status": "pending_review",
        "confidence": 0.9,
        "proposed_sql": "ALTER TABLE catalog.schema.table_a SET TAGS ('data_steward' = 'jane')",
        "created_at": "2026-04-16T10:00:00Z",
        "context_json": '{"owner": "jane", "reason": "table owner is jane"}',
        "citations": "",
        "pre_state": '{"data_steward": null}',
        "proposed_state": '{"data_steward": "jane"}',
        "review_history": [],
    }

    provider.submit_review.return_value = {
        "review_id": "rev-001",
        "proposal_id": "prop-001",
        "decision": "approved",
        "status": "approved",
    }

    return provider


# ── Provider method tests ───────────────────────────────────────────────────


class TestRemediationFunnel:
    def test_funnel_returns_all_stages(self):
        provider = _make_mock_provider()
        result = provider.remediation_funnel()
        assert result["total_violations"] == 247
        assert result["pending_review"] == 12
        assert result["verified"] == 48


class TestAgentEffectiveness:
    def test_returns_agent_list(self):
        provider = _make_mock_provider()
        result = provider.agent_effectiveness()
        assert len(result) == 1
        assert result[0]["agent_id"] == "steward-agent"
        assert result[0]["precision_pct"] == 94.1


class TestListProposals:
    def test_returns_enriched_proposals(self):
        provider = _make_mock_provider()
        result = provider.list_proposals(MagicMock())
        assert len(result) == 1
        assert result[0]["severity"] == "critical"
        assert result[0]["policy_name"] == "PII tables must have a data steward"

    def test_proposal_has_required_fields(self):
        provider = _make_mock_provider()
        result = provider.list_proposals(MagicMock())
        p = result[0]
        required = [
            "proposal_id", "violation_id", "resource_id", "resource_name",
            "policy_id", "policy_name", "severity", "agent_id", "status",
            "confidence", "proposed_sql", "created_at",
        ]
        for field in required:
            assert field in p, f"Missing field: {field}"


class TestGetProposal:
    def test_detail_includes_context_and_history(self):
        provider = _make_mock_provider()
        result = provider.get_proposal("prop-001")
        assert "context_json" in result
        assert "pre_state" in result
        assert "review_history" in result


class TestSubmitReview:
    def test_approve_returns_review_record(self):
        provider = _make_mock_provider()
        result = provider.submit_review(
            "prop-001", "approved", "looks good", reviewer="alice@co.com"
        )
        assert result["decision"] == "approved"

    def test_submit_review_called_with_correct_args(self):
        provider = _make_mock_provider()
        provider.submit_review("prop-001", "rejected", "bad SQL", reviewer="bob@co.com")
        provider.submit_review.assert_called_once_with(
            "prop-001", "rejected", "bad SQL", reviewer="bob@co.com"
        )


# ── TestClient integration tests ────────────────────────────────────────────


def _make_test_client(provider=None):
    """Create a FastAPI TestClient with the remediation router and mock provider."""
    if provider is None:
        provider = _make_mock_provider()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_provider] = lambda: provider
    return TestClient(app)


class TestRemediationRouter:
    """Integration tests — exercise actual HTTP endpoints with mock provider."""

    # -- Funnel --

    def test_funnel_endpoint_returns_200(self):
        client = _make_test_client()
        resp = client.get("/remediation/funnel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_violations"] == 247
        assert data["pending_review"] == 12
        assert data["verified"] == 48

    # -- Agent effectiveness --

    def test_agents_endpoint_returns_200(self):
        client = _make_test_client()
        resp = client.get("/remediation/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["agent_id"] == "steward-agent"

    # -- Reviewer load --

    def test_reviewer_load_endpoint_returns_200(self):
        client = _make_test_client()
        resp = client.get("/remediation/reviewer-load")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data[0]["reviewer"] == "alice@co.com"

    # -- Proposals list --

    def test_proposals_default_status_filter(self):
        provider = _make_mock_provider()
        client = _make_test_client(provider)
        resp = client.get("/remediation/proposals")
        assert resp.status_code == 200
        call_args = provider.list_proposals.call_args
        filters = call_args[0][0]
        assert filters.status == "pending_review"

    def test_proposals_custom_status_filter(self):
        provider = _make_mock_provider()
        client = _make_test_client(provider)
        resp = client.get("/remediation/proposals?status=approved")
        assert resp.status_code == 200
        call_args = provider.list_proposals.call_args
        filters = call_args[0][0]
        assert filters.status == "approved"

    def test_proposals_passes_limit_and_offset(self):
        provider = _make_mock_provider()
        client = _make_test_client(provider)
        resp = client.get("/remediation/proposals?limit=50&offset=10")
        assert resp.status_code == 200
        call_args = provider.list_proposals.call_args
        filters = call_args[0][0]
        assert filters.limit == 50
        assert filters.offset == 10

    # -- Proposal detail --

    def test_proposal_detail_returns_200(self):
        client = _make_test_client()
        resp = client.get("/remediation/proposals/prop-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["proposal_id"] == "prop-001"
        assert "context_json" in data
        assert "review_history" in data

    def test_proposal_detail_404_when_not_found(self):
        provider = _make_mock_provider()
        provider.get_proposal.side_effect = LookupError("not found")
        client = _make_test_client(provider)
        resp = client.get("/remediation/proposals/nonexistent")
        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    # -- Review action --

    def test_review_approve_returns_200(self):
        client = _make_test_client()
        resp = client.post(
            "/remediation/proposals/prop-001/review",
            json={"decision": "approved", "reasoning": "looks good"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "approved"

    def test_review_reject_returns_200(self):
        provider = _make_mock_provider()
        provider.submit_review.return_value = {
            "review_id": "rev-002",
            "proposal_id": "prop-001",
            "decision": "rejected",
            "status": "rejected",
        }
        client = _make_test_client(provider)
        resp = client.post(
            "/remediation/proposals/prop-001/review",
            json={"decision": "rejected", "reasoning": "bad SQL"},
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "rejected"

    def test_review_reassign_requires_reassigned_to(self):
        client = _make_test_client()
        resp = client.post(
            "/remediation/proposals/prop-001/review",
            json={"decision": "reassigned"},
        )
        assert resp.status_code == 422

    def test_review_reassign_with_reassigned_to_returns_200(self):
        provider = _make_mock_provider()
        provider.submit_review.return_value = {
            "review_id": "rev-003",
            "proposal_id": "prop-001",
            "decision": "reassigned",
            "status": "pending_review",
        }
        client = _make_test_client(provider)
        resp = client.post(
            "/remediation/proposals/prop-001/review",
            json={"decision": "reassigned", "reassigned_to": "bob@co.com"},
        )
        assert resp.status_code == 200

    def test_review_404_when_proposal_not_found(self):
        provider = _make_mock_provider()
        provider.submit_review.side_effect = LookupError("not found")
        client = _make_test_client(provider)
        resp = client.post(
            "/remediation/proposals/nonexistent/review",
            json={"decision": "approved", "reasoning": "looks good"},
        )
        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    def test_review_409_on_invalid_status_transition(self):
        provider = _make_mock_provider()
        provider.submit_review.side_effect = ValueError("Proposal is already approved")
        client = _make_test_client(provider)
        resp = client.post(
            "/remediation/proposals/prop-001/review",
            json={"decision": "approved", "reasoning": "duplicate approval"},
        )
        assert resp.status_code == 409
        assert "already approved" in resp.json()["detail"]

    def test_review_passes_user_header_to_provider(self):
        provider = _make_mock_provider()
        client = _make_test_client(provider)
        client.post(
            "/remediation/proposals/prop-001/review",
            json={"decision": "approved", "reasoning": "lgtm"},
            headers={"x-forwarded-user": "alice@co.com"},
        )
        call_kwargs = provider.submit_review.call_args[1]
        assert call_kwargs["reviewer"] == "alice@co.com"
