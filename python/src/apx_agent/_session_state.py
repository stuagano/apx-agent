"""Session-state persistence helpers for the G3 turn boundary.

The served adapters seed the in-graph ``state`` channel from a conversation's
``session_state`` and, after the turn, persist the final ``state`` back through
``ConversationStore.set_session_state``. Prefixes route keys:

- no prefix — this conversation (``set_session_state``)
- ``temp:`` — scratch; never persisted
- ``user:`` / ``app:`` — cross-session ADK scopes; partitioned here but **not**
  written to ConversationStore or managed memory (see
  docs/design/keyed-state-scopes.md)

See docs/design/session-state-persistence.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_TEMP_PREFIX = "temp:"
_USER_PREFIX = "user:"
_APP_PREFIX = "app:"


@dataclass(frozen=True)
class PartitionedState:
    """Final in-graph ``state`` split by persistence scope.

    ``user`` / ``app`` keys are stored **without** their prefix. ``temp`` is
    dropped at the boundary, not retained.
    """

    session: dict[str, Any] = field(default_factory=dict)
    user: dict[str, Any] = field(default_factory=dict)
    app: dict[str, Any] = field(default_factory=dict)


def persistable_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Session-scoped keys only: drop ``temp:``, ``user:``, and ``app:``.

    ``temp:`` is scratch. ``user:`` / ``app:`` are not ConversationStore
    ``session_state`` — putting them there would pin them to one conversation.
    """
    return partition_state(state).session


def partition_state(state: dict[str, Any] | None) -> PartitionedState:
    """Split ``state`` into session / user / app buckets; drop ``temp:``."""
    session: dict[str, Any] = {}
    user: dict[str, Any] = {}
    app: dict[str, Any] = {}
    if not state:
        return PartitionedState()
    for key, value in state.items():
        name = str(key)
        if name.startswith(_TEMP_PREFIX):
            continue
        if name.startswith(_USER_PREFIX):
            user[name[len(_USER_PREFIX):]] = value
        elif name.startswith(_APP_PREFIX):
            app[name[len(_APP_PREFIX):]] = value
        else:
            session[name] = value
    return PartitionedState(session=session, user=user, app=app)


def persist_session_state(
    store: Any, conversation_id: str | None, final_state: dict[str, Any] | None
) -> None:
    """Governed, never-fatal write-back of session-scoped state.

    No-ops without a store or conversation id. Writes only unprefixed keys.
    Any failure (including a non-JSON-serializable value surfacing in a backend)
    is logged and swallowed — the response has already been produced, so a
    persist failure must not crash the turn.
    """
    if store is None or conversation_id is None:
        return
    persisted = persistable_state(final_state)
    try:
        store.set_session_state(conversation_id, persisted)
    except Exception:
        logger.warning(
            "session_state persist degraded for %s", conversation_id, exc_info=True
        )
