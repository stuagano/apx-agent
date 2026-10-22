"""Cumulative per-session token and usd cap enforcement on the served paths (#768, #836).

An ``LlmAgent(session_budget={"tokens": N})`` caps the *cumulative* tokens spent
across ALL turns of one session (``thread_id``). ``{"usd": D}`` (alone or next
to ``tokens``) caps running dollar cost from the same usage, priced with
:func:`apx_agent._cost.usd_for_token_usage`. The running totals live in the
compiled graph's keyed ``state`` channel (``session_tokens`` / ``session_usd``),
which the checkpointer persists per ``thread_id`` — so a long session of
individually small turns eventually trips the cap.

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

from ._cost import usd_for_token_usage
from ._errors import SessionBudgetExceeded

logger = logging.getLogger(__name__)

_STATE_KEY = "session_tokens"
_USD_KEY = "session_usd"


def cap_for(agent: Any) -> int | None:
    """The declared cumulative token cap for ``agent``, or ``None`` if uncapped."""
    sb = getattr(agent, "_session_budget", None)
    if sb is None or "tokens" not in sb:
        return None
    return sb["tokens"]


def usd_cap_for(agent: Any) -> float | None:
    """The declared cumulative dollar cap for ``agent``, or ``None`` if uncapped."""
    sb = getattr(agent, "_session_budget", None)
    if sb is None or "usd" not in sb:
        return None
    return float(sb["usd"])


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
            # `or 0` (not a default) so a present-but-None field — which langchain
            # permits on aggregated streaming chunks — contributes 0 instead of
            # raising int(None) at the turn boundary.
            total += int(um.get("input_tokens") or 0) + int(um.get("output_tokens") or 0)
    return total


def turn_usd(messages: list[Any], *, model: str | None) -> float | None:
    """Dollar cost of this turn's usage, or ``None`` when the model has no rate."""
    inp = 0
    out = 0
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if isinstance(um, dict):
            inp += int(um.get("input_tokens") or 0)
            out += int(um.get("output_tokens") or 0)
    return usd_for_token_usage(inp, out, model=model)


def _prior_state(graph: Any, lg_config: dict[str, Any] | None) -> Any:
    if lg_config is None:
        return {}
    try:
        return graph.get_state(lg_config).values.get("state") or {}
    except Exception:
        return {}


def _prior_tokens(graph: Any, lg_config: dict[str, Any] | None) -> int:
    """The cumulative token total persisted for this session, or 0 if none."""
    return int(_prior_state(graph, lg_config).get(_STATE_KEY, 0))


def _prior_usd(graph: Any, lg_config: dict[str, Any] | None) -> float:
    raw = _prior_state(graph, lg_config).get(_USD_KEY, 0)
    if raw is None:
        return 0.0
    return float(raw)


def enforce_before_turn(
    graph: Any,
    lg_config: dict[str, Any] | None,
    cap: int | None,
    *,
    usd_cap: float | None = None,
) -> int:
    """Refuse before running if the session is already at/over a declared cap.

    Returns the prior cumulative token total so the caller can add this turn's
    usage without re-reading tokens.
    """
    prior = _prior_tokens(graph, lg_config)
    if cap is not None and prior >= cap:
        raise SessionBudgetExceeded(spent=prior, cap=cap)
    if usd_cap is not None:
        spent_usd = _prior_usd(graph, lg_config)
        if spent_usd >= usd_cap:
            raise SessionBudgetExceeded(spent=spent_usd, cap=usd_cap, unit="usd")
    return prior


def accrue_turn(
    graph: Any,
    lg_config: dict[str, Any] | None,
    prior: int,
    new_messages: list[Any],
    *,
    strict: bool = False,
    usd_cap: float | None = None,
    model: str | None = None,
) -> int:
    """Add this turn's usage to ``prior``, persist the new total, return it.

    No raise. Used on the approval-pause path so the model tokens spent up to the
    pause are still counted (#768) even though that turn returns an approval
    response rather than a completed result; the next turn's
    :func:`enforce_before_turn` will refuse if the session is now over cap.
    A declared ``usd`` cap without a published rate for ``model`` fails closed.
    """
    total = prior + turn_usage(new_messages)
    state: dict[str, Any] = {_STATE_KEY: total}
    if usd_cap is not None:
        added = turn_usd(new_messages, model=model)
        if added is None:
            raise ValueError(
                f"session_budget['usd'] has no published rate for model {model!r}; "
                "refusing the turn rather than running uncapped"
            )
        state[_USD_KEY] = _prior_usd(graph, lg_config) + added
    if lg_config is not None:
        try:
            graph.update_state(lg_config, {"state": state})
        except Exception as exc:  # pragma: no cover — checkpointer write failure
            if strict:
                raise
            logger.warning("session_budget: failed to persist token total: %s", exc)
    return total


def enforce_after_turn(
    graph: Any,
    lg_config: dict[str, Any] | None,
    prior: int,
    new_messages: list[Any],
    cap: int | None,
    *,
    strict: bool = False,
    usd_cap: float | None = None,
    model: str | None = None,
) -> None:
    """Accrue this turn's usage, persist the new total, raise if it crossed a cap."""
    prior_usd = _prior_usd(graph, lg_config) if usd_cap is not None else 0.0
    total = accrue_turn(
        graph, lg_config, prior, new_messages, strict=strict, usd_cap=usd_cap, model=model,
    )
    if cap is not None and total >= cap:
        raise SessionBudgetExceeded(spent=total, cap=cap)
    if usd_cap is not None:
        added = turn_usd(new_messages, model=model)
        if added is None:
            raise ValueError(
                f"session_budget['usd'] has no published rate for model {model!r}; "
                "refusing the turn rather than running uncapped"
            )
        spent_usd = prior_usd + added
        if spent_usd >= usd_cap:
            raise SessionBudgetExceeded(spent=spent_usd, cap=usd_cap, unit="usd")
