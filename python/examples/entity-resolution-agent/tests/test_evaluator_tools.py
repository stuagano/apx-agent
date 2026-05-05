import pytest


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("DECISION_TABLE", "catalog.schema.match_decisions")


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
    from entity_resolution_agent.backend.core.evaluator import evaluate_candidates
    result = evaluate_candidates(
        applicant=SAMPLE_APPLICATION,
        candidates=SAMPLE_CANDIDATES,
        ws=mock_ws,
    )
    assert "decision" in result
    assert result["decision"]["category"] == "EXACT"
    assert result["decision"]["matched"] is True
    assert result["decision"]["confidence"] >= 0.90


def test_evaluate_candidates_no_candidates(mock_ws):
    from entity_resolution_agent.backend.core.evaluator import evaluate_candidates
    result = evaluate_candidates(
        applicant=SAMPLE_APPLICATION,
        candidates=[],
        ws=mock_ws,
    )
    assert result["decision"]["category"] == "NO_MATCH"
    assert result["decision"]["matched"] is False


def test_evaluate_candidates_familial_flag(mock_ws):
    from entity_resolution_agent.backend.core.evaluator import evaluate_candidates
    candidates = [
        {"account_id": "acct-003", "name": "John Smith", "address": "123 Main St", "account_number": "12345", "score": 0.85},
    ]
    result = evaluate_candidates(
        applicant={"name": "Jane Smith", "address": "123 Main St", "account_number": "12345"},
        candidates=candidates,
        ws=mock_ws,
    )
    # Same address, same surname, different first name — should flag familial
    assert "familial" in result["decision"]["rationale"].lower()


def test_log_decision_writes_sql(mock_ws):
    from entity_resolution_agent.backend.core.evaluator import log_decision
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
    assert "match_decisions" in call_sql
