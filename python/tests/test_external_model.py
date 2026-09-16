"""Gate tests for declared external-model endpoints (AC-1..AC-11)."""

from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest
from databricks.sdk.errors import NotFound

from apx_agent import cli as _cli
import apx_agent._external_model as _em

from apx_agent._external_model import (
    _PROVIDER_REGISTRY,
    ExternalModelSpec,
    build_endpoint_payload,
    parse_model_scheme,
    reconcile_external_model_endpoint,
)
from apx_agent._inspection import _load_agent_config
from apx_agent._models import GatewayConfig
from apx_agent._resources import ResourceSpec, resources_to_databricks_yml


# --- AC-1 ---
def test_parse_bedrock_scheme() -> None:
    spec = parse_model_scheme("bedrock:anthropic.claude-3-5-sonnet")
    assert spec is not None
    assert spec.provider == "amazon-bedrock"
    assert spec.model_name == "anthropic.claude-3-5-sonnet"


# --- AC-2 ---
def test_bare_model_returns_none() -> None:
    assert parse_model_scheme("databricks-claude-sonnet-4-6") is None


# --- AC-3 ---
def test_unknown_scheme_fails_clear() -> None:
    with pytest.raises(ValueError) as ei:
        parse_model_scheme("quantum:foo")
    msg = str(ei.value)
    assert "quantum" in msg
    # names the known schemes so the author can correct the typo
    assert "bedrock" in msg


# --- AC-4 ---
def test_provider_registry_is_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        _PROVIDER_REGISTRY,
        "newcloud",
        {"provider": "new-cloud", "config_key": "new_cloud_config"},
    )
    spec = parse_model_scheme("newcloud:some-model")
    assert spec is not None
    assert spec.provider == "new-cloud"
    assert spec.model_name == "some-model"


# --- AC-5 ---
def test_gateway_config_governance_on_defaults(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.apx.agent]\n"
        'name = "x"\n'
        'model = "bedrock:anthropic.claude-3-5-sonnet"\n'
        "[tool.apx.agent.gateway]\n"
        'credential = "my_cred"\n'
    )
    config = _load_agent_config(pyproject_path=pyproject)
    assert config is not None
    assert config.gateway is not None
    assert config.gateway.usage_tracking is True
    assert config.gateway.guardrails is True
    assert config.gateway.credential == "my_cred"


# --- AC-6 ---
def test_missing_credential_fails_closed() -> None:
    spec = parse_model_scheme("bedrock:anthropic.claude-3-5-sonnet")
    assert spec is not None
    with pytest.raises(ValueError) as ei:
        build_endpoint_payload(spec, GatewayConfig())
    assert "credential" in str(ei.value).lower()


# --- AC-7 ---
def test_payload_has_external_model_and_gateway() -> None:
    spec = parse_model_scheme("bedrock:anthropic.claude-3-5-sonnet")
    assert spec is not None
    payload = build_endpoint_payload(spec, GatewayConfig(credential="my_cred"))
    assert payload["name"] == spec.endpoint_name
    entity = payload["config"]["served_entities"][0]
    ext = entity["external_model"]
    assert ext["provider"] == "amazon-bedrock"
    assert ext["provider"] == ext["provider"].lower()
    assert ext["name"] == "anthropic.claude-3-5-sonnet"
    assert ext["amazon_bedrock_config"]["uc_service_credential_name"] == "my_cred"
    gw = payload["ai_gateway"]
    assert gw["usage_tracking_config"]["enabled"] is True
    assert "input" in gw["guardrails"] and "output" in gw["guardrails"]


def test_payload_secrets_fallback() -> None:
    spec = parse_model_scheme("bedrock:m")
    assert spec is not None
    payload = build_endpoint_payload(
        spec, GatewayConfig(secret_scope="s", secret_key="k")
    )
    cfg = payload["config"]["served_entities"][0]["external_model"]["amazon_bedrock_config"]
    assert cfg["secret_scope"] == "s"
    assert cfg["secret_key"] == "k"


def _spy_ws() -> MagicMock:
    ws = MagicMock()
    ws.api_client.do = MagicMock()
    return ws


# --- AC-8 ---
def test_reconcile_creates_when_absent() -> None:
    ws = _spy_ws()
    ws.serving_endpoints.get.side_effect = NotFound("nope")
    payload = build_endpoint_payload(
        parse_model_scheme("bedrock:m"), GatewayConfig(credential="c")  # type: ignore[arg-type]
    )
    reconcile_external_model_endpoint(ws, payload)
    posts = [c for c in ws.api_client.do.call_args_list if c.args[0] == "POST"]
    assert len(posts) == 1
    assert posts[0].args[1] == "/api/2.0/serving-endpoints"
    # never delete
    assert not any(c.args[0] == "DELETE" for c in ws.api_client.do.call_args_list)
    ws.serving_endpoints.delete.assert_not_called()


# --- AC-9 ---
def test_reconcile_updates_when_present() -> None:
    ws = _spy_ws()
    ws.serving_endpoints.get.return_value = object()  # exists
    payload = build_endpoint_payload(
        parse_model_scheme("bedrock:m"), GatewayConfig(credential="c")  # type: ignore[arg-type]
    )
    reconcile_external_model_endpoint(ws, payload)
    puts = [c for c in ws.api_client.do.call_args_list if c.args[0] == "PUT"]
    assert any("/config" in c.args[1] for c in puts)
    assert not any(c.args[0] in ("POST", "DELETE") for c in ws.api_client.do.call_args_list)
    ws.serving_endpoints.delete.assert_not_called()


# --- AC-10 ---
def test_reconcile_fails_closed_on_error() -> None:
    ws = _spy_ws()
    ws.serving_endpoints.get.side_effect = NotFound("nope")
    ws.api_client.do.side_effect = RuntimeError("credential unreachable")
    payload = build_endpoint_payload(
        parse_model_scheme("bedrock:m"), GatewayConfig(credential="c")  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="credential unreachable"):
        reconcile_external_model_endpoint(ws, payload)


# --- AC-11 ---
def test_can_query_resource_emitted() -> None:
    spec = parse_model_scheme("bedrock:anthropic.claude-3-5-sonnet")
    assert spec is not None
    yml = resources_to_databricks_yml(
        [ResourceSpec("serving_endpoint", spec.endpoint_name)]
    )
    entries = [e for e in yml if "serving_endpoint" in e]
    assert entries
    body = entries[0]["serving_endpoint"]
    assert body["permission"] == "CAN_QUERY"
    assert body["endpoint_name"] == spec.endpoint_name


# --- deploy wiring: _provision_external_model_endpoint is invoked on deploy ---
def test_provision_bare_model_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare model name never builds a client or reconciles anything."""
    ws_factory = MagicMock()
    reconcile = MagicMock()
    monkeypatch.setattr(_cli, "_make_ws_for_scaffold", ws_factory)
    monkeypatch.setattr(_em, "reconcile_external_model_endpoint", reconcile)
    _cli._provision_external_model_endpoint(
        model="databricks-claude-sonnet-4-6",
        gateway=None,
        profile=None,
        log=lambda _m: None,
    )
    ws_factory.assert_not_called()
    reconcile.assert_not_called()


def test_provision_bedrock_calls_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bedrock: model builds the payload + author client and reconciles once."""
    ws = MagicMock()
    reconcile = MagicMock()
    monkeypatch.setattr(_cli, "_make_ws_for_scaffold", lambda _p: ws)
    monkeypatch.setattr(_em, "reconcile_external_model_endpoint", reconcile)
    _cli._provision_external_model_endpoint(
        model="bedrock:anthropic.claude-3-5-sonnet",
        gateway=GatewayConfig(credential="my_cred"),
        profile=None,
        log=lambda _m: None,
    )
    reconcile.assert_called_once()
    called_ws, payload = reconcile.call_args[0]
    assert called_ws is ws
    entity = payload["config"]["served_entities"][0]["external_model"]
    assert entity["provider"] == "amazon-bedrock"


def test_provision_missing_credential_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential → ClickException before any client build or reconcile."""
    ws_factory = MagicMock()
    reconcile = MagicMock()
    monkeypatch.setattr(_cli, "_make_ws_for_scaffold", ws_factory)
    monkeypatch.setattr(_em, "reconcile_external_model_endpoint", reconcile)
    with pytest.raises(click.ClickException, match="credential"):
        _cli._provision_external_model_endpoint(
            model="bedrock:m",
            gateway=GatewayConfig(),
            profile=None,
            log=lambda _m: None,
        )
    ws_factory.assert_not_called()
    reconcile.assert_not_called()


def test_provision_reconcile_error_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing reconcile aborts the deploy fail-closed."""
    monkeypatch.setattr(_cli, "_make_ws_for_scaffold", lambda _p: MagicMock())
    monkeypatch.setattr(
        _em,
        "reconcile_external_model_endpoint",
        MagicMock(side_effect=RuntimeError("boom")),
    )
    with pytest.raises(click.ClickException, match="fail-closed"):
        _cli._provision_external_model_endpoint(
            model="bedrock:m",
            gateway=GatewayConfig(credential="c"),
            profile=None,
            log=lambda _m: None,
        )


def test_provision_no_author_client_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No author client → ClickException, no reconcile attempted."""
    reconcile = MagicMock()
    monkeypatch.setattr(_cli, "_make_ws_for_scaffold", lambda _p: None)
    monkeypatch.setattr(_em, "reconcile_external_model_endpoint", reconcile)
    with pytest.raises(click.ClickException, match="author client"):
        _cli._provision_external_model_endpoint(
            model="bedrock:m",
            gateway=GatewayConfig(credential="c"),
            profile=None,
            log=lambda _m: None,
        )
    reconcile.assert_not_called()
