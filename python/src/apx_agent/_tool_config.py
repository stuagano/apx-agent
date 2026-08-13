"""Declarative resource tools — load [[tool.apx.tools]] into callables.

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
    """A [[tool.apx.tools]] table could not be turned into a tool."""


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


def _check_allowlist(index: int, type_: str, kwargs: dict[str, Any]) -> None:
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
        raise ToolConfigError(
            f"tool #{index} (type={type_}): host {host!r} is not in "
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
    kwargs: dict[str, Any] = _resolve_env_deep(dict(table))
    type_ = kwargs.pop("type", None)
    if type_ is None:
        raise ToolConfigError(f"tool #{index}: missing 'type' key.")
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
    return result if isinstance(result, list) else [result]


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


def _read_tools_section(pyproject_path: str | None) -> list[dict[str, Any]]:
    if pyproject_path:
        path: Path | None = Path(pyproject_path)
    else:
        path = _find_pyproject_upward(Path.cwd())
    if path is None:
        return []
    if not path.exists():
        return []
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py<3.11
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        data = tomllib.loads(path.read_text())
    except Exception as e:
        logger.warning(
            "Failed to parse %s — [[tool.apx.tools]] will be empty: %s",
            path,
            e,
        )
        return []
    tables = (((data.get("tool") or {}).get("apx") or {}).get("tools")) or []
    return tables if isinstance(tables, list) else []


def merge_config_tools(agent: Any, pyproject_path: str | None = None) -> None:
    """Load [[tool.apx.tools]] and append the callables to the agent.

    Dedup by __name__ (code-wired tools win — config is additive).
    An idempotency sentinel (``_apx_config_tools_merged``) ensures the I/O
    factories inside ``load_config_tools`` are only invoked once per agent
    instance, even when ``finalize_agent`` is called repeatedly.
    Composition roots without ``_register_tool`` are warned + skipped.

    Single-project assumption: once the sentinel is set, a later call with a
    *different* ``pyproject_path`` on the same agent is silently skipped. This
    is fine today (an Agent instance is bound to one project context); revisit
    if agents are ever pooled/reconfigured across projects.
    """
    # 1. Sentinel check — skip everything (including I/O factories) on repeat calls.
    if getattr(agent, "_apx_config_tools_merged", False):
        return

    # 2. Short-circuit when there are no tables to process (don't set sentinel —
    #    a later call with a valid pyproject should still be allowed to merge).
    tables = _read_tools_section(pyproject_path)
    if not tables:
        return

    # 3. Composition-root guard.
    register = getattr(agent, "_register_tool", None)
    existing = {getattr(fn, "__name__", None) for fn in getattr(agent, "_tool_fns", [])}
    if register is None:
        logger.warning(
            "[[tool.apx.tools]] declared but %s is a composition root with no "
            "_tool_fns to attach them to — skipping. Put tools on a leaf LlmAgent.",
            type(agent).__name__,
        )
        return

    # 4. Build tools and register (skipping name-collisions).
    for fn in load_config_tools(tables):
        nm = getattr(fn, "__name__", None)
        if nm in existing:
            logger.warning(
                "[[tool.apx.tools]] declares %r but the agent already wires a "
                "tool with that name — keeping the existing one, ignoring config.",
                nm,
            )
            continue
        register(fn)
        if nm:
            existing.add(nm)

    # 5. Mark as merged so repeat calls skip the I/O factories entirely.
    setattr(agent, "_apx_config_tools_merged", True)


def load_config_tools(raw_tables: list[dict[str, Any]]) -> list[Callable[..., Any]]:
    """Build the flat list of tool callables from [[tool.apx.tools]] tables."""
    registry = _registry()
    out: list[Callable[..., Any]] = []
    for i, table in enumerate(raw_tables):
        out.extend(_build_one(i, table, registry))
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
