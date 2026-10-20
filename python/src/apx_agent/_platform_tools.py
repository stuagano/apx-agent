"""Tools declared in the project's agent.toml, referenced from Python (see
docs/design/agent-toml-source-of-truth.md). agent.toml says what platform tools
exist; platform_tools() says which agent uses them."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._agents import BaseAgent, LlmAgent


@dataclass(frozen=True)
class PlatformToolRef:
    """Placeholder for agent.toml tools; resolved at startup or per request."""

    ids: frozenset[str]


def platform_tools(*ids: str) -> list[PlatformToolRef]:
    """Reference agent.toml tools by id (no ids = all). Splat into ``tools=[...]``."""
    return [PlatformToolRef(frozenset(ids))]


def platform_tool_leaves(agent: BaseAgent) -> list[tuple[LlmAgent, PlatformToolRef]]:
    """Every (LlmAgent leaf, ref) pair in the agent graph, in graph order."""
    from ._agents import LlmAgent
    from ._wiring import _collect_guardrail_targets

    return [
        (leaf, ref)
        for leaf in _collect_guardrail_targets(agent)
        if isinstance(leaf, LlmAgent)
        for ref in leaf._platform_tool_refs
    ]


ClientFor = Callable[[str], Any]
_MCP_KINDS = frozenset({"mcp", "uc_function", "genie_one", "sandbox"})


@contextlib.contextmanager
def _pinned_root(project_root: Path) -> Iterator[None]:
    """AgentKit walks up from cwd for agent.toml; pin it to the project apx resolved."""
    previous = os.environ.get("AGENTBRICKS_PROJECT_ROOT")
    os.environ["AGENTBRICKS_PROJECT_ROOT"] = str(project_root.resolve())
    try:
        yield
    finally:
        if previous is None:
            del os.environ["AGENTBRICKS_PROJECT_ROOT"]
        else:
            os.environ["AGENTBRICKS_PROJECT_ROOT"] = previous


def _records(project_root: Path) -> list[Any]:
    from databricks_agentkit.runtime.tool_manifest import load_tools

    with _pinned_root(project_root):
        return list(load_tools(expected_framework="langgraph"))


def _mcp_server_for(record: Any, client_for: ClientFor) -> Any:
    # ponytail: the one private AgentKit import; pinned by test_private_mcp_seam_still_exists.
    # Upgrade path: a public per-record server builder in AgentKit.
    from databricks_agentkit.langgraph.mcp import _server_from_tool

    return _server_from_tool(record, workspace_client_for=client_for)


def _sync_bridged(tools: list[Any]) -> list[Any]:
    """Give coroutine-only tools (what MCP adapters return) a sync ``func``.

    The durable graph invokes tools synchronously; the bridge runs the original
    coroutine via ``_run_sync`` (the same pattern as ``_compile``'s async tools)
    and leaves ``coroutine`` intact for async callers.
    """
    from .mcp_consume import _run_sync

    def bridge(tool: Any) -> Any:
        coroutine = tool.coroutine

        def func(*args: Any, **kwargs: Any) -> Any:
            return _run_sync(coroutine(*args, **kwargs))

        return tool.model_copy(update={"func": func})

    return [bridge(t) if t.func is None and t.coroutine is not None else t for t in tools]


async def _fetch_mcp(records: list[Any], client_for: ClientFor) -> dict[str, list[Any]]:
    from databricks_agentkit.langgraph.mcp import mcp_client
    from databricks_agentkit.runtime.auth import AuthError
    from databricks_agentkit.runtime.mcp_auth import mcp_auth_error

    servers = [s for r in records if (s := _mcp_server_for(r, client_for)) is not None]
    if not servers:
        return {}
    client = mcp_client(servers, workspace_client_for=client_for, tools=tuple(records))
    request_user = {r.id for r in records if r.auth == "user"}

    async def fetch(name: str) -> list[Any]:
        try:
            return _sync_bridged(await client.get_tools(server_name=name))
        except Exception as error:
            if name not in request_user:
                raise
            # Same mapping as AgentKit's mcp_tools: credential-free, per integration.
            raise mcp_auth_error(error, name) or AuthError(
                "MCP_TOOL_FAILED", "Could not discover configured MCP tools.", 502, name) from None

    return {s.name: await fetch(s.name) for s in servers}


@dataclass(frozen=True)
class StaticPlatformTools:
    """Startup-built tools by id, and each server that failed to build (id → error)."""

    tools: dict[str, list[Any]]
    failures: dict[str, str]


def _failure(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def static_platform_tools(project_root: Path, client_for: ClientFor) -> StaticPlatformTools:
    """Tools safe to build once: Genie (identity per call) and app-auth MCP.

    Genie and each app-auth MCP server build in isolation: one failure is
    recorded by id and never drops the others."""
    from databricks_agentkit.langgraph.genie import genie_tools

    from ._agent_toml import load_agent_toml, platform_tool_names
    from .mcp_consume import _run_sync

    project = load_agent_toml(project_root)
    if project is None:
        raise FileNotFoundError(f"no agent.toml in {project_root}")
    records = _records(project_root)
    unknown = sorted(r.id for r in records if r.kind not in _MCP_KINDS | {"genie_agent"})
    if unknown:
        raise ValueError(f"agent.toml tool kind not supported by platform_tools(): {unknown}")
    owner = {
        name: tool_id
        for tool_id, names in platform_tool_names(project).items()
        for name in names
    }
    out: dict[str, list[Any]] = {}
    failures: dict[str, str] = {}
    genie_ids = [r.id for r in records if r.kind == "genie_agent"]
    if genie_ids:
        try:
            with _pinned_root(project_root):
                # Coroutine-only like MCP tools; the durable graph calls tools sync.
                for tool in _sync_bridged(genie_tools(workspace_client_for=client_for)):
                    out.setdefault(owner[tool.name], []).append(tool)
        except Exception as exc:  # recorded for /readyz, never swallowed
            failures[", ".join(genie_ids)] = _failure(exc)
    for record in records:
        if record.kind not in _MCP_KINDS or record.auth == "user":
            continue
        try:
            out.update(_run_sync(_fetch_mcp([record], client_for)))
        except Exception as exc:  # recorded for /readyz, never swallowed
            failures[record.id] = _failure(exc)
    return StaticPlatformTools(out, failures)


async def request_platform_tools(
    project_root: Path, client_for: ClientFor, ids: frozenset[str]
) -> dict[str, list[Any]]:
    """User-auth MCP tools, fetched inside the request so each caller gets their own client."""
    records = [
        r for r in _records(project_root)
        if r.kind in _MCP_KINDS and r.auth == "user" and (not ids or r.id in ids)
    ]
    return await _fetch_mcp(records, client_for)
