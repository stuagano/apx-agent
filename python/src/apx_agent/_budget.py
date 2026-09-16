"""Cumulative per-session token cap enforcement on the served paths (#768).

An ``LlmAgent(session_budget={"tokens": N})`` caps the *cumulative* tokens spent
across ALL turns of one session (``thread_id``). The running total lives in the
compiled graph's keyed ``state`` channel (``state["session_tokens"]``), which the
checkpointer persists per ``thread_id`` — so a long session of individually small
turns eventually trips the cap.

Enforcement is at the **turn boundary** (LangGraph runs the tool loop in one
call, so there is no mid-turn interrupt): each served entrypoint calls
:func:`enforce_before_turn` after compiling the graph (refuse if the session is
already at/over cap) and :func:`enforce_after_turn` once the turn's messages are
in hand (add this turn's usage, persist the new total, raise if it crossed).

Without a configured checkpointer there is no cross-turn state (``lg_config`` is
``None``), so this degrades to a per-turn cap — documented, not faked.
"""

from __future__ import annotations

import logging
from typing import Any

from ._errors import SessionBudgetExceeded

logger = logging.getLogger(__name__)

_STATE_KEY = "session_tokens"


def cap_for(agent: Any) -> int | None:
    """The declared cumulative token cap for ``agent``, or ``None`` if uncapped.

    Reads the ``session_budget={"tokens": N}`` set at construction (validated
    tokens-only there), so callers can early-out when no cap applies.
    """
    sb = getattr(agent, "_session_budget", None)
    if sb is None:
        return None
    return sb["tokens"]


def turn_usage(messages: list[Any]) -> int:
    """Sum ``input_tokens + output_tokens`` across the turn's ``AIMessage``s.

    Reads ``usage_metadata`` (a dict) off each langchain message; a message
    without it contributes 0 (defensive — a missing metric must not silently
    disable the cap, but it also can't be counted).
    """
    total = 0
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if isinstance(um, dict):
            total += int(um.get("input_tokens", 0)) + int(um.get("output_tokens", 0))
    return total


def _prior_tokens(graph: Any, lg_config: dict[str, Any] | None) -> int:
    """The cumulative total persisted for this session, or 0 if none/no checkpointer."""
    if lg_config is None:
        return 0
    try:
        st = graph.get_state(lg_config).values.get("state") or {}
    except Exception:
        return 0
    return int(st.get(_STATE_KEY, 0))


def enforce_before_turn(graph: Any, lg_config: dict[str, Any] | None, cap: int) -> int:
    """Refuse before running if the session is already at/over ``cap``.

    Returns the prior cumulative total so the caller can add this turn's usage
    without re-reading state.
    """
    prior = _prior_tokens(graph, lg_config)
    if prior >= cap:
        raise SessionBudgetExceeded(spent=prior, cap=cap)
    return prior


def enforce_after_turn(
    graph: Any,
    lg_config: dict[str, Any] | None,
    prior: int,
    new_messages: list[Any],
    cap: int,
) -> None:
    """Add this turn's usage to ``prior``, persist the new total, raise if crossed."""
    total = prior + turn_usage(new_messages)
    if lg_config is not None:
        try:
            graph.update_state(lg_config, {"state": {_STATE_KEY: total}})
        except Exception as exc:  # pragma: no cover — checkpointer write failure
            logger.warning("session_budget: failed to persist token total: %s", exc)
    if total >= cap:
        raise SessionBudgetExceeded(spent=total, cap=cap)
