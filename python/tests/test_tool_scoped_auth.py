"""Tool-scoped auth — per-tool identity + UC/secret scope ceiling (cheap tier).

Covers AC-1..AC-6 of the tool_scoped_auth PRD:

  * AC-1 declaration parses into ToolScope on fn._apx_scope; obo -> execution 'user'
  * AC-2 over-scoped tool hard-fails build with ToolConfigError naming tool+identifier
  * AC-3 out-of-scope call raises ScopeDenied, contained as a scope_denied ToolMessage
  * AC-4 ScopeDenied is distinct from ToolError, is a PermissionError, scope_denied:-prefixed
  * AC-5 identity sp/obo/named-sp maps to service/user/service; conflict with inferred raises
  * AC-6 undeclared scope -> get_scope None, ScopeGuard no-op, unrestricted (back-compat)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apx_agent._errors import ToolError
from apx_agent._resources import ResourceSpec, attach_resources
from apx_agent._tool import get_tool_metadata
from apx_agent._tool_config import ToolConfigError, _registry, load_config_tools
from apx_agent._tool_factory import build_tool
from apx_agent._tool_scope import (
    ScopeDenied,
    ScopeGuard,
    ToolScope,
    attach_scope,
    get_scope,
    identity_to_execution,
    in_scope,
    parse_tool_scope,
    validate_tool_scope,
)


# ---------------------------------------------------------------------------
# Helpers — a lightweight config factory so load_config_tools exercises the
# real _build_one (pops identity/scope/secret_scopes, attaches, maps identity)
# and load_config_tools (validate_tool_scope) without needing a live workspace.
# ---------------------------------------------------------------------------


def _make_tool(name: str, uc_resources: tuple[str, ...] = ()):
    def call(query: str) -> str:
        return query

    call.__name__ = name
    tool = build_tool(
        call,
        name=name,
        description=f"test tool {name}",
        resources=[ResourceSpec("uc_table", ident) for ident in uc_resources],
    )
    return tool


@pytest.fixture
def config_factory(monkeypatch):
    """Register a 'demo' factory that builds a tool with declared UC tables.

    The table's ``uc_tables`` key (a plain list) becomes uc_table ResourceSpecs
    so over-scope validation has something to reject.
    """

    def demo(**kwargs):
        name = kwargs.get("name", "demo_tool")
        uc_tables = tuple(kwargs.get("uc_tables", ()))
        return _make_tool(name, uc_tables)

    real = _registry()
    monkeypatch.setattr(
        "apx_agent._tool_config._registry", lambda: {**real, "demo": demo}
    )
    return demo


# ---------------------------------------------------------------------------
# AC-1 — declaration parses into ToolScope; obo -> execution "user"
# ---------------------------------------------------------------------------


def test_declaration_parses_into_scope(config_factory) -> None:
    table = {
        "type": "demo",
        "name": "sales_lookup",
        "identity": "obo",
        "scope": {"catalogs": ["sales"], "tables": ["sales.crm.leads"]},
        "secret_scopes": ["sales-secrets"],
    }
    [fn] = load_config_tools([table])

    scope = get_scope(fn)
    assert isinstance(scope, ToolScope)
    assert scope.identity == "obo"
    assert scope.catalogs == ("sales",)
    assert scope.tables == ("sales.crm.leads",)
    assert scope.secret_scopes == ("sales-secrets",)

    metadata = get_tool_metadata(fn)
    assert metadata is not None
    assert metadata.execution == "user"  # obo -> user


# ---------------------------------------------------------------------------
# AC-2 — over-scoped tool hard-fails the build
# ---------------------------------------------------------------------------


def test_compile_fails_on_overscope(config_factory) -> None:
    table = {
        "type": "demo",
        "name": "ledger_reader",
        "identity": "obo",
        "scope": {"catalogs": ["sales"]},  # ceiling: only catalog 'sales'
        "uc_tables": ["main.finance.ledger"],  # declared resource outside it
    }
    with pytest.raises(ToolConfigError) as exc:
        load_config_tools([table])

    msg = str(exc.value)
    assert "ledger_reader" in msg  # names the tool
    assert "main.finance.ledger" in msg  # names the offending identifier


def test_in_scope_semantics() -> None:
    scope = ToolScope(
        catalogs=("sales",),
        schemas=("main.crm",),
        tables=("main.finance.ledger",),
    )
    assert in_scope(scope, "sales.crm.leads")  # under allowed catalog
    assert in_scope(scope, "main.crm.contacts")  # under allowed schema
    assert in_scope(scope, "main.finance.ledger")  # exact table
    assert not in_scope(scope, "main.finance.gl")  # different table, no catalog/schema
    assert not in_scope(scope, "other.crm.x")  # unlisted catalog
    # No UC dimension declared -> everything in scope (back-compat).
    assert in_scope(ToolScope(), "anything.at.all")


def test_in_scope_is_case_and_backtick_insensitive() -> None:
    # UC identifiers are case-insensitive; backtick-quoting is cosmetic.
    scope = ToolScope(catalogs=("Sales",), tables=("main.finance.ledger",))
    assert in_scope(scope, "sales.crm.leads")  # catalog case differs
    assert in_scope(scope, "main.finance.LEDGER")  # exact table, case differs
    assert in_scope(scope, "`main`.`finance`.`ledger`")  # backtick-quoted
    assert not in_scope(scope, "main.finance.gl")  # still out of scope


def test_build_tool_python_api_validates_overscope() -> None:
    # Fix #4: the Python build_tool() path validates scope at build time too.
    def call(query: str) -> str:
        return query

    with pytest.raises(ToolConfigError, match="over-scoped"):
        build_tool(
            call,
            name="py_ledger",
            description="over-scoped via python api",
            resources=[ResourceSpec("uc_table", "main.finance.ledger")],
            scope=ToolScope(catalogs=("sales",)),
        )


def test_scope_arg_name_is_not_treated_as_secret() -> None:
    # Fix #2: a plain "scope" arg (OAuth/search/config scope) is NOT a secret
    # scope and must not trip ScopeDenied.
    tool = _make_tool("oauth_tool")
    attach_scope(tool, ToolScope(secret_scopes=("sales-secrets",)))
    guard = ScopeGuard([tool]).for_tool()
    guard("oauth_tool", {"scope": "read:profile"})  # no denial


# ---------------------------------------------------------------------------
# AC-3 — out-of-scope call raises ScopeDenied, contained (no 500)
# ---------------------------------------------------------------------------


def _governed_middleware():
    from apx_agent._compile import _governance_exception_middleware

    return _governance_exception_middleware()


def test_out_of_scope_raises_scope_denied() -> None:
    tool = _make_tool("scoped_reader", uc_resources=("main.finance.ledger",))
    attach_scope(tool, ToolScope(catalogs=("sales",)))  # ledger is out of scope

    guard = ScopeGuard([tool]).for_tool()
    with pytest.raises(ScopeDenied) as exc:
        guard("scoped_reader", {})
    assert str(exc.value).startswith("scope_denied:")

    # The governance middleware contains it as an error ToolMessage (no 500).
    from langchain_core.messages import ToolMessage

    mw = _governed_middleware()

    def handler(req):  # noqa: ANN001
        guard("scoped_reader", {})  # raises ScopeDenied

    out = mw.wrap_tool_call(SimpleNamespace(tool_call={"id": "c1"}), handler)
    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert out.content.startswith("Error: scope_denied:")
    assert out.tool_call_id == "c1"


def test_out_of_scope_call_argument_denied() -> None:
    # A well-known UC arg carrying an out-of-scope FQN is refused too.
    tool = _make_tool("arg_reader")
    attach_scope(tool, ToolScope(catalogs=("sales",)))
    guard = ScopeGuard([tool]).for_tool()
    with pytest.raises(ScopeDenied):
        guard("arg_reader", {"table_name": "main.finance.ledger"})
    # In-scope arg passes.
    guard("arg_reader", {"table_name": "sales.crm.leads"})


def test_secret_scope_out_of_allowlist_denied() -> None:
    tool = _make_tool("secret_reader")
    attach_scope(tool, ToolScope(secret_scopes=("sales-secrets",)))
    guard = ScopeGuard([tool]).for_tool()
    with pytest.raises(ScopeDenied):
        guard("secret_reader", {"secret_scope": "finance-secrets"})
    guard("secret_reader", {"secret_scope": "sales-secrets"})  # allowed


# ---------------------------------------------------------------------------
# AC-4 — ScopeDenied contract distinct from ToolError
# ---------------------------------------------------------------------------


def test_scope_denied_contract_distinct() -> None:
    assert not issubclass(ScopeDenied, ToolError)  # distinct type
    assert issubclass(ScopeDenied, PermissionError)  # rides _CONTAINED
    err = ScopeDenied("scope_denied: tool 'q' may not touch 'main.finance.ledger'")
    assert str(err).startswith("scope_denied:")
    # A ToolError is NOT a ScopeDenied — the two contracts don't overlap.
    assert not isinstance(ToolError("x"), ScopeDenied)


# ---------------------------------------------------------------------------
# AC-5 — identity mode maps to execution; conflict with inferred raises
# ---------------------------------------------------------------------------


def test_identity_mode_maps_to_execution() -> None:
    assert identity_to_execution("sp") == "service"
    assert identity_to_execution("obo") == "user"
    assert identity_to_execution("named-sp:reader") == "service"

    # named-sp name rides ToolMetadata.
    tool = _make_tool("named_reader")
    attach_scope(tool, ToolScope(identity="named-sp:reader"))
    md = get_tool_metadata(tool)
    assert md is not None
    assert md.execution == "service"
    assert md.service_principal_name == "reader"

    # Malformed identity fails loud.
    with pytest.raises(ToolConfigError):
        parse_tool_scope(identity="root")
    with pytest.raises(ToolConfigError):
        parse_tool_scope(identity="named-sp:")  # missing name


def test_identity_conflict_with_inferred_raises(monkeypatch) -> None:
    # A tool whose declared identity conflicts with the identity inferred from
    # its dependencies must fail loud — reusing _apps_authorization's raise.
    from apx_agent._apps_authorization import infer_operation_authorization
    from apx_agent._defaults import _get_workspace_client

    def conflicted(query: str) -> str:
        return query

    # Dependency requires "service" ...
    monkeypatch.setattr(
        "apx_agent._apps_authorization._tool_dependency_callables",
        lambda _fn: {"ws": _get_workspace_client},
    )
    # ... but the declared identity is obo (-> "user"). Conflict.
    attach_scope(conflicted, ToolScope(identity="obo"))

    with pytest.raises(ValueError, match="conflicted.*user.*service"):
        infer_operation_authorization(conflicted)


# ---------------------------------------------------------------------------
# AC-6 — undeclared scope: get_scope None, ScopeGuard no-op, unrestricted
# ---------------------------------------------------------------------------


def test_undeclared_scope_unrestricted() -> None:
    tool = _make_tool("plain_tool", uc_resources=("main.finance.ledger",))
    assert get_scope(tool) is None  # nothing attached

    validate_tool_scope(tool)  # no-op, does not raise even though "over-scoped"

    guard_obj = ScopeGuard([tool])
    assert guard_obj.active is False  # no scoped tools -> inert
    guard = guard_obj.for_tool()
    # Any call, any args — no denial (byte-for-byte today's unrestricted path).
    guard("plain_tool", {"table_name": "main.finance.ledger"})
    guard("plain_tool", {"secret_scope": "anything"})


# ---------------------------------------------------------------------------
# Served-path enforcement — ScopeGuard is auto-wired into the before_tool chain
# by the production wiring (apply_config_guardrails), NOT hand-built. This is
# the test that catches the "runtime guard never connected" gap.
# ---------------------------------------------------------------------------


def test_served_before_tool_chain_enforces_scope() -> None:
    from langchain_core.messages import ToolMessage

    from apx_agent import AgentConfig, LlmAgent
    from apx_agent._wiring import apply_config_guardrails

    scoped = _make_tool("scoped_reader", uc_resources=("main.finance.ledger",))
    attach_scope(scoped, ToolScope(catalogs=("sales",)))  # ledger is out of scope

    agent = LlmAgent(tools=[scoped])
    # No guardrails declared — scope wiring must still happen (driven by tools,
    # independent of [tool.apx.agent.guardrails]).
    apply_config_guardrails(agent, AgentConfig(name="served-scope-test"))

    before_tool = agent._before_tool
    assert before_tool is not None, "ScopeGuard was not wired into the served chain"

    # The wired hook denies the out-of-scope declared resource.
    with pytest.raises(ScopeDenied) as exc:
        before_tool("scoped_reader", {})
    assert str(exc.value).startswith("scope_denied:")

    # And the real serve containment turns it into a scope_denied ToolMessage
    # (turn stays alive, no HTTP 500).
    mw = _governed_middleware()

    def handler(req):  # noqa: ANN001
        before_tool("scoped_reader", {})

    out = mw.wrap_tool_call(SimpleNamespace(tool_call={"id": "srv1"}), handler)
    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert out.content.startswith("Error: scope_denied:")


def test_served_chain_unchanged_when_no_scoped_tools() -> None:
    from apx_agent import AgentConfig, LlmAgent
    from apx_agent._wiring import apply_config_guardrails

    agent = LlmAgent(tools=[_make_tool("plain_tool")])  # no scope attached
    baseline = agent._before_tool
    apply_config_guardrails(agent, AgentConfig(name="plain-test"))
    # Byte-for-byte unchanged: no scoped tools + no guardrails => nothing wired.
    assert agent._before_tool is baseline


def test_parse_returns_none_when_nothing_declared() -> None:
    assert parse_tool_scope(identity=None, scope=None, secret_scopes=None) is None


def test_parse_rejects_unknown_scope_key() -> None:
    with pytest.raises(ToolConfigError, match="unknown key"):
        parse_tool_scope(scope={"catalog": ["typo-not-catalogs"]})
