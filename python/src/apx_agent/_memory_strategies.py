"""Typed memory strategies (#1088): what one session distills into long-term memory.

Each declared strategy (``facts``, ``preferences``, ``summary``) is a namespace.
Its instructions can be replaced with ``memory.strategy_prompts``; its output
contract cannot. The Lakebase / in-memory extractor asks the agent's own model
once per strategy, parses every answer before writing anything, and writes each
record under the session's user principal. Re-extracting a session replaces
that session's records in every strategy namespace, so ``summary`` stays one
record per session and a re-delivered extraction never duplicates.

When it runs (#1089): ``memory.extract_every_n_turns`` schedules
:func:`schedule_session_extraction` from the compiled graph after every N user
turns. The transcript is the turn's graph state (what the checkpointer holds),
and the extraction runs on a background worker, retried, off the request path.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ._memory import Memory, MemoryFilter, MemoryStore

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

    from ._agents import LlmAgent
    from ._models import MemoryBackendConfig, MemoryStrategy

logger = logging.getLogger(__name__)

BUILTIN_PROMPTS: dict[str, str] = {
    "facts": (
        "Extract stated facts about the user and their work from this session: "
        "who they are, what they work on, systems, names and numbers they rely on. "
        "Keep only facts that stay true beyond this session."
    ),
    "preferences": (
        "Extract how the user wants things done: formats, tone, tools, defaults and "
        "things to avoid. Keep only preferences the user stated or clearly confirmed."
    ),
    "summary": (
        "Summarize this session in a few sentences: what the user wanted, what was "
        "done, and what is still open."
    ),
}

OUTPUT_CONTRACTS: dict[str, str] = {
    "facts": (
        "Output contract: reply with only a JSON array of strings, one memory per "
        "string. Reply [] when there is nothing worth saving."
    ),
    "preferences": (
        "Output contract: reply with only a JSON array of strings, one memory per "
        "string. Reply [] when there is nothing worth saving."
    ),
    "summary": "Output contract: reply with only one JSON string holding the summary.",
}


def strategy_prompt(memory: MemoryBackendConfig, strategy: MemoryStrategy) -> str:
    """The strategy's instructions: the declared override, else the built-in text."""
    overrides = memory.strategy_prompts if memory.strategy_prompts is not None else {}
    return overrides[strategy] if strategy in overrides else BUILTIN_PROMPTS[strategy]


def strategy_system_prompt(memory: MemoryBackendConfig, strategy: MemoryStrategy) -> str:
    """Instructions plus the fixed output contract no declaration can change."""
    return f"{strategy_prompt(memory, strategy)}\n\n{OUTPUT_CONTRACTS[strategy]}"


def parse_strategy_output(strategy: MemoryStrategy, text: str) -> list[str]:
    """Parse one model answer against the strategy's contract. Raises on any mismatch."""
    body = text.strip()
    if body.startswith("```"):
        body = body.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"memory strategy {strategy!r} output is not JSON: {text[:200]!r}") from exc
    if strategy == "summary":
        if not isinstance(parsed, str) or not parsed.strip():
            raise ValueError(f"memory strategy 'summary' must return one non-blank JSON string, got {parsed!r}")
        return [parsed.strip()]
    if not isinstance(parsed, list) or not all(isinstance(m, str) and m.strip() for m in parsed):
        raise ValueError(
            f"memory strategy {strategy!r} must return a JSON array of non-blank strings, got {parsed!r}"
        )
    return [m.strip() for m in parsed]


def extract_strategy_memories(
    store: MemoryStore,
    llm: Any,
    memory: MemoryBackendConfig,
    *,
    transcript: str,
    principal_id: str,
    session_id: str,
) -> list[Memory]:
    """Distill one session transcript into the declared strategy namespaces.

    ``llm`` is a LangChain chat model (the agent's own model). Every strategy's
    answer is parsed before the first write, so malformed output raises and the
    store is untouched. Records are written with ``principal_id`` (the session's
    user, never the service principal that runs the extraction).
    """
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

    strategies = memory.strategies if memory.strategies is not None else []
    if not strategies:
        raise ValueError("memory.strategies is not declared; there is nothing to extract")
    for label, value in (("transcript", transcript), ("principal_id", principal_id), ("session_id", session_id)):
        if not value.strip():
            raise ValueError(f"strategy extraction requires a non-blank {label}")

    planned: dict[str, list[str]] = {}
    for strategy in strategies:
        reply = llm.invoke([
            SystemMessage(content=strategy_system_prompt(memory, strategy)),
            HumanMessage(content=f"Session transcript:\n\n{transcript}"),
        ])
        planned[strategy] = parse_strategy_output(strategy, reply.text)

    written: list[Memory] = []
    for strategy, contents in planned.items():
        # Each run reads the whole session, so its records replace the session's
        # earlier ones. Add first, read back, then delete: a failure part-way leaves
        # a duplicate the next delivery removes, never a lost record.
        stale = [
            m for m in store.list(MemoryFilter(principal_id=principal_id, namespace=strategy, limit=10_000))
            if m.metadata.get("session_id") == session_id
        ]
        added = store.add_batch([
            {"principal_id": principal_id, "namespace": strategy, "content": c,
             "metadata": {"session_id": session_id, "strategy": strategy}}
            for c in contents
        ])
        for row, content in zip(added, contents, strict=True):
            back = store.get(row.id)
            if back is None or back.content != content or back.principal_id != principal_id:
                raise ValueError(f"memory {row.id!r} ({strategy}) did not read back as written")
        for old in stale:
            store.delete(old.id)
        written += added
    logger.info(
        "Extracted %d memories for session %s across strategies %s", len(written), session_id, strategies,
    )
    return written


def extract_agent_session(
    agent: LlmAgent,
    *,
    model: str,
    transcript: str,
    principal_id: str,
    session_id: str,
) -> list[Memory]:
    """Run the declared strategies for an agent whose ``memory=`` is attached.

    Uses the agent's own model (its ``model`` override, else ``model``) built the
    same way the compiled agent builds it, and the declared memory store. The
    caller runs as the app service principal; ``principal_id`` is the session's
    recorded user.
    """
    from ._compile import _build_chat_databricks  # noqa: PLC0415

    store = getattr(agent, "_apx_memory_store", None)
    memory = getattr(agent, "_apx_memory_config", None)
    if store is None or memory is None:
        raise ValueError("agent has no attached memory= store; strategy extraction needs one")
    llm = _build_chat_databricks(agent._overrides.get("model", model))
    return extract_strategy_memories(
        store, llm, memory, transcript=transcript, principal_id=principal_id, session_id=session_id,
    )


# One worker: extractions run one at a time, so two triggers for the same session
# never interleave their add / delete passes.
_EXTRACTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apx-memory-extract")
_ATTEMPTS = 3
_RETRY_DELAY_S = 2.0
_ROLES = {"human": "user", "ai": "assistant"}


def session_transcript(messages: Sequence[BaseMessage]) -> str:
    """User and assistant text of a session, one ``role: text`` line per message."""
    return "\n".join(
        f"{_ROLES[m.type]}: {m.text}" for m in messages if m.type in _ROLES and m.text.strip()
    )


def schedule_session_extraction(
    agent: LlmAgent,
    *,
    model: str,
    messages: Sequence[BaseMessage],
    principal_id: str,
    session_id: str,
) -> Future[list[Memory] | None] | None:
    """Queue extraction when the session has just reached a multiple of
    ``memory.extract_every_n_turns`` user turns. Returns at once; ``None`` when not due.

    ``messages`` is the session so far (the graph state after the turn). The
    extraction runs on a background worker as the app service principal and
    writes under ``principal_id``; a failure is retried and logged, never raised
    into the turn.
    """
    memory = getattr(agent, "_apx_memory_config", None)
    every = memory.extract_every_n_turns if memory is not None else None
    turns = sum(1 for m in messages if m.type == "human")
    if every is None or turns == 0 or turns % every:
        return None
    return _EXTRACTOR.submit(
        _deliver, agent, model=model, transcript=session_transcript(messages),
        principal_id=principal_id, session_id=session_id,
    )


def _deliver(
    agent: LlmAgent, *, model: str, transcript: str, principal_id: str, session_id: str,
) -> list[Memory] | None:
    """At-least-once: retry a failed extraction. Re-delivery is safe because each
    run replaces the session's records. Returns ``None`` after the last failure."""
    failure: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            return extract_agent_session(
                agent, model=model, transcript=transcript, principal_id=principal_id, session_id=session_id,
            )
        except Exception as exc:  # the turn already returned; nothing to raise into
            failure = exc
            logger.warning(
                "memory extraction for session %s failed (attempt %d/%d): %s",
                session_id, attempt, _ATTEMPTS, exc,
            )
        if attempt < _ATTEMPTS:
            time.sleep(_RETRY_DELAY_S * attempt)
    logger.error(
        "memory extraction for session %s gave up after %d attempts; no memories written",
        session_id, _ATTEMPTS, exc_info=failure,
    )
    return None


__all__ = [
    "BUILTIN_PROMPTS",
    "OUTPUT_CONTRACTS",
    "extract_agent_session",
    "extract_strategy_memories",
    "parse_strategy_output",
    "schedule_session_extraction",
    "session_transcript",
    "strategy_prompt",
    "strategy_system_prompt",
]
