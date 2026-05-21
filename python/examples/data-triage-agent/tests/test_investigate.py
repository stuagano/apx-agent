import json
import pytest
import respx
import httpx
from unittest.mock import MagicMock

from jobs.investigate import (
    _extract_investigation_query,
    _format_comment,
    run_investigation,
)


def test_extract_query_all_fields():
    query = _extract_investigation_query(
        summary="Missing billing data for account 1234",
        description="We expected 500 rows but got 0.",
        priority="High",
        reporter="Alice",
    )
    assert "Missing billing data" in query
    assert "We expected 500" in query
    assert "High" in query
    assert "Alice" in query


def test_extract_query_minimal():
    query = _extract_investigation_query(
        summary="Missing data",
        description="",
        priority="",
        reporter="",
    )
    assert "Missing data" in query
    assert query.strip()


def test_format_comment_includes_key_sections():
    result = "## What is missing\nRows gone.\n## Why\nJob failed."
    comment = _format_comment("DATA-42", result)
    assert "DATA-42" in comment
    assert "What is missing" in comment
    assert "Job failed" in comment


@respx.mock
def test_run_investigation_returns_string():
    respx.post(
        "https://adb-test.azuredatabricks.net/serving-endpoints/databricks-claude-sonnet-4-6/invocations"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Investigation complete."},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    )
    mock_ws = MagicMock()
    mock_ws.config.host = "https://adb-test.azuredatabricks.net"
    mock_ws.config.token = "test-token"

    result = run_investigation("Why is data missing from billing table?", mock_ws)
    assert isinstance(result, str)
    assert len(result) > 0
