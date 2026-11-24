"""genie_tool / genie_query_tool / genie_one_tool — Genie as a registered apx-agent tool.

Three factories:

  * ``genie_tool(space_id)`` — returns the Genie space's *text answer* as a string.
    Use when the agent needs a narrative response ("what's our top-selling region?").

  * ``genie_query_tool(space_id)`` — returns a structured dict including
    ``sql_results`` rows, ``generated_sql``, and ``genie_response``. Use when
    downstream agents need to operate on the data, not just read the answer.

  * ``genie_one_tool()`` — asks Genie One (the workspace-wide Genie, not one space)
    through its managed MCP and returns the finished answer as one tool call.

``genie_tool`` and ``genie_query_tool`` continue one Genie conversation per apx
session per space, so follow-up questions keep their context (#1254). apx deletes
the conversations it started when the session ends or is evicted, and
``sweep_genie_conversations`` / ``apx-agent resources genie-sweep`` clear leftovers (#1255).

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly at function definition time and
``get_type_hints()`` in ``_inspection.py`` sees the real Annotated[...] type, not a string.
"""

import collections
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from databricks.sdk.errors import NotFound
from databricks.sdk.errors.base import DatabricksError

from ._errors import ToolError
from ._mlflow_tracing import current_session_id
from ._obo import reauthorize_hint

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


# ---------------------------------------------------------------------------
# Follow-ups: one Genie conversation per apx session per space (#1254, #1255)
# ---------------------------------------------------------------------------

@dataclass
class _Conversation:
    conversation_id: str
    ws: Any  # the client that created it, reused to delete it
    last_used: float


class GenieConversations:
    """Bounded map of (apx session, Genie space) → the Genie conversation apx started.

    Later questions in a session continue that conversation, so Genie keeps the
    context of earlier ones. A conversation is deleted when its session ends
    (:meth:`end_session`), sits idle past ``idle_ttl_s``, or is the least recently
    used once more than ``max_conversations`` are tracked. Deletion is best-effort:
    a failure is logged and never reaches a user request.
    """

    def __init__(self, *, max_conversations: int = 512, idle_ttl_s: float = 3600.0) -> None:
        self.max_conversations = max_conversations
        self.idle_ttl_s = idle_ttl_s
        self._entries: collections.OrderedDict[tuple[str, str], _Conversation] = (
            collections.OrderedDict()
        )
        self._lock = threading.Lock()

    def get(self, session_id: str, space_id: str) -> str | None:
        """The live conversation for this session and space, or ``None``."""
        with self._lock:
            evicted = self._pop_idle()
            entry = self._entries.get((session_id, space_id))
            if entry is not None:
                entry.last_used = time.monotonic()
                self._entries.move_to_end((session_id, space_id))
        self._delete(evicted)
        return entry.conversation_id if entry is not None else None

    def put(self, session_id: str, space_id: str, conversation_id: str, ws: Any) -> None:
        """Track a conversation apx started, evicting past ``max_conversations``."""
        with self._lock:
            self._entries[(session_id, space_id)] = _Conversation(
                conversation_id, ws, time.monotonic()
            )
            self._entries.move_to_end((session_id, space_id))
            evicted = self._pop_idle()
            while len(self._entries) > self.max_conversations:
                evicted.append(self._entries.popitem(last=False))
        self._delete(evicted)

    def forget(self, session_id: str, space_id: str) -> None:
        """Drop a conversation that no longer exists, without deleting it."""
        with self._lock:
            self._entries.pop((session_id, space_id), None)

    def end_session(self, session_id: str) -> int:
        """Delete every conversation ``session_id`` started; returns how many it tracked."""
        with self._lock:
            keys = [key for key in self._entries if key[0] == session_id]
            ended = [(key, self._entries.pop(key)) for key in keys]
        self._delete(ended)
        return len(ended)

    def _pop_idle(self) -> list[tuple[tuple[str, str], _Conversation]]:
        cutoff = time.monotonic() - self.idle_ttl_s
        idle = [key for key, entry in self._entries.items() if entry.last_used < cutoff]
        return [(key, self._entries.pop(key)) for key in idle]

    @staticmethod
    def _delete(entries: list[tuple[tuple[str, str], _Conversation]]) -> None:
        for (_, space_id), entry in entries:
            try:
                entry.ws.genie.delete_conversation(
                    space_id=space_id, conversation_id=entry.conversation_id
                )
            except Exception as exc:
                logger.warning(
                    "Could not delete Genie conversation %s in space %s: %s",
                    entry.conversation_id, space_id, exc,
                )


_CONVERSATIONS = GenieConversations()


def end_genie_session(session_id: str) -> int:
    """Delete the Genie conversations apx started for ``session_id``.

    Call it when a chat session ends; sessions that never call it are cleaned
    up by idle/LRU eviction. Returns how many conversations the session had.
    Deletion failures are logged, never raised.
    """
    return _CONVERSATIONS.end_session(session_id)


def _ask(ws: Any, space_id: str, question: str, *, follow_ups: bool) -> Any:
    """Ask ``question``, continuing this session's conversation when there is one.

    Without a session (or with ``follow_ups=False``) every question starts a new
    conversation, as before. A tracked conversation that no longer exists is
    replaced by a new one.
    """
    session_id = current_session_id() if follow_ups else None
    if session_id is None:
        return _run_genie_query(ws, space_id, question)
    conversation_id = _CONVERSATIONS.get(session_id, space_id)
    if conversation_id is not None:
        try:
            return ws.genie.create_message_and_wait(
                space_id=space_id, conversation_id=conversation_id, content=question
            )
        except NotFound:
            logger.info(
                "Genie conversation %s is gone in space %s; starting a new one",
                conversation_id, space_id,
            )
            _CONVERSATIONS.forget(session_id, space_id)
    msg = _run_genie_query(ws, space_id, question)
    if msg.conversation_id:
        _CONVERSATIONS.put(session_id, space_id, str(msg.conversation_id), ws)
    return msg


def sweep_genie_conversations(
    ws: Any,
    space_id: str,
    *,
    older_than_s: float,
    include_all: bool = False,
    dry_run: bool = True,
) -> list[str]:
    """List, and unless ``dry_run`` delete, Genie conversations older than ``older_than_s``.

    Removes conversations left behind when a process stopped before its sessions
    ended. Lists the caller's own conversations; ``include_all=True`` lists every
    user's (needs CAN MANAGE on the space). The listing cannot tell apx-started
    conversations from ones a person started in the Genie UI, so pick an age past
    any conversation still in use. Returns the stale ids on a dry run, else the ids
    actually deleted; a failed delete is logged and skipped.
    """
    cutoff_ms = (time.time() - older_than_s) * 1000
    stale: list[str] = []
    page_token: str | None = None
    while True:
        page = ws.genie.list_conversations(
            space_id=space_id, include_all=include_all, page_token=page_token
        )
        for conv in page.conversations or []:
            if conv.created_timestamp is not None and conv.created_timestamp < cutoff_ms:
                stale.append(conv.conversation_id)
        page_token = page.next_page_token
        if not page_token:
            break
    if dry_run:
        return stale
    deleted: list[str] = []
    for conversation_id in stale:
        try:
            ws.genie.delete_conversation(space_id=space_id, conversation_id=conversation_id)
        except Exception as exc:
            logger.warning(
                "Could not delete Genie conversation %s in space %s: %s",
                conversation_id, space_id, exc,
            )
            continue
        deleted.append(conversation_id)
    return deleted


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
    follow_ups: bool = True,
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
        follow_ups: Continue one Genie conversation per apx session, so later
            questions keep the context of earlier ones. ``False`` starts a new
            conversation for every question. Defaults to ``True``.
    """
    if not space_id:
        raise ValueError(
            "genie_tool() requires a non-empty space_id. "
            "Check that the GENIE_SPACE_ID (or equivalent) environment variable is set "
            "before calling genie_tool() at module level, or guard the call:\n"
            "    if SPACE_ID: my_tool = genie_tool(SPACE_ID)"
        )

    from ._defaults import UserClientDependency

    _desc = (
        description
        or f"Ask a natural-language question to the Genie space and receive an answer. "
        f"Use this for data questions that Genie can answer via SQL. (space_id={space_id})"
    )

    async def _ask_genie(question: str, ws: UserClientDependency) -> str:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        try:
            msg = _ask(ws, space_id, question, follow_ups=follow_ups)
        except DatabricksError as e:
            # A Genie API failure is a finding, not a pipeline-fatal 500 (#562)
            # — matches the structured genie_query_tool, which already contains.
            hint = reauthorize_hint(e)
            if hint is not None:
                raise ToolError(str(hint)) from e
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
    follow_ups: bool = True,
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
        follow_ups: Continue one Genie conversation per apx session (see
            ``genie_tool``). ``False`` starts a new conversation per question.

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
            msg = _ask(ws, space_id, question, follow_ups=follow_ups)
        except Exception as exc:
            logger.warning("Genie query failed for space %s: %s", space_id, exc)
            hint = reauthorize_hint(exc)
            detail = str(hint) if hint is not None else f"Genie query failed: {exc}"
            return {"error": detail, "question": question}

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


# ---------------------------------------------------------------------------
# Factory 3: Genie One (workspace-wide), via its managed MCP
# ---------------------------------------------------------------------------

GENIE_ONE_MCP_PATH = "/api/2.0/mcp/genie"


async def _genie_one_state(ws: Any, question: str, *, wait_s: float, poll_s: float) -> dict[str, Any]:
    """``genie_ask``, then ``genie_poll_response`` until done or ``wait_s``; the last state.

    Genie One's MCP answers a polling client asynchronously: ``genie_ask`` returns
    ``in_progress`` plus IDs, and the answer arrives through ``genie_poll_response``
    (typically 40-260s). Owning the loop here keeps it out of the model's step budget.
    """
    from .mcp_consume import _open_session, call_until_done

    url = f"{ws.config.host.rstrip('/')}{GENIE_ONE_MCP_PATH}"
    async with _open_session(url, ws.config.authenticate(), "http") as session:
        result = await call_until_done(
            session, "genie_ask", {"question": question}, poll_tool="genie_poll_response",
            poll_ids=("conversation_id", "response_id"), wait_s=wait_s, poll_s=poll_s,
        )
    return result.structuredContent or {}


def _genie_one_text(state: dict[str, Any], wait_s: float) -> str:
    link = state.get("deep_link")
    answer = state.get("final_answer")
    if state.get("status") == "completed" and isinstance(answer, str) and answer.strip():
        return f"{answer.strip()}\n\n[Explore in Databricks]({link})" if link else answer.strip()
    follow = f" Follow the answer here: [Explore in Databricks]({link})." if link else ""
    return f"Genie One did not finish within {wait_s:.0f}s (status: {state.get('status')}).{follow}"


def genie_one_tool(
    *,
    name: str = "ask_genie_one",
    description: str | None = None,
    wait_s: float = 150.0,
    poll_s: float = 4.0,
) -> Any:
    """Return a tool that asks Genie One a question and returns its finished answer.

    Genie One is the workspace-wide Genie: it consults the Genie spaces and data
    the calling user can see. Its managed MCP (``/api/2.0/mcp/genie``) is
    asynchronous, so exposing its raw tools makes the model poll and can exhaust
    an agent's step budget. This tool owns the ask-and-poll loop and makes it
    one call, run as the calling user (OBO). It declares the ``genie`` user scope.

    Answers typically take 40-260s. Past ``wait_s`` the tool returns Genie's deep
    link so the user can follow the answer in the Databricks UI.

    Args:
        name: Tool name shown to the LLM. Defaults to ``"ask_genie_one"``.
        description: Tool description shown to the LLM.
        wait_s: Longest time to wait for an answer before returning the link.
        poll_s: Seconds between polls (Genie One asks for 2-5s).

    Example::

        from apx_agent import Agent, genie_one_tool

        agent = Agent(tools=[genie_one_tool()])
    """
    if wait_s <= 0 or poll_s <= 0:
        raise ValueError("genie_one_tool() requires positive wait_s and poll_s")

    from ._defaults import UserClientDependency
    from ._resources import require_user_api_scopes
    from ._tool_factory import build_tool

    _desc = description or (
        "Ask Genie One a natural-language question about the data and business context "
        "in this workspace (definitions, balances, trends, what changed). Returns a grounded "
        "answer with a link to explore it in Databricks. Answers can take a few minutes."
    )

    async def _ask_genie_one(question: str, ws: UserClientDependency) -> str:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        try:
            state = await _genie_one_state(ws, question, wait_s=wait_s, poll_s=poll_s)
        except Exception as e:
            hint = reauthorize_hint(e)
            if hint is not None:
                raise ToolError(str(hint)) from e
            raise ToolError(f"Genie One question failed: {e}") from e
        return _genie_one_text(state, wait_s)

    tool = build_tool(_ask_genie_one, name=name, description=_desc)
    require_user_api_scopes(tool, ["genie"])
    return tool
