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

A tool that calls a Databricks API with no securable to point a ``ResourceSpec``
at — the UC metadata/discovery REST API is the motivating case — can instead
declare its raw OBO scope directly via ``require_user_api_scopes`` (#563);
``collect_user_api_scopes`` gathers those for the Apps deploy to union in.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from ._env import resolve_env_var

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
    "uc_connection",
    "lakebase_instance",
    "job",
    "app",
})


@dataclass(frozen=True)
class ResourceSpec:
    """Lightweight, mlflow-free description of a Databricks resource.

    Materialized into ``mlflow.models.resources.Databricks*`` at log time via
    ``mlflow_resources_for``. Stored on tool callables as ``_apx_resources``.

    Args:
        kind: One of ``"uc_function"``, ``"genie_space"``,
            ``"serving_endpoint"``, ``"sql_warehouse"``,
            ``"vector_search_index"``, ``"uc_table"``, ``"uc_connection"``,
            ``"lakebase_instance"``, ``"job"``, ``"app"``.
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
# Tool-side raw OBO-scope declaration
# ---------------------------------------------------------------------------


# The raw Databricks OBO scopes a tool may declare directly. Kept as a closed
# set so a typo becomes a deploy-time error instead of another prod-only
# "missing scopes" 500 (#563).
_KNOWN_USER_API_SCOPES = frozenset({
    "sql",
    "dashboards.genie",
    "serving.serving-endpoints",
    "vectorsearch.vector-search-endpoints",
    "catalog.catalogs:read",
    "catalog.connections:read",
    "catalog.schemas:read",
    "catalog.tables:read",
})


def require_user_api_scopes(fn: Any, scopes: Iterable[str]) -> Any:
    """Declare raw OBO ``user_api_scopes`` a tool needs. Returns ``fn``.

    Most scopes are *derived* from a tool's ``ResourceSpec``s (a Genie space
    implies ``dashboards.genie``; a serving endpoint implies
    ``serving.serving-endpoints`` — see :func:`user_api_scopes_for`). Use this
    only for a tool that calls a Databricks API with **no securable** to point a
    ``ResourceSpec`` at. The UC metadata/discovery REST API is the motivating
    case: ``ws.catalogs.list()`` / ``ws.schemas.list()`` / ``ws.tables.get()``
    need an SDK catalog scope but name no specific securable, so the scope can't
    be inferred from a resource (#563)::

        def list_catalogs(ws): ...
        require_user_api_scopes(list_catalogs, ["catalog.catalogs:read"])

    ``apx-agent deploy`` unions these declared scopes onto the resource-derived
    baseline in ``databricks.yml`` — turning a runtime "does not have required
    scopes" 500 into a scope declared at deploy time. Raises ``ValueError`` on
    an unknown scope string so a typo fails fast rather than silently.
    """
    cleaned = [s for s in scopes if s]
    unknown = [s for s in cleaned if s not in _KNOWN_USER_API_SCOPES]
    if unknown:
        raise ValueError(
            f"Unknown user_api_scope(s) {unknown}. "
            f"Known scopes: {sorted(_KNOWN_USER_API_SCOPES)}"
        )
    merged = list(getattr(fn, "_apx_user_api_scopes", []) or [])
    for s in cleaned:
        if s not in merged:
            merged.append(s)
    fn._apx_user_api_scopes = merged  # type: ignore[attr-defined]
    return fn


def get_user_api_scopes(fn: Any) -> list[str]:
    """Return the raw OBO scopes declared on ``fn`` via ``require_user_api_scopes``."""
    scopes = getattr(fn, "_apx_user_api_scopes", None)
    if not scopes:
        return []
    return [s for s in scopes if isinstance(s, str) and s]


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
        expanded = resolve_env_var(raw)
        if not expanded:
            logger.warning("sub_agent env var %s not set — skipping", raw)
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

    Descends into every composition type via ``_iter_child_agents`` (the same
    canonical child-walk the topology/discovery card uses), so router/composite
    leaves — including ``KeywordRouter`` branches and ``RouterAgent`` routes —
    are reached, not just the direct children a hand-rolled walk happened to
    enumerate.
    """
    from ._agents import LlmAgent
    from ._topology import _iter_child_agents

    if isinstance(agent, LlmAgent):
        for fn in agent._tool_fns:
            yield fn
        return

    children = _iter_child_agents(agent)
    if children:
        for _name, sub in children:
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
    """Yield raw sub_agent URL strings registered anywhere in the tree.

    Like :func:`_iter_tool_fns`, descends through ``_iter_child_agents`` so
    sub-agents declared on router/composite leaves (``KeywordRouter`` branches,
    ``RouterAgent`` routes) are surfaced — not only those on the root.
    """
    from ._agents import LlmAgent
    from ._topology import _iter_child_agents

    if isinstance(agent, LlmAgent):
        for u in agent._sub_agent_urls:
            yield u
        return

    for _name, sub in _iter_child_agents(agent):
        yield from _iter_sub_agents(sub)


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


# ---------------------------------------------------------------------------
# Databricks Apps bundle YAML materialisation
# ---------------------------------------------------------------------------


_YML_NAME_SAFE = "abcdefghijklmnopqrstuvwxyz0123456789-_"

# Disambiguator suffix appended to the slug so cross-kind name collisions
# don't happen (e.g. a uc_function named ``main.tools.bar`` and a warehouse
# whose only memorable substring is also ``bar``).
_DAB_KIND_SUFFIX: dict[str, str] = {
    "serving_endpoint": "endpoint",
    "sql_warehouse": "warehouse",
    "genie_space": "genie",
    "uc_function": "function",
    "vector_search_index": "vsi",
    "uc_table": "table",
    "uc_connection": "connection",
    "lakebase_instance": "lakebase",
    "job": "job",
    "app": "app",
}


def _slugify(identifier: str, kind: str) -> str:
    """Produce a databricks.yml-safe ``name`` slug for a resource entry.

    The bundle YAML uses the ``name`` field as the local handle for the
    resource (and as the key for app-scoped OBO grants). It must be unique
    within a single app's resource block, so we suffix the identifier-slug
    with a per-kind disambiguator.
    """
    ident = identifier or ""
    # For UC names, drop the catalog/schema prefix so the slug stays compact.
    short = ident.rsplit(".", 1)[-1] if "." in ident else ident
    lowered = short.lower()
    out: list[str] = []
    for ch in lowered:
        if ch in _YML_NAME_SAFE:
            out.append(ch)
        elif ch in "./: ":
            out.append("-")
        # else drop
    slug = "".join(out).strip("-_") or "resource"
    while "--" in slug:
        slug = slug.replace("--", "-")
    suffix = _DAB_KIND_SUFFIX.get(kind, kind)
    digest = hashlib.sha256(f"{kind}:{identifier}".encode()).hexdigest()[:8]
    return f"{slug}-{suffix}-{digest}"


def _spec_to_yml_entry(spec: "ResourceSpec") -> dict[str, Any] | None:
    """Project a single ``ResourceSpec`` onto its databricks.yml resource shape.

    Each entry is a one-key dict whose value carries the typed body — name,
    identifier field, and the OBO permission the App needs at runtime. The
    shapes mirror the resource block schema used in the
    ``databricks/app-templates`` bundles.
    """
    name = _slugify(spec.identifier, spec.kind)

    if spec.kind == "serving_endpoint":
        return {
            "serving_endpoint": {
                "name": name,
                "endpoint_name": spec.identifier,
                "permission": "CAN_QUERY",
            }
        }

    if spec.kind == "uc_function":
        return {
            "uc_securable": {
                "name": name,
                "securable_full_name": spec.identifier,
                "securable_type": "FUNCTION",
                "permission": "EXECUTE",
            }
        }

    if spec.kind == "genie_space":
        return {
            "genie_space": {
                "name": name,
                "space_id": spec.identifier,
                "permission": "CAN_RUN",
            }
        }

    if spec.kind == "vector_search_index":
        return {
            "uc_securable": {
                "name": name,
                "securable_full_name": spec.identifier,
                "securable_type": "TABLE",
                "permission": "SELECT",
            }
        }

    if spec.kind == "sql_warehouse":
        return {
            "sql_warehouse": {
                "name": name,
                "id": spec.identifier,
                "permission": "CAN_USE",
            }
        }

    if spec.kind == "job":
        return {
            "job": {
                "name": name,
                "id": spec.identifier,
                "permission": "CAN_MANAGE_RUN",
            }
        }

    if spec.kind == "app":
        return {
            "app": {
                "name": spec.identifier,
                "permission": "CAN_USE",
            }
        }

    if spec.kind == "uc_connection":
        return {
            "uc_securable": {
                "name": name,
                "securable_full_name": spec.identifier,
                "securable_type": "CONNECTION",
                "permission": "USE_CONNECTION",
            }
        }

    if spec.kind == "lakebase_instance":
        return {
            "database": {
                "name": name,
                "database_name": "databricks_postgres",
                "instance_name": spec.identifier,
                "permission": "CAN_CONNECT_AND_CREATE",
            }
        }

    if spec.kind == "uc_table":
        return {
            "uc_securable": {
                "name": name,
                "securable_full_name": spec.identifier,
                "securable_type": "TABLE",
                "permission": "SELECT",
            }
        }

    return None  # pragma: no cover — guarded by _VALID_KINDS above


def resources_to_databricks_yml(
    resources: Iterable["ResourceSpec"],
) -> list[dict[str, Any]]:
    """Project ``ResourceSpec`` list to the Databricks Apps bundle YAML shape.

    Apps deployments declare their grants in ``databricks.yml`` rather than in
    a logged pyfunc's ``resources=`` list — at deploy time, the bundle's
    ``resources`` block is what determines which Databricks objects the
    App's runtime token can access. This function is the Apps-side counterpart
    to :func:`mlflow_resources_for` (Model Serving / ChatAgent path).

    Output shape per spec kind:

      +----------------------+------------------------------------------------+
      | ``ResourceSpec.kind``| databricks.yml resource entry                  |
      +======================+================================================+
      | serving_endpoint     | ``{serving_endpoint: {name, endpoint_name,     |
      |                      | permission: CAN_QUERY}}``                      |
      | uc_function          | ``{uc_securable: {name, securable_full_name,   |
      |                      | securable_type: FUNCTION, permission: EXECUTE}}``|
      | genie_space          | ``{genie_space: {name, space_id,               |
      |                      | permission: CAN_RUN}}``                        |
      | vector_search_index  | ``{uc_securable: {..., securable_type: TABLE,  |
      |                      | permission: SELECT}}``                         |
      | sql_warehouse        | ``{sql_warehouse: {name, id,                   |
      |                      | permission: CAN_USE}}``                        |
      | uc_connection        | ``{uc_securable: {..., securable_type:         |
      |                      | CONNECTION, permission: USE_CONNECTION}}``     |
      | lakebase_instance    | ``{database: {name, database_name:             |
      |                      | databricks_postgres, instance_name,            |
      |                      | permission: CAN_CONNECT_AND_CREATE}}``         |
      | uc_table             | ``{uc_securable: {..., securable_type: TABLE,  |
      |                      | permission: SELECT}}``                         |
      | job                  | ``{job: {name, id,                             |
      |                      | permission: CAN_MANAGE_RUN}}``                 |
      | app                  | ``{app: {name, permission: CAN_USE}}``         |
      +----------------------+------------------------------------------------+

    Each entry's ``name`` field is auto-derived from the resource identifier
    via a readable slug plus a short hash of the complete kind and identifier,
    except for ``app`` where the CLI schema uses ``name`` as the peer's natural
    identifier. Callers that need a stable name across edits can post-process
    the returned list before merging it into the bundle.

    Args:
        resources: Specs to project. Typically the output of
            :func:`collect_resource_specs`.

    Returns:
        List of dicts, each shaped as ``{"<resource_type>": {name, ...}}``,
        ready to embed under ``resources:`` in a Databricks Apps bundle.
    """
    out: list[dict[str, Any]] = []
    for spec in resources:
        entry = _spec_to_yml_entry(spec)
        if entry is not None:
            out.append(entry)
    return out


# Map a ResourceSpec kind → the Databricks Apps OAuth scope the forwarded user
# (OBO) token needs to use it. The iam.* defaults are always granted and are
# never listed. These names track the current Apps authorization scopes.
_KIND_TO_SCOPE: dict[str, str] = {
    "sql_warehouse": "sql",
    "uc_table": "sql",
    "uc_function": "sql",
    "uc_connection": "sql",
    "serving_endpoint": "serving.serving-endpoints",
    "genie_space": "dashboards.genie",
    "vector_search_index": "vectorsearch.vector-search-endpoints",
}


def user_api_scopes_for(resources: Iterable["ResourceSpec"]) -> list[str]:
    """Derive the OBO ``user_api_scopes`` an Apps deploy needs from its resources.

    e.g. a Genie space → ``dashboards.genie``; a serving endpoint →
    ``serving.serving-endpoints``. Returned sorted + de-duplicated. Note: a
    ``sql_tool`` that auto-discovers its warehouse declares no SQL resource —
    that path uses :func:`require_user_api_scopes` for ``sql`` instead, and
    deploy unions both sources onto the scaffold baseline.
    """
    scopes = {
        _KIND_TO_SCOPE[s.kind] for s in resources if s.kind in _KIND_TO_SCOPE
    }
    return sorted(scopes)


def collect_user_api_scopes(agent: "BaseAgent") -> list[str]:
    """Raw OBO scopes declared by tools anywhere in the agent tree.

    Walks the same tool set as :func:`collect_resource_specs` (so router /
    composite leaves are included) and unions every tool's
    :func:`require_user_api_scopes` declaration. Sorted + de-duplicated. These
    are scopes with no backing ``ResourceSpec`` — e.g. ``catalog.tables:read``
    for UC metadata/discovery calls — so :func:`user_api_scopes_for` can't
    derive them;
    the deploy path unions this onto the derived baseline (#563).
    """
    scopes: set[str] = set()
    for fn in _iter_tool_fns(agent):
        scopes.update(get_user_api_scopes(fn))
    return sorted(scopes)
