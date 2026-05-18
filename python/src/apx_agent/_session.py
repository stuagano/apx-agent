"""Session and SessionStore — multi-turn conversation memory.

A Session persists conversation state across requests. When a ChatAgent is
compiled with a ``session_store``, each ``predict()`` call:

  1. Reads ``session_id`` from ``custom_inputs``.
  2. Loads the session from the store (creating one if missing).
  3. Prepends the session's history to the incoming messages so the LLM sees
     prior turns.
  4. Runs the agent.
  5. Appends the new turn (the user's input + the agent's reply) to the
     session and persists.

Two store implementations:

  * ``InMemorySessionStore`` — default, process-local. Good for tests, dev,
    and single-process Apps.
  * ``DeltaSessionStore`` — UC Delta-backed, suitable for Model Serving and
    multi-replica Apps. See ``_session_delta.py``.

The wire format for history is a list of ``ChatAgentMessage``-compatible
dicts so the store stays free of any specific agent runtime.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session type
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """A multi-turn conversation's persisted state.

    Attributes:
        session_id: Stable identifier for the conversation. Conventional shape
            is ``"<scope>:<id>"`` — e.g. ``"user:alice:thread-42"`` — but the
            framework treats it as an opaque string.
        history: List of message dicts forming the prior turns. Each dict has
            at least ``role`` and ``content`` keys; ``tool_calls`` and
            ``tool_call_id`` are passed through verbatim when present.
        state: Free-form dict for working memory. Agent code can read/write
            via ``Session.state["key"]`` to maintain non-message scratch state
            across turns (e.g. user preferences, intermediate decisions).
        created_at: Unix epoch seconds at first persistence.
        updated_at: Unix epoch seconds at the most recent persistence.
    """

    session_id: str
    history: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionStore(Protocol):
    """Storage backend for ``Session`` objects.

    Implementations may be process-local (``InMemorySessionStore``),
    UC-Delta-backed (``DeltaSessionStore``), or anything else that
    satisfies the get/put/delete contract.
    """

    def get(self, session_id: str) -> Session | None:
        """Return the session with ``session_id``, or ``None`` if missing."""
        ...

    def put(self, session: Session) -> None:
        """Persist ``session``. Updates ``updated_at`` to now."""
        ...

    def delete(self, session_id: str) -> None:
        """Remove the session. No-op if missing."""
        ...


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


class InMemorySessionStore:
    """Process-local session store backed by a dict.

    Suitable for tests, local dev, single-process Apps. For multi-replica or
    Model-Serving deployments use ``DeltaSessionStore`` (or another durable
    backend).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def put(self, session: Session) -> None:
        session.updated_at = time.time()
        # Store a shallow copy so callers can't accidentally mutate the
        # persisted record after putting it.
        self._sessions[session.session_id] = Session(
            session_id=session.session_id,
            history=list(session.history),
            state=dict(session.state),
            created_at=session.created_at,
            updated_at=session.updated_at,
        )

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# Helpers used by the predict-path integration
# ---------------------------------------------------------------------------


def load_or_create_session(store: SessionStore, session_id: str) -> Session:
    """Return the stored session, creating an empty one if absent."""
    existing = store.get(session_id)
    if existing is not None:
        return existing
    session = Session(session_id=session_id)
    store.put(session)
    return session


def append_turn(
    session: Session,
    *,
    input_messages: list[dict[str, Any]],
    new_messages: list[dict[str, Any]],
) -> None:
    """Append a turn to the session's history.

    ``input_messages`` are the inbound messages for this turn (typically a
    single user message; multiple if the caller supplied conversation context
    explicitly). ``new_messages`` are what the agent produced this turn —
    assistant replies, tool calls, tool results.

    Messages already present in ``session.history`` are not re-appended; we
    diff by object identity to avoid double-counting when the caller passed
    the session's own history as part of the input.
    """
    existing_ids = {id(m) for m in session.history}
    for m in input_messages:
        if id(m) not in existing_ids:
            session.history.append(m)
    for m in new_messages:
        if id(m) not in existing_ids:
            session.history.append(m)
