"""Resource declaration — auto-derive Mosaic AI resources from an agent tree.

When an apx-agent ``BaseAgent`` is compiled to an MLflow ``ChatAgent`` and
logged to Unity Catalog, the platform requires a declared ``resources=[...]``
list so it can mint a scoped token at serving time. The agent can only access
the resources it declared; everything outside the list is denied. This is the
governance backbone of the Model Serving deployment story.

This module derives that list automatically from the agent's own structure:

  * Each platform tool factory (``genie_tool``, ``uc_function_tool``, etc.)
    attaches its resource requirement to the returned callable as the
    ``_apx_resources`` attribute — a list of ``ResourceSpec`` tuples.
  * ``collect_resource_specs(agent, model=...)`` walks the agent tree
    (LlmAgent, SequentialAgent, ParallelAgent, LoopAgent, RouterAgent,
    HandoffAgent), gathers every tool's specs, adds the LLM serving endpoint,
    and adds an endpoint reference for each sub-agent.
  * ``mlflow_resources_for(agent, model=...)`` materializes the specs into
    concrete ``mlflow.models.resources.DatabricksResource`` instances ready to
    pass to ``mlflow.pyfunc.log_model(resources=...)``.

The spec layer is mlflow-free so the package imports cleanly without the
``eval`` extra. Only ``mlflow_resources_for`` and ``log_agent`` require mlflow.

Custom tools can opt in by setting ``my_tool._apx_resources`` to a list of
``ResourceSpec`` instances at definition time.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from ._agents import BaseAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spec type
# ---------------------------------------------------------------------------


_VALID_KINDS = frozenset({
    "uc_function",
    "genie_space",
    "serving_endpoint",
    "sql_warehouse",
    "vector_search_index",
    "uc_table",
})


@dataclass(frozen=True)
class ResourceSpec:
    """Lightweight, mlflow-free description of a Databricks resource.

    Materialized into ``mlflow.models.resources.Databricks*`` at log time via
    ``mlflow_resources_for``. Stored on tool callables as ``_apx_resources``.

    Args:
        kind: One of ``"uc_function"``, ``"genie_space"``,
            ``"serving_endpoint"``, ``"sql_warehouse"``,
            ``"vector_search_index"``, ``"uc_table"``.
        identifier: The natural identifier for the kind — function name,
            space ID, endpoint name, warehouse ID, index name, table name.
    """

    kind: str
    identifier: str

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"Unknown resource kind {self.kind!r}. "
                f"Valid kinds: {sorted(_VALID_KINDS)}"
            )
        if not self.identifier:
            raise ValueError(f"ResourceSpec({self.kind!r}) requires a non-empty identifier")


# ---------------------------------------------------------------------------
# Tool-side annotation helpers
# ---------------------------------------------------------------------------


def attach_resources(fn: Any, specs: Iterable[ResourceSpec]) -> Any:
    """Annotate ``fn`` with a ``_apx_resources`` list. Returns ``fn`` for chaining.

    Tool factories call this so the resource walker can find what each tool
    needs. Users with custom tools can call it too::

        def my_tool(...): ...
        attach_resources(my_tool, [ResourceSpec("uc_table", "main.sales.orders")])
    """
    existing = list(getattr(fn, "_apx_resources", []) or [])
    new = list(specs)
    fn._apx_resources = existing + new  # type: ignore[attr-defined]
    return fn


def get_resources(fn: Any) -> list[ResourceSpec]:
    """Return the ``_apx_resources`` list on ``fn``, or ``[]`` if unset."""
    specs = getattr(fn, "_apx_resources", None)
    if not specs:
        return []
    return [s for s in specs if isinstance(s, ResourceSpec)]


# ---------------------------------------------------------------------------
# Sub-agent URL → endpoint name
# ---------------------------------------------------------------------------


def _sub_agent_to_endpoint(raw: str) -> ResourceSpec | None:
    """Translate a sub_agents entry to a serving_endpoint ResourceSpec.

    Accepted forms (after ``$VAR`` expansion):

      * ``endpoints/<name>``                       → serving_endpoint(<name>)
      * ``serving-endpoints/<name>``               → serving_endpoint(<name>)
      * ``<name>`` (bare, no scheme, no path)      → serving_endpoint(<name>)
      * ``https://<host>/serving-endpoints/<name>/invocations`` → serving_endpoint(<name>)
      * ``https://<app>.databricksapps.com/...``   → None (Apps URL — declared
                                                     via app-to-app permissions
                                                     instead, not via MLflow
                                                     resources)
    """
    # Expand $VAR / ${VAR}
    if raw.startswith("$"):
        var_name = raw.lstrip("$").strip("{}")
        expanded = os.environ.get(var_name, "")
        if not expanded:
            logger.warning("sub_agent env var %s not set — skipping", var_name)
            return None
        raw = expanded

    if not raw:
        return None

    # Apps URLs are not Model Serving endpoints — return None and let the
    # Apps deployment path handle them via CAN_USE permissions.
    if "databricksapps.com" in raw:
        return None

    # Strip scheme/host if present
    name = raw
    if "://" in name:
        _, _, rest = name.partition("://")
        # Take everything after the host
        if "/" in rest:
            _, _, name = rest.partition("/")
        else:
            name = ""

    if not name:
        return None

    # Normalise known prefixes and tail
    for prefix in ("serving-endpoints/", "endpoints/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break

    # Trim trailing path components (e.g. "/invocations")
    if "/" in name:
        name = name.split("/", 1)[0]

    name = name.strip()
    if not name:
        return None

    return ResourceSpec("serving_endpoint", name)


# ---------------------------------------------------------------------------
# Tree walker
# ---------------------------------------------------------------------------


def _iter_tool_fns(agent: "BaseAgent") -> Iterable[Any]:
    """Yield the raw tool callables registered anywhere in the agent tree.

    Encapsulates the lookup against each agent type's internal state so the
    rest of this module stays agnostic.
    """
    from ._agents import (
        HandoffAgent,
        LlmAgent,
        LoopAgent,
        ParallelAgent,
        RouterAgent,
        SequentialAgent,
    )

    if isinstance(agent, LlmAgent):
        for fn in agent._tool_fns:
            yield fn
        return

    if isinstance(agent, LoopAgent):
        yield from _iter_tool_fns(agent._inner)
        return

    if isinstance(agent, (SequentialAgent, ParallelAgent)):
        for sub in agent._agents:
            yield from _iter_tool_fns(sub)
        return

    if isinstance(agent, (RouterAgent, HandoffAgent)):
        # Both keep a dict/list of branch agents
        children = (
            getattr(agent, "_agents", None)
            or getattr(agent, "_routes", None)
            or {}
        )
        if isinstance(children, dict):
            children = list(children.values())
        for sub in children:
            yield from _iter_tool_fns(sub)
        return

    # Unknown agent — fall back to collect_tools() if it carries raw fns,
    # otherwise emit nothing. Custom agents can override this by exposing
    # ``_tool_fns``.
    raw = getattr(agent, "_tool_fns", None)
    if raw:
        for fn in raw:
            yield fn


def _iter_sub_agents(agent: "BaseAgent") -> Iterable[str]:
    """Yield raw sub_agent URL strings registered anywhere in the tree."""
    from ._agents import (
        HandoffAgent,
        LlmAgent,
        LoopAgent,
        ParallelAgent,
        RouterAgent,
        SequentialAgent,
    )

    if isinstance(agent, LlmAgent):
        for u in agent._sub_agent_urls:
            yield u
        return

    if isinstance(agent, LoopAgent):
        yield from _iter_sub_agents(agent._inner)
        return

    if isinstance(agent, (SequentialAgent, ParallelAgent)):
        for sub in agent._agents:
            yield from _iter_sub_agents(sub)
        return

    if isinstance(agent, (RouterAgent, HandoffAgent)):
        children = (
            getattr(agent, "_agents", None)
            or getattr(agent, "_routes", None)
            or {}
        )
        if isinstance(children, dict):
            children = list(children.values())
        for sub in children:
            yield from _iter_sub_agents(sub)
        return


def collect_resource_specs(
    agent: "BaseAgent",
    *,
    model: str | None = None,
    extra: Iterable[ResourceSpec] | None = None,
) -> list[ResourceSpec]:
    """Walk the agent tree and return a deduplicated resource spec list.

    Includes:

      * Every tool's ``_apx_resources`` annotations.
      * ``ResourceSpec("serving_endpoint", model)`` if ``model`` is given —
        the LLM endpoint the compiled graph calls.
      * One ``ResourceSpec("serving_endpoint", ...)`` per sub_agent URL that
        resolves to a Model Serving endpoint reference. Apps URLs are skipped
        (they're declared via app-to-app permissions, not MLflow resources).
      * Any ``extra`` specs passed by the caller (escape hatch for warehouses,
        UC tables, vector indices, etc. that aren't auto-inferrable).

    Order is preserved for the first occurrence of each (kind, identifier)
    pair; duplicates are dropped.
    """
    seen: set[tuple[str, str]] = set()
    out: list[ResourceSpec] = []

    def _add(spec: ResourceSpec) -> None:
        key = (spec.kind, spec.identifier)
        if key in seen:
            return
        seen.add(key)
        out.append(spec)

    if model:
        _add(ResourceSpec("serving_endpoint", model))

    for fn in _iter_tool_fns(agent):
        for spec in get_resources(fn):
            _add(spec)

    for sub_url in _iter_sub_agents(agent):
        ep = _sub_agent_to_endpoint(sub_url)
        if ep is not None:
            _add(ep)

    for spec in extra or []:
        _add(spec)

    return out


# ---------------------------------------------------------------------------
# MLflow materialisation
# ---------------------------------------------------------------------------


def mlflow_resources_for(
    agent: "BaseAgent",
    *,
    model: str | None = None,
    extra: Iterable[ResourceSpec] | None = None,
) -> list[Any]:
    """Return the MLflow ``DatabricksResource`` list ready for log_model.

    Equivalent to ``collect_resource_specs`` followed by mapping each spec to
    its corresponding ``mlflow.models.resources.Databricks*`` class.

    Requires the ``eval`` extra (``pip install 'apx-agent[eval]'``) for the
    ``mlflow`` import. Raises ``ImportError`` with a friendly message if
    mlflow is not available.
    """
    try:
        from mlflow.models.resources import (
            DatabricksFunction,
            DatabricksGenieSpace,
            DatabricksServingEndpoint,
            DatabricksSQLWarehouse,
            DatabricksTable,
            DatabricksVectorSearchIndex,
        )
    except ImportError as e:  # pragma: no cover — exercised only without mlflow
        raise ImportError(
            "mlflow is required to materialise resources. "
            "Install with: pip install 'apx-agent[eval]'"
        ) from e

    kind_to_cls: dict[str, Any] = {
        "uc_function": lambda ident: DatabricksFunction(function_name=ident),
        "genie_space": lambda ident: DatabricksGenieSpace(genie_space_id=ident),
        "serving_endpoint": lambda ident: DatabricksServingEndpoint(endpoint_name=ident),
        "sql_warehouse": lambda ident: DatabricksSQLWarehouse(warehouse_id=ident),
        "vector_search_index": lambda ident: DatabricksVectorSearchIndex(index_name=ident),
        "uc_table": lambda ident: DatabricksTable(table_name=ident),
    }

    specs = collect_resource_specs(agent, model=model, extra=extra)
    return [kind_to_cls[s.kind](s.identifier) for s in specs]
