"""publish_tools_to_uc — sync @tool(uc=...) functions into Unity Catalog.

Walks an agent tree, finds every tool annotated with ``@tool(uc=...)``, and
registers each as a Unity Catalog Python function via ``unitycatalog-ai``.
Applies declarative ``grant=[...]`` lists at the same time.

After publishing, the tools are reachable by any UC-aware client (Genie,
Managed MCP, other agents, AI Playground), governed by standard UC
permissions, and auditable through UC's normal lineage and access logs.
The agent itself still executes the Python in-process at runtime — the UC
function is the discovery / external-composition surface, not the
execution path during the LLM loop.

Requires the ``uc`` extra::

    pip install 'apx-agent[uc]'

This pulls in ``unitycatalog-ai``, which provides the
``DatabricksFunctionClient.create_python_function`` primitive used here.
Grants go through the Databricks SDK's permissions API directly.

Usage::

    from apx_agent import Agent, log_agent, publish_tools_to_uc, tool

    @tool(uc="main.agents.tools.classify_intent", grant=["agent_consumers"])
    def classify_intent(query: str) -> str:
        \"\"\"Classify a customer query as billing/technical/account/other.\"\"\"
        return "billing" if "bill" in query.lower() else "other"

    agent = Agent(tools=[classify_intent], instructions="...")

    publish_tools_to_uc(agent)            # registers + grants
    log_agent(agent, model="...", registered_model_name="main.agents.triage")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._resources import _iter_tool_fns
from ._tool import ToolMetadata, get_tool_metadata

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from ._agents import BaseAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishResult:
    """Outcome of publishing a single ``@tool(uc=...)`` to Unity Catalog."""

    uc_name: str
    function_name: str
    grants_applied: tuple[str, ...]
    skipped: bool = False
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_uc_client(client: Any | None) -> Any:
    """Return a ``unitycatalog-ai`` DatabricksFunctionClient instance."""
    if client is not None:
        return client
    try:
        # The OSS Unity Catalog AI package layout (unitycatalog/unitycatalog-ai)
        from unitycatalog.ai.core.databricks import DatabricksFunctionClient
    except ImportError as e:  # pragma: no cover — exercised only without extra
        raise ImportError(
            "publish_tools_to_uc requires the unitycatalog-ai package. "
            "Install with: pip install 'apx-agent[uc]'"
        ) from e
    return DatabricksFunctionClient()


def _grant_execute(
    ws: "WorkspaceClient",
    uc_name: str,
    principal: str,
) -> None:
    """Apply ``GRANT EXECUTE ON FUNCTION <uc_name> TO <principal>``.

    Uses the Unity Catalog permissions API directly. Idempotent — the API
    is additive when EXECUTE is already present.
    """
    body = {
        "changes": [
            {
                "principal": principal,
                "add": ["EXECUTE"],
            }
        ]
    }
    # The UC permissions API takes the securable type + full name in the URL
    # and a list of permission changes in the body.
    ws.api_client.do(
        "PATCH",
        f"/api/2.1/unity-catalog/permissions/function/{uc_name}",
        body=body,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def publish_tools_to_uc(
    agent: "BaseAgent",
    *,
    client: Any | None = None,
    workspace_client: "WorkspaceClient | None" = None,
    replace: bool = True,
    dry_run: bool = False,
) -> list[PublishResult]:
    """Publish every ``@tool(uc=...)`` in the agent tree to Unity Catalog.

    Args:
        agent: The apx-agent ``BaseAgent`` to walk. All nested agents
            (Sequential, Parallel, Loop, Router, Handoff) are visited.
        client: Optional ``DatabricksFunctionClient`` instance to use. When
            omitted, a default client is constructed via
            ``unitycatalog.ai.core.databricks.DatabricksFunctionClient()``.
            Pass an explicit client when you need a non-default profile or
            workspace.
        workspace_client: Optional ``databricks.sdk.WorkspaceClient`` used
            for the GRANT step. When omitted, a default client is built
            from the ambient environment (which is the same auth chain
            the OBO / SP fallback uses elsewhere in apx-agent).
        replace: When ``True`` (default), pass ``replace=True`` to
            ``create_python_function`` so existing functions with the same
            name are overwritten. Set to ``False`` for a "fail if exists"
            mode useful in CI.
        dry_run: When ``True``, walks the tree and reports what would be
            published without making any UC writes. Useful for previewing
            in CI or local dev.

    Returns:
        One ``PublishResult`` per ``@tool(uc=...)`` found in the tree,
        including any that were skipped (and why).
    """
    results: list[PublishResult] = []

    seen: set[str] = set()
    targets: list[tuple[Any, ToolMetadata]] = []
    for fn in _iter_tool_fns(agent):
        meta = get_tool_metadata(fn)
        if meta is None or meta.uc_name is None:
            continue
        if meta.uc_name in seen:
            # Same UC name registered by multiple references to the same
            # tool — publish once.
            continue
        seen.add(meta.uc_name)
        targets.append((fn, meta))

    if not targets:
        return results

    if dry_run:
        for fn, meta in targets:
            results.append(
                PublishResult(
                    uc_name=meta.uc_name or "",
                    function_name=fn.__name__,
                    grants_applied=meta.grants,
                    skipped=True,
                    skip_reason="dry_run",
                )
            )
        return results

    uc_client = _load_uc_client(client)
    ws = workspace_client
    if ws is None and any(meta.grants for _, meta in targets):
        # Only build a default WorkspaceClient if we actually need one for
        # grants — keeps the no-grants path SDK-free.
        from databricks.sdk import WorkspaceClient as _WS
        ws = _WS()

    for fn, meta in targets:
        assert meta.uc_name is not None  # narrowed by the filter above
        catalog, schema, _function = meta.uc_name.split(".")
        try:
            uc_client.create_python_function(
                func=fn,
                catalog=catalog,
                schema=schema,
                replace=replace,
            )
        except Exception as e:
            logger.exception("Failed to publish %s to UC", meta.uc_name)
            raise RuntimeError(
                f"Failed to publish {meta.uc_name!r} to UC: {e}"
            ) from e

        grants_applied: list[str] = []
        for principal in meta.grants:
            try:
                assert ws is not None
                _grant_execute(ws, meta.uc_name, principal)
                grants_applied.append(principal)
            except Exception as e:
                logger.warning(
                    "GRANT EXECUTE on %s to %s failed: %s — "
                    "function is published but ungranted.",
                    meta.uc_name, principal, e,
                )

        results.append(
            PublishResult(
                uc_name=meta.uc_name,
                function_name=fn.__name__,
                grants_applied=tuple(grants_applied),
            )
        )
        logger.info(
            "Published %s to UC (grants: %s)",
            meta.uc_name, ", ".join(grants_applied) or "none",
        )

    return results
