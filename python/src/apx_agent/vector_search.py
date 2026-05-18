"""vector_search_tool — wrap a Databricks Vector Search index as a tool.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly at function definition time
and ``get_type_hints()`` in ``_inspection.py`` sees the real ``Annotated[...]``
type, not a string.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def vector_search_tool(
    index_name: str,
    *,
    columns: list[str] | None = None,
    num_results: int = 5,
    filters_json: str | None = None,
    name: str = "vector_search",
    description: str | None = None,
) -> Any:
    """Return a tool function that queries a Databricks Vector Search index.

    The LLM sees one parameter — ``query: str`` — and the index is fixed at
    factory time. The workspace client is injected automatically and runs as
    the calling user, so UC grants on the index and underlying tables apply.

    Usage::

        from apx_agent import Agent, vector_search_tool

        agent = Agent(tools=[
            vector_search_tool(
                index_name="main.search.docs_index",
                columns=["doc_id", "title", "content"],
                num_results=5,
            ),
        ])

    Args:
        index_name: Fully-qualified Vector Search index name
            (``catalog.schema.index``).
        columns: Columns to return in each hit. When omitted, returns every
            column on the index (which may be expensive — set this for
            production tools).
        num_results: Maximum number of matches to return. Default 5.
        filters_json: Optional JSON-encoded filter expression passed through
            to the index. Fixed at factory time; for per-call filters,
            expose a custom tool that calls the SDK directly.
        name: Tool name shown to the LLM. Defaults to ``"vector_search"``.
        description: Tool description shown to the LLM. When omitted,
            generated from the index name.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec, attach_resources

    _desc = description or (
        f"Search the Vector Search index `{index_name}` for documents matching "
        f"a natural-language query. Returns up to {num_results} results."
    )

    async def _vector_search(query: str, ws: UserClientDependency) -> list[dict[str, Any]]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        try:
            result = ws.vector_search_indexes.query_index(
                index_name=index_name,
                query_text=query,
                columns=columns,
                num_results=num_results,
                filters_json=filters_json,
            )
        except Exception as e:
            logger.warning("Vector Search query failed on %s: %s", index_name, e)
            return []

        # The response shape: result.result.data_array is a list of rows;
        # result.manifest.columns gives the column names. Match them up.
        manifest = getattr(result, "manifest", None)
        rows_holder = getattr(result, "result", None)
        if manifest is None or rows_holder is None:
            return []

        column_names = [c.name for c in (manifest.columns or [])]
        data = rows_holder.data_array or []
        return [dict(zip(column_names, row)) for row in data]

    _vector_search.__name__ = name
    _vector_search.__qualname__ = name
    _vector_search.__doc__ = _desc

    attach_resources(_vector_search, [ResourceSpec("vector_search_index", index_name)])
    return _vector_search
