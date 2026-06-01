"""Declarative resource tools — load [[tool.apx.tools]] into callables.

Each table is ``{type, <factory kwargs>}``. ``type`` selects a factory from the
registry; the remaining keys are splatted as keyword args (every factory takes
its identifier as positional-or-keyword, so all-keyword calls work uniformly).
Toolkit factories return lists, which are flattened. The factories are the
validation surface — a bad/missing kwarg surfaces as a wrapped ToolConfigError.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

# Reserved for later tasks: T2 (skip-with-warning) and T4 (merge_config_tools).
logger = logging.getLogger(__name__)


class ToolConfigError(ValueError):
    """A [[tool.apx.tools]] table could not be turned into a tool."""


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
    from .vector_search import vector_search_tool

    return {
        "genie": genie_tool,
        "genie_query": genie_query_tool,
        "vector_search": vector_search_tool,
        "uc_function": uc_function_tool,
        "uc_function_toolkit": uc_function_toolkit,
        "catalog": catalog_tool,
        "schema": schema_tool,
        "lineage": lineage_tool,
        "sql": sql_tool,
        "http": http_tool,
        "openapi": openapi_tool,
        "mcp_tool": mcp_tool,
        "mcp_toolkit": mcp_toolkit,
        "foundation_model": foundation_model_tool,
        "jobs": jobs_tools,
        "jobs_for_table": jobs_for_table_tool,
        "jobs_history": jobs_history_tool,
        "jobs_logs": jobs_logs_tool,
        "jobs_source_paths": jobs_source_paths_tool,
    }


def _build_one(
    index: int, table: dict[str, Any], registry: dict[str, Callable[..., Any]]
) -> list[Callable[..., Any]]:
    kwargs = dict(table)
    type_ = kwargs.pop("type", None)
    if type_ is None:
        raise ToolConfigError(f"tool #{index}: missing 'type' key.")
    factory = registry.get(type_)
    if factory is None:
        raise ToolConfigError(
            f"tool #{index}: unknown type {type_!r}; known: {sorted(registry)}."
        )
    try:
        result = factory(**kwargs)
    except ToolConfigError:
        raise
    except TypeError as e:
        raise ToolConfigError(f"tool #{index} (type={type_}): {e}") from e
    return result if isinstance(result, list) else [result]


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
