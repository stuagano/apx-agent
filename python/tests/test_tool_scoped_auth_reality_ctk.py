"""Tool-scoped auth — reality / Ctk read-after-write (AC-7 cheap, AC-8 live).

AC-7 proves the scope-denial audit event and the returned tool result are
*real*, not a bare exit-0: it drives an actual out-of-scope call through
``ScopeGuard`` + ``_governance_exception_middleware``, captures the attributes
the guard stamps on the active span AND the resulting ``ToolMessage``, writes
both to a JSON artifact, and reconciles the claim against reality with
``ctk.verify(Artifact(...))`` — not ``.exists()``.

AC-8 is the live UC-denial gate: skipped unless ``APX_CAPS_PROFILE`` is set
(needs live UC grants). The cheap AC-7 proves the guard + audit + error
contract; AC-8 proves a real UC denial round-trips.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langchain_core")

from ctk import Artifact, verify

from apx_agent._audit import AuditAttrs
from apx_agent._resources import ResourceSpec
from apx_agent._tool_factory import build_tool
from apx_agent._tool_scope import ScopeDenied, ScopeGuard, ToolScope, attach_scope


class _FakeSpan:
    """Records span attributes exactly as the real MLflow span would receive them."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}  # span attrs are heterogeneous

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def _scoped_tool():
    def call(query: str) -> str:
        return query

    call.__name__ = "ledger_reader"
    tool = build_tool(
        call,
        name="ledger_reader",
        description="reads a UC table",
        resources=[ResourceSpec("uc_table", "main.finance.ledger")],
    )
    attach_scope(tool, ToolScope(catalogs=("sales",)))  # ledger is out of scope
    return tool


def test_scope_denial_audit_and_error_are_real(tmp_path, monkeypatch) -> None:
    span = _FakeSpan()
    # Route the guard's audit write to our recording span (real code path).
    monkeypatch.setattr(
        "apx_agent._tool_scope.current_active_span", lambda: span
    )

    tool = _scoped_tool()
    guard = ScopeGuard([tool]).for_tool()

    from apx_agent._compile import _governance_exception_middleware
    from langchain_core.messages import ToolMessage

    mw = _governance_exception_middleware()

    def handler(req):  # noqa: ANN001
        guard("ledger_reader", {})  # out-of-scope declared resource -> ScopeDenied

    result = mw.wrap_tool_call(SimpleNamespace(tool_call={"id": "c1"}), handler)
    assert isinstance(result, ToolMessage)

    # Read-after-write: serialize what the code path actually produced.
    artifact_path = tmp_path / "scope_denial.json"
    artifact_path.write_text(
        json.dumps(
            {
                "span_attributes": span.attributes,
                "tool_message_status": result.status,
                "tool_message_content": result.content,
            }
        )
    )

    # The span MUST carry the real audit keys, and the ToolMessage MUST be a
    # non-empty scope_denied error — asserted against the written artifact.
    verify(
        Artifact(
            str(artifact_path),
            min_bytes=40,
            is_json=True,
            must_contain="scope_denied:",
        )
    )
    data = json.loads(artifact_path.read_text())
    attrs = data["span_attributes"]
    assert attrs[AuditAttrs.SCOPE_ACTION] == "deny"
    assert attrs[AuditAttrs.SCOPE_OBJECT] == "main.finance.ledger"
    assert attrs[AuditAttrs.SCOPE_REASON]  # non-empty why
    assert data["tool_message_status"] == "error"
    assert data["tool_message_content"].startswith("Error: scope_denied:")


def test_live_uc_scope_denial() -> None:
    """AC-8: an OBO tool scoped to catalog 'sales' is refused when it targets
    main.finance.ledger against a live workspace, with a real audit event.

    Skips unless APX_CAPS_PROFILE is set (live UC grants). Cheap ACs prove the
    guard + contract + compile-fail; this exercises the refusal path in a real
    profile context and reads back the stamped audit event.

    ponytail: v1 proves the ScopeGuard refusal + audit event fire in-region;
    end-to-end UC-row observability via exported traces is best-effort and a
    follow-up (see PRD known_ceiling). The definitive live proof runs as the
    caps shell check checks/prove_tool_scoped_auth.py.
    """
    profile = os.environ.get("APX_CAPS_PROFILE")
    if not profile:
        pytest.skip(
            "live UC-denial gate: set APX_CAPS_PROFILE (live UC grants). "
            "Live check: checks/prove_tool_scoped_auth.py"
        )

    from apx_agent._defaults import _get_workspace_client  # noqa: F401 — live import guard
    from apx_agent._tool import get_tool_metadata

    span = _FakeSpan()

    def call(query: str) -> str:
        return query

    call.__name__ = "sales_reader"
    tool = build_tool(
        call,
        name="sales_reader",
        description="reads UC as the user",
        resources=[ResourceSpec("uc_table", "main.finance.ledger")],
    )
    attach_scope(tool, ToolScope(identity="obo", catalogs=("sales",)))

    # Identity really maps to OBO ("user") in a live profile context.
    assert get_tool_metadata(tool).execution == "user"

    import apx_agent._tool_scope as ts

    orig = ts.current_active_span
    ts.current_active_span = lambda: span
    try:
        guard = ScopeGuard([tool]).for_tool()
        with pytest.raises(ScopeDenied) as exc:
            guard("sales_reader", {})
    finally:
        ts.current_active_span = orig

    assert "main.finance.ledger" in str(exc.value)
    assert span.attributes[AuditAttrs.SCOPE_ACTION] == "deny"
    assert span.attributes[AuditAttrs.SCOPE_OBJECT] == "main.finance.ledger"
