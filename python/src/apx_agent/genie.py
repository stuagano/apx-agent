"""genie_tool / genie_query_tool — wrap a Genie space as a registered apx-agent tool.

Two factories for two use cases:

  * ``genie_tool(space_id)`` — returns the Genie space's *text answer* as a string.
    Use when the agent needs a narrative response ("what's our top-selling region?").

  * ``genie_query_tool(space_id)`` — returns a structured dict including
    ``sql_results`` rows, ``generated_sql``, and ``genie_response``. Use when
    downstream agents need to operate on the data, not just read the answer.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly at function definition time and
``get_type_hints()`` in ``_inspection.py`` sees the real Annotated[...] type, not a string.
"""

import logging
from typing import Any

from databricks.sdk.errors.base import DatabricksError

from ._errors import ToolError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers — both factories use the modern ws.genie SDK
# ---------------------------------------------------------------------------

def _run_genie_query(ws: Any, space_id: str, question: str) -> Any:
    """Start a Genie conversation, wait for completion, return the GenieMessage.

    Returns the raw ``GenieMessage`` so callers can extract either the text
    answer or the underlying query results depending on their use case.
    """
    return ws.genie.start_conversation_and_wait(space_id=space_id, content=question)


def _extract_text(msg: Any) -> str:
    """Pull the narrative text answer out of a Genie message's attachments."""
    for att in (msg.attachments or []):
        if att.text and att.text.content:
            return str(att.text.content)
    return ""


def _extract_generated_sql(msg: Any) -> str:
    """Pull the SQL Genie generated out of a query attachment."""
    for att in (msg.attachments or []):
        if att.query and att.query.query:
            return str(att.query.query)
    return ""


def _extract_rows(ws: Any, space_id: str, msg: Any) -> list[dict[str, Any]]:
    """Fetch query results for every query attachment on the message."""
    from ._sql import decode_statement
    rows: list[dict[str, Any]] = []
    for att in (msg.attachments or []):
        if att.query is None or not att.attachment_id:
            continue
        try:
            qr = ws.genie.get_message_query_result_by_attachment(
                space_id=space_id,
                conversation_id=msg.conversation_id,
                message_id=msg.message_id,
                attachment_id=att.attachment_id,
            )
            rows.extend(decode_statement(qr.statement_response, ws=ws))
        except Exception as exc:
            logger.warning("Failed to fetch Genie query result: %s", exc)
    return rows


def _is_completed(msg: Any) -> bool:
    """Check whether a GenieMessage reached the COMPLETED status."""
    from databricks.sdk.service.dashboards import MessageStatus
    return msg.status == MessageStatus.COMPLETED


# ---------------------------------------------------------------------------
# Factory 1: narrative answer
# ---------------------------------------------------------------------------

def genie_tool(
    space_id: str,
    *,
    name: str = "ask_genie",
    description: str | None = None,
) -> Any:
    """Return a tool that queries a Genie space and returns the text answer.

    The returned callable is a normal apx-agent tool: register it directly in
    ``Agent(tools=[...])``. The LLM sees one parameter — ``question: str`` —
    and the workspace client is injected automatically from the incoming request.

    Use ``genie_query_tool(space_id)`` instead when you need the structured
    SQL results, not just the narrative answer.

    Args:
        space_id: Genie space ID (the UUID from the Genie space URL).
        name: Tool name shown to the LLM. Defaults to ``"ask_genie"``.
        description: Tool description shown to the LLM.
    """
    from ._defaults import UserClientDependency

    _desc = (
        description
        or f"Ask a natural-language question to the Genie space and receive an answer. "
        f"Use this for data questions that Genie can answer via SQL. (space_id={space_id})"
    )

    async def _ask_genie(question: str, ws: UserClientDependency) -> str:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        try:
            msg = _run_genie_query(ws, space_id, question)
        except DatabricksError as e:
            # A Genie API failure is a finding, not a pipeline-fatal 500 (#562)
            # — matches the structured genie_query_tool, which already contains.
            raise ToolError(f"Genie query failed: {e}") from e
        if not _is_completed(msg):
            logger.warning("Genie query ended with status %s in space %s", msg.status, space_id)
            return f"Genie query did not complete (status: {msg.status})."
        return _extract_text(msg)

    from ._resources import ResourceSpec
    from ._tool_factory import build_tool
    return build_tool(_ask_genie, name=name, description=_desc,
                      resources=[ResourceSpec("genie_space", space_id)])


# ---------------------------------------------------------------------------
# Factory 2: structured query results
# ---------------------------------------------------------------------------

def genie_query_tool(
    space_id: str,
    *,
    name: str = "query_genie",
    description: str | None = None,
    max_rows: int = 20,
) -> Any:
    """Return a tool that asks Genie a question and returns structured results.

    Unlike ``genie_tool``, the returned dict includes ``sql_results`` (the actual
    rows), ``generated_sql`` (the SQL Genie produced), and ``genie_response``
    (the narrative answer). Use this when downstream agents need to reason
    over the data — for example, a report-generator step that needs row-level
    detail, not just the summary.

    Args:
        space_id: Genie space ID (the UUID from the Genie space URL).
        name: Tool name shown to the LLM. Defaults to ``"query_genie"``.
        description: Tool description shown to the LLM. If omitted, a sensible
            ad-hoc-exploration description is generated.
        max_rows: Truncate ``sql_results`` to this many rows. Defaults to 20.

    Example::

        from apx_agent import Agent, genie_query_tool

        agent = Agent(tools=[
            genie_query_tool("abc123",
                             description="Explore demand orders ad-hoc"),
        ])
    """
    from ._defaults import UserClientDependency

    _desc = (
        description
        or f"Ask a natural-language question and receive both the answer and the "
        f"underlying SQL result rows. Use for ad-hoc data exploration when you "
        f"need the data itself, not just a summary. (space_id={space_id})"
    )

    async def _query_genie(question: str, ws: UserClientDependency) -> dict[str, Any]:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        try:
            msg = _run_genie_query(ws, space_id, question)
        except Exception as exc:
            logger.warning("Genie query failed for space %s: %s", space_id, exc)
            return {"error": f"Genie query failed: {exc}", "question": question}

        if not _is_completed(msg):
            return {
                "error": f"Genie query ended with status {msg.status}",
                "details": str(msg.error) if msg.error else None,
                "question": question,
            }

        rows = _extract_rows(ws, space_id, msg)
        return {
            "question": question,
            "sql_results": rows[:max_rows],
            "result_count": len(rows),
            "genie_response": _extract_text(msg)[:500],
            "generated_sql": _extract_generated_sql(msg),
        }

    from ._resources import ResourceSpec
    from ._tool_factory import build_tool
    return build_tool(_query_genie, name=name, description=_desc,
                      resources=[ResourceSpec("genie_space", space_id)])
