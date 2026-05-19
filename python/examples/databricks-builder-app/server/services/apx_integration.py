"""apx-agent integration helpers for the Claude Code Agent SDK runtime.

This app's agent runtime is the Claude Code Agent SDK (`claude_agent_sdk`),
not apx-agent's Mosaic AI ChatAgent. The two runtimes are complementary:
apx-agent for Model Serving deployments, Claude Code for builder/IDE flows.

What this module does: thread apx-agent's **tracing and audit-attrs**
conventions around each Claude session so the trace stream produced by
this app lands under the same `apx.*` span schema as a deployed
apx-agent. Downstream consumers — `apx cost`, `apx export-traces`,
watchdog dashboards — then work on this app's traces without special
casing.

What this module does NOT do: replace the Claude Code runtime, the
WorkspaceClient auth flow, or the MCP gateway.

If apx-agent isn't installed in the runtime image, every helper here
degrades to a no-op via best-effort imports — so the app continues to
work even when this integration is unwired.

Three call sites:

  * `wrap_agent_span(...)` — context manager wrapping a single agent turn.
    Sets `apx.agent_name`, `apx.principal_id_hash`, `apx.model`, and
    `apx.span.type=AGENT` on the active span before the Claude session
    starts.

  * `record_tool_invocation(...)` — call this from the agent service's
    tool-result handler to add per-tool spans tagged with `apx.tool.name`
    + `apx.tool.outcome`, matching apx-agent's tool-invocation schema.

  * `hash_principal(...)` — convenience re-export of apx-agent's
    `hash_for_audit` for principal identity hashing. Pass the calling
    user's email or principal id; never the raw token.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Best-effort imports — degrade to no-ops when apx-agent isn't installed.
# ---------------------------------------------------------------------------

try:
    from apx_agent import (  # type: ignore[attr-defined]
        hash_for_audit,
        safe_span,
        set_audit_attrs,
    )

    _APX_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only when apx-agent is absent
    _APX_AVAILABLE = False

    def hash_for_audit(value: str | bytes) -> str:  # type: ignore[no-redef]
        """No-op fallback. Returns the value unchanged."""
        return str(value)

    def set_audit_attrs(span: Any, **kwargs: Any) -> None:  # type: ignore[no-redef]
        """No-op fallback."""
        return

    @contextlib.contextmanager
    def safe_span(  # type: ignore[no-redef]
        name: str, span_type: str | None = None, **_kwargs: Any
    ) -> Iterator[Any]:
        """No-op fallback yielding a sentinel span object."""
        yield None


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def hash_principal(value: str | bytes | None) -> str | None:
    """Hash a principal identifier (e.g. email) for audit-safe logging.

    Returns None when the input is None/empty so callers can pass through
    optional auth context without branching.
    """
    if not value:
        return None
    return hash_for_audit(value)


@contextlib.contextmanager
def wrap_agent_span(
    *,
    project_id: str,
    user_identity: str | None,
    model: str | None,
    session_id: str | None = None,
) -> Iterator[Any]:
    """Wrap a single Claude agent turn with an apx-shaped trace span.

    Sets the canonical `apx.*` span attributes so this app's traces are
    interoperable with `apx export-traces`, `apx cost`, and watchdog
    dashboards.

    Yields the span (or None when MLflow tracing isn't configured /
    apx-agent isn't installed). Always safe to use — never raises out of
    the no-op fallback path.

    Args:
        project_id: The chat / project id — used as `apx.agent_name`.
        user_identity: Calling user's email or principal id. Hashed before
            it lands on the span; the raw value never leaves this function.
        model: LLM endpoint or model name. Reported as `apx.model`.
        session_id: Conversation id when present. Reported as
            `apx.session_id`. Most chat surfaces want this for stitching
            turns into a single trace tree.
    """
    with safe_span("apx.invoke", span_type="AGENT") as span:
        try:
            set_audit_attrs(
                span,
                agent_name=project_id,
                principal_id_hash=hash_principal(user_identity),
                model=model,
                session_id=session_id,
            )
        except Exception:
            # Never let an audit-attrs failure tank a real agent turn.
            logger.exception("set_audit_attrs failed; continuing")
        yield span


@contextlib.contextmanager
def wrap_tool_span(
    *, tool_name: str, principal_id_hash: str | None = None
) -> Iterator[Any]:
    """Wrap a single tool invocation with an apx-shaped span.

    Sets `apx.tool.name` and (optionally) `apx.principal_id_hash`. The
    caller should set `apx.tool.outcome` ("ok"|"error") on the yielded
    span before exit via :func:`record_tool_outcome`.

    The Claude Code agent service is welcome to skip this and rely on
    `mlflow.anthropic.autolog()` for tool tracing — the two layers
    compose; this just adds the apx-shaped attributes.
    """
    with safe_span("apx.tool", span_type="TOOL") as span:
        try:
            set_audit_attrs(
                span,
                tool_name=tool_name,
                principal_id_hash=principal_id_hash,
            )
        except Exception:
            logger.exception("tool span audit-attrs failed; continuing")
        yield span


def record_tool_outcome(span: Any, *, ok: bool, error: str | None = None) -> None:
    """Record the outcome of a tool invocation on its span.

    Mirrors apx-agent's `apx.tool.outcome` schema (`ok` | `error`).
    """
    if span is None:
        return
    try:
        set_audit_attrs(
            span,
            tool_outcome="ok" if ok else "error",
            tool_error=error if not ok else None,
        )
    except Exception:
        logger.exception("record_tool_outcome failed; continuing")


def is_apx_available() -> bool:
    """Return True when apx-agent is importable in the running interpreter.

    Useful at startup to log whether the integration is wired live or
    running in no-op fallback mode.
    """
    return _APX_AVAILABLE
