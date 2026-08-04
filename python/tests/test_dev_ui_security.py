"""Security tests for the dev UI: write-auth gate (H17), SSRF probe guard
(H18), and the fail-closed judge parser (M4)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from apx_agent._dev import (
    _dev_write_guard,
    _enforce_dev_write_auth,
    _enforce_discover_operator_auth,
    _parse_judge_output,
)
from apx_agent._ui_probe import _validate_probe_url, validate_wire_peer_url


def _req(method: str = "POST", path: str = "/_apx/edit", headers=None, query=None):
    r = MagicMock()
    r.method = method
    r.url.path = path
    r.headers = headers or {}
    r.query_params = query or {}
    return r


# --- H17: dev-UI write authorization --------------------------------------


def test_local_allows_writes(monkeypatch):
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)
    _enforce_dev_write_auth(_req())


def test_deployed_without_sso_denies_writes(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_dev_write_auth(_req())
    assert exc.value.status_code == 403
    assert "SSO" in exc.value.detail


def test_deployed_sso_allows_writes(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)
    _enforce_dev_write_auth(
        _req(headers={"x-forwarded-access-token": "obo-user-token"})
    )


def test_deployed_token_still_works_as_automation_override(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.setenv("APX_DEV_UI_TOKEN", "s3cret")
    with pytest.raises(HTTPException):
        _enforce_dev_write_auth(_req())
    with pytest.raises(HTTPException):
        _enforce_dev_write_auth(_req(headers={"x-apx-dev-token": "wrong"}))
    _enforce_dev_write_auth(_req(headers={"x-apx-dev-token": "s3cret"}))
    _enforce_dev_write_auth(_req(query={"token": "s3cret"}))
    # SSO wins even when a token is configured.
    _enforce_dev_write_auth(
        _req(headers={"x-forwarded-access-token": "obo-user-token"})
    )


# --- #611: Discover wire/unwire requires operator token on Apps ------------


def test_discover_operator_local_allows(monkeypatch):
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)
    _enforce_discover_operator_auth(_req(path="/_apx/discover/wire-agent"))


def test_discover_operator_deployed_sso_alone_denies(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.setenv("APX_DEV_UI_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as exc:
        _enforce_discover_operator_auth(
            _req(
                path="/_apx/discover/wire-agent",
                headers={"x-forwarded-access-token": "obo-user-token"},
            )
        )
    assert exc.value.status_code == 403
    assert "shared live agent" in exc.value.detail.lower() or "operator" in exc.value.detail.lower()


def test_discover_operator_deployed_requires_configured_token(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_discover_operator_auth(
            _req(
                path="/_apx/discover/wire-tool",
                headers={"x-forwarded-access-token": "obo-user-token"},
            )
        )
    assert exc.value.status_code == 403
    assert "APX_DEV_UI_TOKEN" in exc.value.detail


def test_discover_operator_matching_token_allows(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.setenv("APX_DEV_UI_TOKEN", "s3cret")
    _enforce_discover_operator_auth(
        _req(path="/_apx/discover/wire-agent", headers={"x-apx-dev-token": "s3cret"})
    )
    _enforce_discover_operator_auth(
        _req(path="/_apx/discover/unwire-tool", query={"token": "s3cret"})
    )


@pytest.mark.asyncio
async def test_guard_discover_mutation_needs_operator_even_with_sso(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.setenv("APX_DEV_UI_TOKEN", "s3cret")
    sso = {"x-forwarded-access-token": "obo-user-token"}

    with pytest.raises(HTTPException) as exc:
        await _dev_write_guard(
            _req(method="POST", path="/_apx/discover/wire-agent", headers=sso)
        )
    assert exc.value.status_code == 403

    # Ordinary writes still accept SSO alone.
    await _dev_write_guard(_req(method="POST", path="/_apx/edit", headers=sso))

    # Discover mutation with matching operator token succeeds.
    await _dev_write_guard(
        _req(
            method="POST",
            path="/_apx/discover/wire-agent",
            headers={**sso, "x-apx-dev-token": "s3cret"},
        )
    )


@pytest.mark.asyncio
async def test_guard_gates_writes_and_probe_not_reads(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)

    with pytest.raises(HTTPException):
        await _dev_write_guard(_req(method="POST", path="/_apx/tools/new"))
    with pytest.raises(HTTPException):
        await _dev_write_guard(_req(method="DELETE", path="/_apx/tools/foo"))
    with pytest.raises(HTTPException):
        await _dev_write_guard(_req(method="GET", path="/_apx/setup/probe-json"))
    with pytest.raises(HTTPException):
        await _dev_write_guard(_req(method="GET", path="/_apx/deploy/stream"))
    await _dev_write_guard(_req(method="GET", path="/_apx/probe/checks"))
    # Signed-in SSO unlocks writes.
    await _dev_write_guard(
        _req(
            method="POST",
            path="/_apx/tools/new",
            headers={"x-forwarded-access-token": "obo"},
        )
    )


@pytest.mark.asyncio
async def test_guard_gates_per_principal_data_reads_on_deployed_app(monkeypatch):
    """#468: on a deployed App per-principal data reads need SSO (or token)."""
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8080")
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)

    for path in (
        "/_apx/approvals",
        "/_apx/conversations",
        "/_apx/conversations/conv-1/items",
        "/_apx/memories",
        "/_apx/traces",
        "/_apx/traces/tr-1",
    ):
        with pytest.raises(HTTPException):
            await _dev_write_guard(_req(method="GET", path=path))
        await _dev_write_guard(
            _req(
                method="GET",
                path=path,
                headers={"x-forwarded-access-token": "obo"},
            )
        )

    for path in ("/_apx/chat", "/_apx/topology", "/_apx/probe/checks"):
        await _dev_write_guard(_req(method="GET", path=path))


@pytest.mark.asyncio
async def test_data_reads_open_locally(monkeypatch):
    """Locally (not a deployed App) the data reads stay open — dev convenience."""
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    monkeypatch.delenv("APX_DEV_UI_TOKEN", raising=False)
    for path in ("/_apx/approvals", "/_apx/conversations", "/_apx/memories", "/_apx/traces"):
        await _dev_write_guard(_req(method="GET", path=path))


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


# --- #610: Discover wire-agent Apps-host allowlist ------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://peer.aws.databricksapps.com/",  # https required
        "https://evil.example/",
        "https://databricksapps.com.evil.example/",
        "https://169.254.169.254/",
        "ftp://x.databricksapps.com/",
        "not a url",
    ],
)
def test_validate_wire_peer_url_rejects(url):
    assert validate_wire_peer_url(url) is not None


def test_validate_wire_peer_url_allows_apps_host(monkeypatch):
    monkeypatch.setattr("apx_agent._ui_probe._validate_probe_url", lambda _url: None)
    assert validate_wire_peer_url("https://peer.aws.databricksapps.com/") is None


def test_validate_wire_peer_url_rejects_dns_rebind(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **k):
        return [(None, None, None, None, ("169.254.169.254", 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    reason = validate_wire_peer_url("https://evil.aws.databricksapps.com/")
    assert reason is not None
    assert "blocked" in reason.lower() or "169.254" in reason


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
