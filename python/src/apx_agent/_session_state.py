"""Session-state persistence helpers for the G3 phase-2 turn boundary.

The served adapters seed the in-graph ``state`` channel from a conversation's
``session_state`` and, after the turn, persist the final ``state`` back through
``ConversationStore.set_session_state``. ``temp:``-prefixed keys are scratch —
readable in-graph during the turn but never persisted. See
docs/design/session-state-persistence.md.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TEMP_PREFIX = "temp:"


def persistable_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``state`` with ``temp:``-scoped keys removed (they live only for
    the turn)."""
    if not state:
        return {}
    return {k: v for k, v in state.items() if not str(k).startswith(_TEMP_PREFIX)}


def persist_session_state(
    store: Any, conversation_id: str | None, final_state: dict[str, Any] | None
) -> None:
    """Governed, never-fatal write-back of session-scoped state.

    No-ops without a store or conversation id. Strips ``temp:`` keys, then calls
    the governed ``set_session_state`` mutator. Any failure (including a
    non-JSON-serializable value surfacing in a backend) is logged and swallowed —
    the response has already been produced, so a persist failure must not crash
    the turn.
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
