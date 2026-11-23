"""Session-state persistence helpers for the G3 turn boundary.

The served adapters seed the in-graph ``state`` channel from a conversation's
``session_state`` and, after the turn, persist the final ``state`` back through
``ConversationStore.set_session_state``. Prefixes route keys:

- no prefix — this conversation (``set_session_state``)
- ``temp:`` — scratch; never persisted
- ``user:`` / ``app:`` — partitioned here but **not** written to
  ConversationStore or managed memory. ``user:`` is managed memory (the
  pipeline and the memory tools). ``app:`` is deploy-time config. A write or
  a drop logs once per key (see docs/design/keyed-state-scopes.md)

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
_SCOPED_PREFIXES = (_USER_PREFIX, _APP_PREFIX)
_warned_scoped_keys: set[str] = set()


def note_unpersisted_scope(key: str) -> None:
    """Warn once per process when a ``user:`` or ``app:`` key is written or dropped.

    Those prefixes are not a key-value store. ``user:`` preferences are managed
    memory. ``app:`` values are deploy-time config. See
    docs/design/keyed-state-scopes.md.
    """
    if not key.startswith(_SCOPED_PREFIXES) or key in _warned_scoped_keys:
        return
    _warned_scoped_keys.add(key)
    if key.startswith(_USER_PREFIX):
        logger.warning(
            "state key %s is not stored as cross-session key-value. "
            "Save the preference with the remember tool, or let the memory "
            "pipeline distill it (about every 24h, or a manual run), and read "
            "it with recall. See docs/running/sessions-and-memory.md#cross-session-per-user-preferences.",
            key,
        )
        return
    logger.warning(
        "state key %s is not runtime state. app: values are agent config set "
        "at deploy time (agent.toml or [tool.apx]); agents do not write them. "
        "See docs/design/keyed-state-scopes.md (#1233).",
        key,
    )


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
    parts = partition_state(final_state)
    for name in parts.user:
        note_unpersisted_scope(f"{_USER_PREFIX}{name}")
    for name in parts.app:
        note_unpersisted_scope(f"{_APP_PREFIX}{name}")
    persisted = parts.session
    try:
        store.set_session_state(conversation_id, persisted)
    except Exception:
        logger.warning(
            "session_state persist degraded for %s", conversation_id, exc_info=True
        )
