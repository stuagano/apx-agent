"""Tests for Apps tool identity inference."""

from __future__ import annotations

from typing import Any

import pytest

from apx_agent import Dependencies, ResourceSpec, require_user_api_scopes, tool
from apx_agent._apps_authorization import infer_operation_authorization
from apx_agent._defaults import _get_workspace_client
from apx_agent._resources import attach_resources


def test_client_dependency_uses_service_identity() -> None:
    def lookup(ws: Dependencies.Client) -> str:
        return "ok"

    authorization = infer_operation_authorization(lookup)

    assert authorization.execution_identity == "service"
    assert authorization.requires_request_context is False


def test_inference_uses_inspection_dependency_callables(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup() -> str:
        return "ok"

    monkeypatch.setattr(
        "apx_agent._apps_authorization._tool_dependency_callables",
        lambda _fn: {"ws": _get_workspace_client},
    )

    assert infer_operation_authorization(lookup).execution_identity == "service"


@pytest.mark.parametrize("dependency", [Dependencies.UserClient, Dependencies.Workspace, Dependencies.Sql])
def test_user_client_dependencies_use_user_identity(dependency: Any) -> None:
    def lookup(client: str) -> str:
        return "ok"

    lookup.__annotations__["client"] = dependency

    authorization = infer_operation_authorization(lookup)

    assert authorization.execution_identity == "user"
    assert authorization.requires_request_context is True


@pytest.mark.parametrize("dependency", [Dependencies.Headers, Dependencies.Principal, Dependencies.Request])
def test_request_context_dependencies_do_not_select_identity(dependency: Any) -> None:
    def lookup(context: str) -> str:
        return "ok"

    lookup.__annotations__["context"] = dependency

    authorization = infer_operation_authorization(lookup)

    assert authorization.execution_identity == "service"
    assert authorization.requires_request_context is True


@pytest.mark.parametrize("dependency", [Dependencies.Progress, Dependencies.State])
def test_progress_and_state_do_not_select_identity(dependency: Any) -> None:
    def lookup(context: str) -> str:
        return "ok"

    lookup.__annotations__["context"] = dependency

    authorization = infer_operation_authorization(lookup)

    assert authorization.execution_identity == "service"
    assert authorization.requires_request_context is False


def test_matching_explicit_identity_succeeds() -> None:
    @tool(execution="service")
    def lookup(ws: Dependencies.Client) -> str:
        return "ok"

    assert infer_operation_authorization(lookup).execution_identity == "service"


def test_conflicting_explicit_identity_fails_with_tool_and_values() -> None:
    @tool(execution="user")
    def lookup(ws: Dependencies.Client) -> str:
        return "ok"

    with pytest.raises(ValueError, match="lookup.*user.*service"):
        infer_operation_authorization(lookup)


def test_mixed_client_dependencies_fail() -> None:
    def lookup(
        service_ws: Dependencies.Client,
        user_ws: Dependencies.UserClient,
    ) -> str:
        return "ok"

    with pytest.raises(ValueError, match="lookup.*user.*service"):
        infer_operation_authorization(lookup)


@pytest.mark.parametrize("declared", ["resource", "scope"])
def test_resource_or_scope_without_credential_dependency_defaults_to_user(declared: str) -> None:
    def lookup() -> str:
        return "ok"

    if declared == "resource":
        attach_resources(lookup, [ResourceSpec("uc_table", "main.sales.orders")])
    else:
        require_user_api_scopes(lookup, ["catalog.tables:read"])

    authorization = infer_operation_authorization(lookup)

    assert authorization.execution_identity == "user"
    assert authorization.requires_request_context is True


def test_pure_tool_uses_service_without_request_context() -> None:
    def lookup() -> str:
        return "ok"

    authorization = infer_operation_authorization(lookup)

    assert authorization.name == "lookup"
    assert authorization.execution_identity == "service"
    assert authorization.requires_request_context is False
    assert authorization.resources == ()
    assert authorization.user_api_scopes == ()
