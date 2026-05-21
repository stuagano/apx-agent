import pytest
from unittest.mock import MagicMock
from tests.conftest import _col


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("AFR_DECISION_TABLE", "catalog.schema.afr_processing")


SAMPLE_CANDIDATES = [
    {"account_id": "acct-001", "name": "Jane Smith", "address": "123 Main St", "account_number": "12345", "score": 0.92},
    {"account_id": "acct-002", "name": "Janet Smyth", "address": "123 Main Street", "account_number": "12345", "score": 0.87},
]

SAMPLE_APPLICATION = {
    "name": "Jane Smith",
    "address": "123 Main St",
    "account_number": "12345",
}


def test_evaluate_candidates_high_confidence(mock_ws):
    from sub_agents.evaluator.agent import evaluate_candidates
    result = evaluate_candidates(applicant=SAMPLE_APPLICATION, candidates=SAMPLE_CANDIDATES, ws=mock_ws)
    assert "decision" in result
    assert result["decision"]["category"] in ("EXACT", "HIGH_CONFIDENCE", "LOW_CONFIDENCE", "NO_MATCH")
    assert 0.0 <= result["decision"]["confidence"] <= 1.0
    assert "rationale" in result["decision"]


def test_evaluate_candidates_no_candidates(mock_ws):
    from sub_agents.evaluator.agent import evaluate_candidates
    result = evaluate_candidates(applicant=SAMPLE_APPLICATION, candidates=[], ws=mock_ws)
    assert result["decision"]["category"] == "NO_MATCH"
    assert result["decision"]["matched"] is False


def test_evaluate_candidates_familial_flag(mock_ws):
    from sub_agents.evaluator.agent import evaluate_candidates
    # Same address, DIFFERENT surname — classic familial case (spouse or parent)
    candidates = [
        {"account_id": "acct-003", "name": "John Williams", "address": "123 Main St", "account_number": "99999", "score": 0.75},
    ]
    result = evaluate_candidates(
        applicant={"name": "Jane Smith", "address": "123 Main St", "account_number": "12345"},
        candidates=candidates,
        ws=mock_ws,
    )
    assert "familial" in result["decision"]["rationale"].lower()


def test_evaluate_candidates_account_number_boosts_score(mock_ws):
    from sub_agents.evaluator.agent import evaluate_candidates
    candidates = [
        {"account_id": "acct-004", "name": "Jane Smith", "address": "123 Main St", "account_number": "12345", "score": 0.88},
    ]
    result = evaluate_candidates(
        applicant={"name": "Jane Smith", "address": "123 Main St", "account_number": "12345"},
        candidates=candidates,
        ws=mock_ws,
    )
    assert "account number exact match" in result["decision"]["rationale"].lower()
    assert result["decision"]["confidence"] > 0.88


def test_log_decision_writes_sql(mock_ws):
    from sub_agents.evaluator.agent import log_decision
    from databricks.sdk.service.sql import StatementStatus, StatementState
    sql_result = MagicMock()
    sql_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    sql_result.manifest.schema.columns = [_col("account_id")]
    sql_result.result.data_array = []
    mock_ws.statement_execution.execute_statement.return_value = sql_result

    decision = {
        "applicant_name": "Jane Smith",
        "matched": True,
        "account_id": "acct-001",
        "category": "HIGH_CONFIDENCE",
        "rationale": "Name and address match.",
        "confidence": 0.92,
        "candidates_reviewed": 2,
    }
    result = log_decision(decision=decision, ws=mock_ws)
    assert result["status"] == "logged"
    mock_ws.statement_execution.execute_statement.assert_called_once()
    call_sql = mock_ws.statement_execution.execute_statement.call_args[1]["statement"]
    assert "INSERT" in call_sql.upper()
    assert "afr_processing" in call_sql
