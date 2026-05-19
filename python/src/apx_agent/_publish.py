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
