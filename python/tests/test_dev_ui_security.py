"""Security tests for the dev UI: write-auth gate (H17), SSRF probe guard
(H18), and the fail-closed judge parser (M4)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from apx_agent._dev import (
    _dev_write_guard,
    _enforce_dev_write_auth,
    _parse_judge_output,
)
from apx_agent._ui_probe import _validate_probe_url


def _req(method: str = "POST", path: str = "/_apx/edit", headers=None, query=None):
    r = MagicMock()
    r.method = method
    r.url.path = path
    r.headers = headers or {}
    r.query_params = query or {}
    return r


# --- H17: dev-UI write authorization --------------------------------------


def test_local_no_token_allows_writes(monkeypatch):
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)
    # Should not raise.
    _enforce_dev_write_auth(_req())


def test_deployed_no_token_denies_writes(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_dev_write_auth(_req())
    assert exc.value.status_code == 403


def test_deployed_token_requires_matching_header(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.setenv("APX_DEV_UI_TOKEN", "s3cret")
    with pytest.raises(HTTPException):
        _enforce_dev_write_auth(_req())
    with pytest.raises(HTTPException):
        _enforce_dev_write_auth(_req(headers={"x-apx-dev-token": "wrong"}))
    # Correct header (or query param) is accepted — should not raise.
    _enforce_dev_write_auth(_req(headers={"x-apx-dev-token": "s3cret"}))
    _enforce_dev_write_auth(_req(query={"token": "s3cret"}))


@pytest.mark.asyncio
async def test_guard_gates_writes_and_probe_not_reads(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)

    # Write method → gated.
    with pytest.raises(HTTPException):
        await _dev_write_guard(_req(method="POST", path="/_apx/tools/new"))
    # DELETE → gated.
    with pytest.raises(HTTPException):
        await _dev_write_guard(_req(method="DELETE", path="/_apx/tools/foo"))
    # SSRF probe (GET) → gated by path.
    with pytest.raises(HTTPException):
        await _dev_write_guard(_req(method="GET", path="/_apx/setup/probe-json"))
    # Deploy stream (GET that spawns `apx-agent deploy`) → gated by path.
    with pytest.raises(HTTPException):
        await _dev_write_guard(_req(method="GET", path="/_apx/deploy/stream"))
    # Read diagnostic (H9) → NOT gated.
    await _dev_write_guard(_req(method="GET", path="/_apx/probe/checks"))


# --- H18: SSRF probe URL validation ---------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/",
        "http://localhost/",
        "http://2130706433/",  # decimal-encoded 127.0.0.1
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "not a url at all",
    ],
)
def test_validate_probe_url_rejects(url):
    assert _validate_probe_url(url) is not None


def test_validate_probe_url_allows_public():
    assert _validate_probe_url("https://example.com/health") is None


# --- M4: judge parser fails closed on unclear output -----------------------


def test_judge_no_verdict_stays_fail():
    assert _parse_judge_output("The answer is passable.")[0] == "FAIL"
    assert _parse_judge_output("This would pass review.")[0] == "FAIL"
    assert _parse_judge_output("COMPASSIONATE and kind.")[0] == "FAIL"


def test_judge_explicit_verdicts():
    assert _parse_judge_output("VERDICT: PASS\nREASON: ok")[0] == "PASS"
    assert _parse_judge_output("VERDICT: FAIL\nREASON: no")[0] == "FAIL"
    assert _parse_judge_output("")[0] == "FAIL"


# --- H9: trace-read probe detects blob-blocked span degradation ------------


@pytest.mark.asyncio
async def test_mlflow_read_detects_span_blob_failure(monkeypatch):
    """Metadata read succeeds, span read raises (blocked blob) → fail, not ok."""
    from unittest.mock import MagicMock

    import apx_agent._ui_probe as probe

    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-123")

    meta = MagicMock()
    meta.info.trace_id = "tr-1"

    def search_traces(*, experiment_ids, max_results, include_spans):
        if include_spans:
            raise RuntimeError("egress to abc.storage.cloud.databricks.com blocked")
        return [meta]

    client = MagicMock()
    client.search_traces.side_effect = search_traces

    fake_mod = MagicMock()
    fake_mod.MlflowClient.return_value = client
    monkeypatch.setitem(__import__("sys").modules, "mlflow.tracking", fake_mod)

    result = await probe._check_mlflow_read()
    assert result["status"] == "fail"
    assert "span" in result["message"].lower()


@pytest.mark.asyncio
async def test_mlflow_read_no_traces_skips(monkeypatch):
    """No traces recorded → skip (absence is not failure)."""
    from unittest.mock import MagicMock

    import apx_agent._ui_probe as probe

    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-123")
    client = MagicMock()
    client.search_traces.return_value = []
    fake_mod = MagicMock()
    fake_mod.MlflowClient.return_value = client
    monkeypatch.setitem(__import__("sys").modules, "mlflow.tracking", fake_mod)

    result = await probe._check_mlflow_read()
    assert result["status"] == "skip"
