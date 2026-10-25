"""Programmatic spec tool construction and legacy TOML migration diagnostics.

Each table is ``{type, <factory kwargs>}``. ``type`` selects a factory from the
registry; the remaining keys are splatted as keyword args (every factory takes
its identifier as positional-or-keyword, so all-keyword calls work uniformly).
Toolkit factories return lists, which are flattened. The factories are the
validation surface — a bad/missing kwarg surfaces as a wrapped ToolConfigError.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class ToolConfigError(ValueError):
    """Invalid tool declaration or unsupported legacy tool configuration."""


# Factory types whose construction touches the network (host-gated + skippable).
_IO_TYPES = {"openapi", "mcp_tool", "mcp_toolkit"}
# Which kwarg carries the host-bearing URL, per IO type.
_HOST_KEY: dict[str, str] = {
    "openapi": "spec",
    "mcp_tool": "server_url",
    "mcp_toolkit": "server_url",
}


def _resolve_env_deep(value: Any) -> Any:
    """Recursively resolve ``$VAR`` / ``${VAR}`` references in string leaves."""
    if isinstance(value, str):
        # Lazy local import to avoid a top-level cycle if _wiring ever imports us.
        from ._wiring import _resolve_env_var  # noqa: PLC0415

        return _resolve_env_var(value)
    if isinstance(value, list):
        return [_resolve_env_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_env_deep(v) for k, v in value.items()}
    return value


def _check_allowlist(index: int | None, type_: str, kwargs: dict[str, Any]) -> None:
    """Raise ToolConfigError if the tool's host is not in APX_TOOLS_ALLOWED_HOSTS."""
    allowed_raw = (os.environ.get("APX_TOOLS_ALLOWED_HOSTS") or "").strip()
    if not allowed_raw or type_ not in _IO_TYPES:
        return  # unset → trusted default; or non-network type → no restriction
    hosts = {h.strip() for h in allowed_raw.split(",") if h.strip()}
    url = kwargs.get(_HOST_KEY[type_], "") or ""
    if not url:
        return  # no URL to check; factory will raise TypeError for the missing kwarg
    host = urlparse(url).hostname or ""
    if host not in hosts:
        label = f"tool #{index}" if index is not None else "tool"
        raise ToolConfigError(
            f"{label} (type={type_}): host {host!r} is not in "
            f"APX_TOOLS_ALLOWED_HOSTS ({sorted(hosts)})."
        )


def skill_tool(name: str, description: str, path: str) -> Callable[[], str]:
    """Return a zero-argument callable that reads and returns a skill markdown file.

    The returned function is registered with the agent as a tool.  When the
    LLM calls it, the skill's markdown content is returned as the tool result.
    Relative *path* values are resolved from ``Path.cwd()`` at call time, which
    in a deployed Databricks App is the ``source_code_path`` root.

    :param name: Tool name exposed to the LLM, e.g. ``"sql_analysis"``.
    :param description: One-sentence description in the tool schema,
        e.g. ``"Best practices for writing SQL queries"``.
    :param path: Path to the markdown file.  Relative paths are resolved from
        ``Path.cwd()`` at invocation time, e.g. ``"skills/sql_analysis.md"``.
    :returns: A callable with ``__name__ = name`` and ``__doc__ = description``.
    :raises FileNotFoundError: At call time if the resolved path does not exist.
    """
    from pathlib import Path as _Path

    skill_path = _Path(path)

    def _load() -> str:
        resolved = skill_path if skill_path.is_absolute() else _Path.cwd() / skill_path
        return resolved.read_text()

    _load.__name__ = name
    _load.__doc__ = description
    return _load


def _registry() -> dict[str, Callable[..., Any]]:
    # Lazy imports keep this module cheap to import (factories pull in the SDK).
    from .catalog import (
        catalog_tool,
        lineage_tool,
        schema_tool,
        uc_function_tool,
        uc_function_toolkit,
    )
    from .foundation_model import foundation_model_tool
    from .genie import genie_query_tool, genie_tool
    from .knowledge_assistant import knowledge_assistant_tool
    from .http_tools import http_tool, openapi_tool
    from .jobs_tools import (
        jobs_for_table_tool,
        jobs_history_tool,
        jobs_logs_tool,
        jobs_source_paths_tool,
        jobs_tools,
    )
    from .mcp_consume import mcp_tool, mcp_toolkit
    from .document_extract import document_extract_tool
    from .sql_tools import sql_tool
    from .uc_comment import uc_comment_tool
    from .vector_search import vector_search_tool

    return {
        "genie": genie_tool,
        "genie_query": genie_query_tool,
        "vector_search": vector_search_tool,
        "uc_function": uc_function_tool,
        "uc_function_toolkit": uc_function_toolkit,
        "uc_comment_writer": uc_comment_tool,
        "catalog": catalog_tool,
        "schema": schema_tool,
        "lineage": lineage_tool,
        "sql": sql_tool,
        "document_extract": document_extract_tool,
        "http": http_tool,
        "openapi": openapi_tool,
        "mcp_tool": mcp_tool,
        "mcp_toolkit": mcp_toolkit,
        "foundation_model": foundation_model_tool,
        "knowledge_assistant": knowledge_assistant_tool,
        "jobs": jobs_tools,
        "jobs_for_table": jobs_for_table_tool,
        "jobs_history": jobs_history_tool,
        "jobs_logs": jobs_logs_tool,
        "jobs_source_paths": jobs_source_paths_tool,
        "skill": skill_tool,
    }


def _build_one(
    index: int, table: dict[str, Any], registry: dict[str, Callable[..., Any]]
) -> list[Callable[..., Any]]:
    from ._tool_scope import attach_scope, parse_tool_scope

    kwargs: dict[str, Any] = _resolve_env_deep(dict(table))
    type_ = kwargs.pop("type", None)
    if type_ is None:
        raise ToolConfigError(f"tool #{index}: missing 'type' key.")
    # Pop scope/identity keys (like 'type') before the factory splat.
    try:
        scope = parse_tool_scope(
            identity=kwargs.pop("identity", None),
            scope=kwargs.pop("scope", None),
            secret_scopes=kwargs.pop("secret_scopes", None),
            callers=kwargs.pop("callers", None),
        )
    except ToolConfigError as e:
        raise ToolConfigError(f"tool #{index} (type={type_}): {e}") from e
    factory = registry.get(type_)
    if factory is None:
        raise ToolConfigError(
            f"tool #{index}: unknown type {type_!r}; known: {sorted(registry)}."
        )
    _check_allowlist(index, type_, kwargs)
    try:
        result = factory(**kwargs)
    except ToolConfigError:
        raise
    except TypeError as e:
        # Bad/missing kwarg — always a hard config error.
        raise ToolConfigError(f"tool #{index} (type={type_}): {e}") from e
    except Exception as e:
        # Factory-time runtime failure (network/live discovery). Only I/O types
        # reach here; pure-data factories don't fail for connectivity reasons.
        _strict_raw = os.environ.get("APX_TOOLS_STRICT")
        strict = bool(_strict_raw and _strict_raw.strip().lower() in ("1", "true", "yes"))
        if strict:
            raise ToolConfigError(f"tool #{index} (type={type_}): {e}") from e
        logger.warning(
            "Skipping tool #%d (type=%s): factory failed at load time: %s",
            index,
            type_,
            e,
        )
        return []
    tools = result if isinstance(result, list) else [result]
    for fn in tools:
        attach_scope(fn, scope)
    return tools


def _find_pyproject_upward(start: Path) -> Path | None:
    """Nearest pyproject.toml walking up from *start*; None if absent.

    Intentionally cwd-only — unlike ``_load_agent_config`` (which tries
    ``__main__.__file__`` first), tools discovery must NOT consult ``__main__``:
    under pytest/notebooks it resolves to the interpreter/repo, not the user's
    project, which would surface the wrong pyproject (and break chdir tests).
    """
    for directory in [start, *start.parents]:
        candidate = directory / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None


def reject_legacy_tool_config(pyproject_path: str | None = None, *, allow_agent_table: bool = False) -> None:
    """Reject removed TOML declarations ([[tool.apx.tools]], [tool.apx.agent]) before any factory can run."""
    import tomllib

    explicit = pyproject_path or os.environ.get("APX_PYPROJECT")
    path = Path(explicit) if explicit else _find_pyproject_upward(Path.cwd())
    if path is None or not path.exists():
        return
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ToolConfigError(f"Cannot parse {path}: {exc}") from exc
    apx = data.get("tool", {}).get("apx", {})
    if "tools" in apx:
        raise ToolConfigError(
            f"{path}: [[tool.apx.tools]] is no longer supported. "
            "Declare tools with Python factories in tools/ and import them in agent.py; "
            "pass them to the agent's tools (or extra_tools) argument, then remove "
            "the TOML tool declarations."
        )
    if "agent" in apx and not allow_agent_table:
        raise ToolConfigError(
            f"{path}: [tool.apx.agent] is no longer supported. Run `apx-agent agents migrate-config` "
            f"in {path.parent} to move its settings onto the agent in agent.py and into "
            "[tool.apx.deploy] / [tool.apx], then commit the result."
        )


def load_config_tools(raw_tables: list[dict[str, Any]]) -> list[Callable[..., Any]]:
    """Build the flat list of tool callables from [[tool.apx.tools]] tables."""
    from ._tool_scope import validate_tool_scope

    registry = _registry()
    out: list[Callable[..., Any]] = []
    for i, table in enumerate(raw_tables):
        built = _build_one(i, table, registry)
        for fn in built:
            validate_tool_scope(fn)
        out.extend(built)
    # Config-vs-config name collision: two tables yielding the same __name__ is
    # an authoring bug (would break the LLM tool schema) — fail loud.
    seen: set[str] = set()
    for fn in out:
        nm = getattr(fn, "__name__", None)
        if nm in seen:
            raise ToolConfigError(
                f"duplicate tool name {nm!r} from [[tool.apx.tools]]; "
                f"set an explicit 'name' on one of them."
            )
        if nm:
            seen.add(nm)
    return out
