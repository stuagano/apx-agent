"""Typed findings for expected investigation-stage failures."""

from unittest.mock import MagicMock

from tools import list_genie_spaces, query_genie_space


def test_genie_space_query_failure_is_typed_unavailable() -> None:
    ws = MagicMock()
    ws.genie.start_conversation_and_wait.side_effect = RuntimeError(
        "failed to reach COMPLETED: MessageStatus.FAILED"
    )

    result = query_genie_space(
        space_id="space-1",
        question="Why is the table stale?",
        ws=ws,
    )

    assert result["status"] == "failed"
    assert result["availability"] == "unavailable"
    assert result["capability"] == "genie"
    assert "failed to reach COMPLETED" in result["error"]


def test_genie_space_discovery_failure_is_typed_unavailable() -> None:
    ws = MagicMock()
    ws.genie.list_spaces.side_effect = RuntimeError("Genie API unavailable")

    result = list_genie_spaces(ws)

    assert result["count"] == 0
    assert result["spaces"] == []
    assert result["availability"] == "unavailable"
    assert result["capability"] == "genie"
    assert "Genie API unavailable" in result["error"]
