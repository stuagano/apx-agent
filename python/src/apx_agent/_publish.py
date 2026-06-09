"""publish_to_supervisor — register an apx-agent as a Mosaic AI Supervisor sub-agent.

Closes the multi-agent topology loop: once an apx-agent is deployed as a
Model Serving endpoint via ``log_agent`` + ``databricks.agents.deploy``,
this module registers it as a tool on an existing Supervisor Agent so the
supervisor can route to it alongside Knowledge Assistants, Genie spaces,
and other sub-agents.

The Mosaic AI Supervisor Agent SDK lives at
``databricks.sdk.service.supervisoragents`` and is currently in preview.
This module imports it lazily and raises a friendly error if the
installed Databricks SDK version doesn't include it yet — bumping the SDK
is opt-in.

Two entry points:

  * ``create_supervisor_agent(...)`` — create a new Supervisor Agent.
    Wraps ``WorkspaceClient.supervisor_agents.create_supervisor_agent``
    with positional-arg-style ergonomics.

  * ``publish_to_supervisor(...)`` — add a serving-endpoint-backed
    sub-agent (apx-agent or any other Mosaic AI agent endpoint) to an
    existing Supervisor Agent.

The Supervisor SDK surface is preview; expect field names and tool-type
literals to settle over the next few releases. This helper is structured
so the dataclass-shape is at one place and easy to update.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


_SUPERVISOR_SDK_MISSING = (
    "publish_to_supervisor / create_supervisor_agent require the "
    "supervisoragents service from databricks-sdk. The installed SDK "
    "doesn't include it (likely too old). Upgrade with:\n"
    "    pip install --upgrade databricks-sdk\n"
    "If the supervisor surface is still preview in your workspace, this "
    "feature won't work until it lands."
)


def _load_supervisor_sdk() -> Any:
    """Return the supervisoragents service module, or raise a friendly ImportError."""
    try:
        from databricks.sdk.service import supervisoragents
    except ImportError as e:  # pragma: no cover — exercised only on older SDKs
        raise ImportError(_SUPERVISOR_SDK_MISSING) from e
    return supervisoragents


def _ensure_ws(ws: "WorkspaceClient | None") -> "WorkspaceClient":
    if ws is not None:
        return ws
    from databricks.sdk import WorkspaceClient as _WS
    return _WS()


def _slug(text: str) -> str:
    """Normalise a string into a safe tool_id (letters / digits / underscores)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()
    return cleaned or "tool"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_supervisor_agent(
    *,
    display_name: str,
    description: str,
    instructions: str,
    ws: "WorkspaceClient | None" = None,
) -> Any:
    """Create a Mosaic AI Supervisor Agent.

    Args:
        display_name: Human-readable name shown in the Databricks UI.
        description: Short description for the supervisor's metadata.
        instructions: System prompt for the supervisor's routing behavior.
        ws: Optional ``WorkspaceClient``. Default-constructed when omitted.

    Returns:
        The created supervisor agent record (SDK response object).
    """
    sdk = _load_supervisor_sdk()
    SupervisorAgent = sdk.SupervisorAgent  # noqa: N806

    ws = _ensure_ws(ws)
    return ws.supervisor_agents.create_supervisor_agent(
        supervisor_agent=SupervisorAgent(
            display_name=display_name,
            description=description,
            instructions=instructions,
        )
    )


def publish_to_supervisor(
    *,
    supervisor_agent_id: str,
    serving_endpoint: str,
    description: str,
    display_name: str | None = None,
    tool_id: str | None = None,
    ws: "WorkspaceClient | None" = None,
    extra_tool_kwargs: dict[str, Any] | None = None,
) -> Any:
    """Register a serving-endpoint-backed agent as a Supervisor sub-agent.

    This is how a deployed apx-agent becomes routable from a Supervisor.
    The supervisor's LLM picks among its declared sub-agents at runtime;
    when it picks the one you publish here, the call flows to the serving
    endpoint with identity passthrough scoped to the endpoint's declared
    resources.

    Args:
        supervisor_agent_id: ID of the existing Supervisor Agent to attach
            the sub-agent to. Obtainable from
            ``ws.supervisor_agents.list_supervisor_agents()``.
        serving_endpoint: Name of the Model Serving endpoint that hosts
            the sub-agent (e.g. ``"data-triage"`` if you deployed via
            ``databricks.agents.deploy("main.agents.data_triage", ...)``).
        description: How the supervisor's LLM should think about when to
            route to this sub-agent. Treat this like a tool description —
            it's what drives routing accuracy.
        display_name: Human-readable name shown in the Databricks UI.
            Defaults to ``serving_endpoint``.
        tool_id: Stable identifier for this sub-agent within the
            supervisor. Defaults to a slug derived from
            ``serving_endpoint``. Use a stable value if you want
            idempotent updates on re-publish.
        ws: Optional ``WorkspaceClient``. Default-constructed when omitted.
        extra_tool_kwargs: Escape hatch for additional fields on the
            ``Tool`` dataclass that this helper doesn't expose explicitly.
            The Supervisor SDK is in preview — use this if the field
            surface evolves before the helper is updated.

    Returns:
        The created Tool record (SDK response object).
    """
    sdk = _load_supervisor_sdk()
    Tool = sdk.Tool  # noqa: N806

    ws = _ensure_ws(ws)

    name = display_name or serving_endpoint
    final_tool_id = tool_id or _slug(serving_endpoint)

    tool_kwargs: dict[str, Any] = {
        "tool_type": "serving_endpoint",
        "name": serving_endpoint,
        "description": description,
    }
    if extra_tool_kwargs:
        tool_kwargs.update(extra_tool_kwargs)

    tool = Tool(**tool_kwargs)

    logger.info(
        "Publishing serving endpoint %s as Supervisor sub-agent %s "
        "(tool_id=%s, display_name=%s)",
        serving_endpoint, supervisor_agent_id, final_tool_id, name,
    )
    return ws.supervisor_agents.create_tool(
        parent=f"supervisor-agents/{supervisor_agent_id}",
        tool=tool,
        tool_id=final_tool_id,
    )


# ---------------------------------------------------------------------------
# UC Delta agent registry
# ---------------------------------------------------------------------------

_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  agent_id      STRING  COMMENT 'Stable key: name + workspace host',
  name          STRING  COMMENT 'Serving endpoint / app name',
  display_name  STRING,
  description   STRING,
  endpoint_url  STRING,
  endpoint_type STRING  COMMENT 'apps or model-serving',
  workspace_host STRING,
  supervisor_agent_id STRING COMMENT 'Supervisor Agent ID if registered',
  published_by  STRING,
  published_at  DOUBLE,
  updated_at    DOUBLE
) USING DELTA
  COMMENT 'apx-agent org registry — all published agents in this workspace'
"""

_REGISTRY_MERGE = """
MERGE INTO {table} AS target
USING (SELECT
  {agent_id}   AS agent_id,
  {name}       AS name,
  {display_name} AS display_name,
  {description} AS description,
  {endpoint_url} AS endpoint_url,
  {endpoint_type} AS endpoint_type,
  {workspace_host} AS workspace_host,
  {supervisor_agent_id} AS supervisor_agent_id,
  {published_by} AS published_by,
  {published_at} AS published_at,
  {updated_at}  AS updated_at
) AS src ON target.agent_id = src.agent_id
WHEN MATCHED THEN UPDATE SET
  name = src.name,
  display_name = src.display_name,
  description = src.description,
  endpoint_url = src.endpoint_url,
  endpoint_type = src.endpoint_type,
  workspace_host = src.workspace_host,
  supervisor_agent_id = COALESCE(src.supervisor_agent_id, target.supervisor_agent_id),
  published_by = src.published_by,
  updated_at = src.updated_at
WHEN NOT MATCHED THEN INSERT (
  agent_id, name, display_name, description, endpoint_url, endpoint_type,
  workspace_host, supervisor_agent_id, published_by, published_at, updated_at
) VALUES (
  src.agent_id, src.name, src.display_name, src.description, src.endpoint_url,
  src.endpoint_type, src.workspace_host, src.supervisor_agent_id,
  src.published_by, src.published_at, src.updated_at
)
"""


def _q(s: str | None) -> str:
    """SQL-escape a string literal (single-quote doubling)."""
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def publish_to_registry(
    *,
    name: str,
    description: str,
    display_name: str | None = None,
    endpoint_url: str | None = None,
    endpoint_type: str = "apps",
    supervisor_agent_id: str | None = None,
    registry_table: str = "main.apx.agent_registry",
    ws: "WorkspaceClient | None" = None,
    warehouse_id: str | None = None,
) -> None:
    """Upsert an agent record into the UC Delta agent registry table.

    Creates the table (and schema) automatically on first use. Safe to
    call on every ``apx-agent publish`` — MERGE is idempotent.

    Args:
        name: Serving endpoint or app name (used as the stable key with host).
        description: What this agent does.
        display_name: Human-readable label. Defaults to ``name``.
        endpoint_url: Full URL of the deployed agent.
        endpoint_type: ``"apps"`` or ``"model-serving"``.
        supervisor_agent_id: Set when also registering with Supervisor Agent.
        registry_table: Fully-qualified UC table, e.g. ``main.apx.agent_registry``.
        ws: Optional ``WorkspaceClient``.
        warehouse_id: SQL warehouse to use; auto-discovered when omitted.
    """
    import time

    from ._sql import run_sql
    from ._memory_delta import _validate_table_name

    _validate_table_name(registry_table)
    ws = _ensure_ws(ws)

    host = getattr(getattr(ws, "config", None), "host", None) or ""
    agent_id = _slug(f"{name}_{host.replace('https://', '').split('.')[0]}")

    # Ensure the schema exists before the table.
    parts = registry_table.split(".")
    if len(parts) == 3:
        schema_fqn = f"{parts[0]}.{parts[1]}"
        try:
            run_sql(ws, f"CREATE SCHEMA IF NOT EXISTS {schema_fqn}", warehouse_id=warehouse_id)
        except Exception:
            pass  # may lack CREATE SCHEMA; table creation will surface the real error

    try:
        run_sql(ws, _REGISTRY_DDL.format(table=registry_table), warehouse_id=warehouse_id)
    except Exception as e:
        logger.warning("Could not create registry table %s: %s", registry_table, e)
        raise

    now = time.time()
    try:
        me = ws.current_user.me()
        published_by = getattr(me, "user_name", None) or getattr(me, "display_name", None) or "unknown"
    except Exception:
        published_by = "unknown"

    run_sql(
        ws,
        _REGISTRY_MERGE.format(
            table=registry_table,
            agent_id=_q(agent_id),
            name=_q(name),
            display_name=_q(display_name or name),
            description=_q(description),
            endpoint_url=_q(endpoint_url),
            endpoint_type=_q(endpoint_type),
            workspace_host=_q(host),
            supervisor_agent_id=_q(supervisor_agent_id),
            published_by=_q(published_by),
            published_at=str(now),
            updated_at=str(now),
        ),
        warehouse_id=warehouse_id,
    )
    logger.info("Registered %s in registry table %s", name, registry_table)
