"""example-mining — extract few-shot examples from real session history.

Walks a :class:`SessionStore`, pairs (user, assistant) message turns from
each session's ``history`` list, and writes them as :class:`Example` rows
into an :class:`ExampleStore`. Useful for cold-starting a few-shot bank
from real conversations.

Turn-pairing rules:

  * We iterate ``Session.history`` in order. For each message with
    ``role == "user"``, we look forward and emit a :class:`Turn` for the
    **next** message whose ``role == "assistant"`` AND whose ``content``
    is a non-empty string.
  * Messages with any other role between them (e.g. ``"tool"``,
    ``"system"``, assistant messages that only carry ``tool_calls`` and
    no content) are skipped — they are scaffolding, not the
    demonstration response.
  * After emitting an assistant turn, we resume scanning for the next
    user message. Multiple user→assistant pairs per session are fine.
  * We never pair across sessions.

Session discovery:

  The Python :class:`SessionStore` protocol does not include ``list``
  (unlike its TS sibling). When the caller does not pass
  ``session_ids``, we duck-type the store: if it has a ``list()`` method
  we call it; if it exposes a ``_sessions`` dict we read its keys; else
  we raise :class:`ValueError`.

Mirrors ``typescript/src/example-mining.ts`` — same semantics, snake_case
field names. ``dry_run`` materializes :class:`Example` rows client-side
without writing; their ids are placeholders minted client-side and will
NOT match what the store would mint on a real ``add()``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, Union, runtime_checkable

from ._example import Example, ExampleStore, materialize_example

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One mined (user, assistant) turn drawn from a session's history.

    Attributes:
        session_id: The session this turn was pulled from.
        user_message: The raw user message dict (typically has
            ``role`` and ``content``).
        assistant_message: The raw assistant message dict.
        turn_index: 0-based ordinal of this Turn within its session
            (first user→assistant pair is index 0). Not the raw index
            into the history list.
    """

    session_id: str
    user_message: Mapping[str, Any]
    assistant_message: Mapping[str, Any]
    turn_index: int


@dataclass(frozen=True)
class MineResult:
    """Result returned by :func:`mine_examples`.

    Attributes:
        sessions_scanned: Number of sessions actually loaded + walked.
        turns_considered: Total user→assistant pairs found before any
            filtering. Useful for tuning ``filter_fn``.
        examples_added: Number of Examples actually written to the
            store. Always 0 when ``dry_run=True``.
        examples: The materialized Examples — either fresh from
            ``store.add()`` or client-side materialized when ``dry_run``.
    """

    sessions_scanned: int
    turns_considered: int
    examples_added: int
    examples: tuple[Example, ...]


# Optional store protocol with ``list()`` — duck-typed at runtime.
@runtime_checkable
class _ListableSessionStore(Protocol):
    def list(self) -> Sequence[str]: ...


IntentFn = Callable[[Turn], Union[str, Awaitable[str]]]
ScoreFn = Callable[
    [Turn], Union[float, None, Awaitable[Union[float, None]]]
]
FilterFn = Callable[[Turn], bool]
TagsFn = Callable[[Turn], Sequence[str]]
MetadataFn = Callable[[Turn], Mapping[str, Any]]


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def pair_turns(
    session_id: str, messages: Sequence[Mapping[str, Any]]
) -> list[Turn]:
    """Walk a session's history and emit (user, assistant) :class:`Turn`s.

    Exported so callers + tests can reproduce the pairing without going
    through the full mining pipeline. See module docstring for the exact
    rules.
    """
    turns: list[Turn] = []
    turn_index = 0
    pending_user: Mapping[str, Any] | None = None
    for m in messages:
        role = m.get("role")
        if role == "user":
            pending_user = m
            continue
        if role == "assistant" and pending_user is not None:
            content = m.get("content")
            if isinstance(content, str) and len(content) > 0:
                turns.append(
                    Turn(
                        session_id=session_id,
                        user_message=pending_user,
                        assistant_message=m,
                        turn_index=turn_index,
                    )
                )
                turn_index += 1
                pending_user = None
            # else: assistant message with no content (tool-call only).
            # Keep waiting for a real assistant reply.
        # All other roles (tool, system, etc.) — skip.
    return turns


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def _default_filter(turn: Turn) -> bool:
    content = turn.assistant_message.get("content")
    return isinstance(content, str) and len(content.strip()) > 0


def _default_intent(_turn: Turn) -> str:
    return "default"


def _maybe_await(value: Any) -> Any:
    """Synchronously coerce a sync-or-async callable return.

    The mining API is sync — callers wiring an async ``intent_fn`` /
    ``score_fn`` must either await the result themselves and pass a sync
    wrapper, OR the function must return a value (we'll not block on
    coroutines). To stay strict-sync, we reject coroutines.
    """
    if hasattr(value, "__await__"):
        raise TypeError(
            "mine_examples is sync; pass a sync callable for intent_fn / score_fn "
            "(wrap your async LLM call in asyncio.run(...) if needed)"
        )
    return value


def _discover_session_ids(store: Any) -> list[str]:
    """Best-effort enumeration of session ids on a store.

    Tries ``store.list()``; falls back to ``store._sessions.keys()`` for
    the in-memory store; raises if neither is available.
    """
    list_attr = getattr(store, "list", None)
    if callable(list_attr):
        return list(list_attr())
    sessions = getattr(store, "_sessions", None)
    if isinstance(sessions, dict):
        return list(sessions.keys())
    raise ValueError(
        "session_store has no list() method and no _sessions dict; "
        "pass session_ids explicitly"
    )


# ---------------------------------------------------------------------------
# Mining
# ---------------------------------------------------------------------------


def mine_examples(
    *,
    session_store: Any,
    example_store: ExampleStore,
    agent_id: str,
    session_ids: Sequence[str] | None = None,
    intent_fn: IntentFn | None = None,
    score_fn: ScoreFn | None = None,
    filter_fn: FilterFn | None = None,
    tags_fn: TagsFn | None = None,
    metadata_fn: MetadataFn | None = None,
    limit: int | None = None,
    min_score: float | None = None,
    dry_run: bool = False,
) -> MineResult:
    """Mine (user, assistant) Examples from a SessionStore's history."""
    if intent_fn is None:
        intent_fn = _default_intent
    if filter_fn is None:
        filter_fn = _default_filter

    ids: list[str]
    if session_ids is not None:
        ids = list(session_ids)
    else:
        ids = _discover_session_ids(session_store)

    sessions_scanned = 0
    turns_considered = 0
    out: list[Example] = []

    stop = False
    for sid in ids:
        if stop:
            break
        snap = session_store.get(sid)
        if snap is None:
            continue
        sessions_scanned += 1
        history = getattr(snap, "history", None)
        if history is None:
            # Tolerate snapshot-shaped objects (e.g. TS-style dict-ish)
            history = snap.get("history", []) if isinstance(snap, Mapping) else []
        turns = pair_turns(sid, history)

        for turn in turns:
            turns_considered += 1
            if not filter_fn(turn):
                continue

            intent = _maybe_await(intent_fn(turn))
            score: float | None = None
            if score_fn is not None:
                score = _maybe_await(score_fn(turn))
            # When a quality floor is requested, drop unscored turns too —
            # mirrors the store-side ``score >= min_score`` predicate, which
            # excludes NULL scores. Letting unscored turns through here would
            # write rows the store-side filter would never surface.
            if min_score is not None and (score is None or score < min_score):
                continue

            tags: tuple[str, ...] = (
                tuple(tags_fn(turn)) if tags_fn is not None else ()
            )
            extra_meta: dict[str, Any] = (
                dict(metadata_fn(turn)) if metadata_fn is not None else {}
            )
            metadata: dict[str, Any] = {
                **extra_meta,
                "session_id": turn.session_id,
                "turn_index": turn.turn_index,
            }

            example_input: dict[str, Any] = {
                "agent_id": agent_id,
                "input": str(turn.user_message.get("content", "")),
                "output": str(turn.assistant_message.get("content", "")),
                "intent": intent,
                "tags": tags,
                "metadata": metadata,
            }
            if score is not None:
                example_input["score"] = score

            if dry_run:
                out.append(materialize_example(example_input))
            else:
                out.append(example_store.add(example_input))

            if limit is not None and len(out) >= limit:
                stop = True
                break

    return MineResult(
        sessions_scanned=sessions_scanned,
        turns_considered=turns_considered,
        examples_added=0 if dry_run else len(out),
        examples=tuple(out),
    )


__all__ = [
    "FilterFn",
    "IntentFn",
    "MetadataFn",
    "MineResult",
    "ScoreFn",
    "TagsFn",
    "Turn",
    "mine_examples",
    "pair_turns",
]
