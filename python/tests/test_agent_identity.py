"""Agent identity (#816) — the agent principal, declared once, composed with OBO.

An agent declares its own identity once in ``[tool.apx.agent]`` and tools that
declare none inherit it. A tool's own ``identity`` always wins. The inherited
identity rides the same machinery per-tool ``named-sp`` already uses, so deploy
authorization, the authorization summary, and the tool-call span all name the
principal the call actually runs as.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apx_agent._agent_identity import apply_agent_identity
from apx_agent._agents import LlmAgent
from apx_agent._apps_authorization import (
    AppFamilyPermissions,
    AuthorizationPlan,
    OperationAuthorization,
    authorization_summary_lines,
    infer_operation_authorization,
)
from apx_agent._audit import AuditAttrs
from apx_agent._defaults import _get_workspace_client
from apx_agent._models import AgentConfig
from apx_agent._resources import ResourceSpec
from apx_agent._tool import get_tool_metadata
from apx_agent._tool_factory import build_tool
from apx_agent._tool_scope import ToolScope, attach_scope


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def _tool(name: str, identity: str | None = None):
    def call(query: str) -> str:
        return query

    call.__name__ = name
    tool = build_tool(call, name=name, description=f"test tool {name}")
    if identity is not None:
        attach_scope(tool, ToolScope(identity=identity))
    return tool


def test_agent_config_declares_an_identity() -> None:
    config = AgentConfig(name="triage", identity="named-sp:reader")
    assert config.identity == "named-sp:reader"


def test_agent_config_rejects_a_malformed_identity() -> None:
    with pytest.raises(ValueError, match="named-sp"):
        AgentConfig(name="triage", identity="root")


def test_agent_identity_fills_only_tools_that_declare_none() -> None:
    plain = _tool("plain")
    explicit = _tool("explicit", identity="obo")

    agent = SimpleNamespace(_tool_fns=[plain, explicit])
    apply_agent_identity(agent, "named-sp:reader")

    plain_md = get_tool_metadata(plain)
    explicit_md = get_tool_metadata(explicit)
    assert plain_md is not None and plain_md.execution == "service"
    assert plain_md.service_principal_name == "reader"
    # A per-tool declaration is the tool's own identity and is never overwritten.
    assert explicit_md is not None and explicit_md.execution == "user"
    assert explicit_md.service_principal_name is None


def test_app_service_principal_is_not_named() -> None:
    """``identity = "sp"`` means the platform's app service principal."""
    tool = _tool("plain")
    apply_agent_identity(SimpleNamespace(_tool_fns=[tool]), "sp")

    md = get_tool_metadata(tool)
    assert md is not None and md.execution == "service"
    assert md.service_principal_name is None


def test_user_identity_is_on_behalf_of() -> None:
    tool = _tool("plain")
    apply_agent_identity(SimpleNamespace(_tool_fns=[tool]), "obo")

    assert infer_operation_authorization(tool).execution_identity == "user"


def test_named_principal_is_the_app_service_principal(monkeypatch) -> None:
    """Databricks Apps injects one service principal. A name is only honored
    when that name is the App's own service principal."""
    tool = _tool("plain")
    apply_agent_identity(SimpleNamespace(_tool_fns=[tool]), "named-sp:reader")
    monkeypatch.setattr(
        "apx_agent._apps_authorization._tool_dependency_callables",
        lambda _fn: {"ws": _get_workspace_client},
    )

    authorization = infer_operation_authorization(tool)
    assert authorization.execution_identity == "service"
    assert authorization.principal == "reader"


def test_inherited_identity_conflicts_with_dependencies(monkeypatch) -> None:
    tool = _tool("plain")
    apply_agent_identity(SimpleNamespace(_tool_fns=[tool]), "obo")
    monkeypatch.setattr(
        "apx_agent._apps_authorization._tool_dependency_callables",
        lambda _fn: {"ws": _get_workspace_client},
    )

    with pytest.raises(ValueError, match="plain"):
        infer_operation_authorization(tool)


def test_authorization_summary_names_the_agent_principal() -> None:
    plan = AuthorizationPlan(
        operations=(
            OperationAuthorization(
                name="read_ledger",
                execution_identity="service",
                requires_request_context=False,
                resources=(ResourceSpec("uc_table", "main.finance.ledger"),),
                user_api_scopes=(),
                principal="reader",
            ),
            OperationAuthorization(
                name="ask_user",
                execution_identity="user",
                requires_request_context=True,
                resources=(),
                user_api_scopes=("sql",),
            ),
        ),
        user_resources=(),
        service_resources=(ResourceSpec("uc_table", "main.finance.ledger"),),
        user_api_scopes=("sql",),
        app_dependencies=(),
        agent_identity="named-sp:reader",
    )

    lines = authorization_summary_lines(plan, [], AppFamilyPermissions())
    assert "Agent principal:" in lines
    principal = lines[lines.index("Agent principal:") + 1]
    assert "reader" in principal
    assert "app service principal" in principal
    read = next(line for line in lines if line.startswith("  - read_ledger"))
    assert "as reader" in read


def test_summary_without_an_agent_identity_names_the_app_default() -> None:
    plan = AuthorizationPlan(
        operations=(),
        user_resources=(),
        service_resources=(),
        user_api_scopes=(),
        app_dependencies=(),
    )

    lines = authorization_summary_lines(plan, [], AppFamilyPermissions())
    principal = lines[lines.index("Agent principal:") + 1]
    assert "app service principal" in principal


def test_finalize_stamps_inherited_identity_and_a_tool_override() -> None:
    """The real seam: finalize_agent reads [tool.apx.agent] identity and
    leaves a per-tool identity alone."""
    from apx_agent._wiring import finalize_agent

    plain = _tool("plain")
    explicit = _tool("explicit", identity="obo")
    agent = LlmAgent(tools=[plain, explicit])
    config = AgentConfig(name="triage", identity="named-sp:reader")

    finalize_agent(agent, config=config, pyproject_path="/nonexistent/pyproject.toml")

    plain_md = get_tool_metadata(plain)
    explicit_md = get_tool_metadata(explicit)
    assert plain_md is not None and plain_md.service_principal_name == "reader"
    assert explicit_md is not None and explicit_md.execution == "user"

    # An inherited identity is a scope, so the request stamps the principal.
    before_tool = agent._before_tool
    span = _FakeSpan()

    import apx_agent._defaults as defaults
    import apx_agent._tool_scope as tool_scope

    headers = defaults.get_databricks_headers(
        user_id="user@example.com",
        request_id=None,
    )
    original = tool_scope.current_active_span
    tool_scope.current_active_span = lambda: span
    original_headers = defaults.get_databricks_headers
    defaults.get_databricks_headers = lambda: headers
    try:
        # The served hook is called with the tool name, matching LangChain's
        # callback contract (see the scope-enforcement test).
        before_tool("plain", {"query": "q"})
    finally:
        tool_scope.current_active_span = original
        defaults.get_databricks_headers = original_headers

    assert span.attributes[AuditAttrs.PRINCIPAL] == "service-principal:reader"
    assert span.attributes[AuditAttrs.USER_HASH]
    assert "user@example.com" not in str(span.attributes[AuditAttrs.USER_HASH])


