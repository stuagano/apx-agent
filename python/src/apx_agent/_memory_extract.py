"""Declared session-to-memory extraction through AgentKit.

One finished managed session is distilled into the bound memory store by
``Session.extract_memories``. This is not consolidation of memories that
already exist, and it does not start a cross-session memory pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._memory import new_memory_id
from ._memory_managed import _first_line, validate_memory_store_name
from ._memory_strategies import strategy_prompt

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from databricks_agentkit.session_store import ExtractedMemory

    from ._models import AgentConfig, MemoryBackendConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractedSessionMemory:
    """One entry the service returned. ``written`` is false on a dry run."""

    name: str
    content: str | None
    description: str | None
    path: str | None
    actor_id: str | None
    session_id: str | None
    written: bool


@dataclass(frozen=True)
class SessionExtractionResult:
    """Outcome of one declared extraction. The session itself is untouched."""

    session_store: str
    session_id: str
    memory_store: str
    actor_id: str
    dry_run: bool
    entries: tuple[ExtractedSessionMemory, ...]


def _entry_id(name: str) -> str:
    marker = "/entries/"
    if marker not in name:
        raise ValueError(f"Extracted memory name {name!r} is not an entry resource")
    entry_id = name.rsplit("/", 1)[-1]
    if not entry_id or "/" in entry_id:
        raise ValueError(f"Extracted memory name {name!r} has no entry id")
    return entry_id


def extraction_enabled(config: AgentConfig) -> bool:
    """True for managed memory with an enabled extraction policy, or with declared
    strategies and no policy (an explicit ``enabled: false`` still wins)."""
    memory = config.memory
    if memory is None or memory.type != "managed":
        return False
    if memory.extraction is not None:
        return memory.extraction.enabled
    return bool(memory.strategies)


def _require_store_name(value: str | None, label: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{label} is required")
    return validate_memory_store_name(value)


def _entry(raw: ExtractedMemory, *, written: bool) -> ExtractedSessionMemory:
    return ExtractedSessionMemory(
        name=raw.name,
        content=raw.content,
        description=raw.description,
        path=raw.path,
        actor_id=raw.actor_id,
        session_id=raw.session_id,
        written=written,
    )


def extract_declared_session(
    ws: WorkspaceClient,
    config: AgentConfig,
    *,
    session_id: str,
    actor_id: str | None = None,
    dry_run: bool = False,
) -> SessionExtractionResult:
    """Distill one completed managed session under the declared policy.

    The actor comes from the trusted caller. The service's session record must
    carry the same actor; a mismatch is refused before extraction. A service
    error propagates and the completed session is left as it was.
    """
    memory = config.memory
    session = config.session
    if not extraction_enabled(config) or memory is None:
        raise ValueError(
            "Session extraction is off. Declare memory= "
            "type='managed' with extraction enabled (memory={'type': 'managed', "
            "'extraction': {'enabled': True}})."
        )
    if session is None or session.type != "managed":
        raise ValueError(
            "Session extraction requires session= type='managed'. "
            "The service reads the completed transcript from that store."
        )
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id is required")
    if actor_id is not None and (not isinstance(actor_id, str) or not actor_id.strip()):
        raise ValueError("actor_id must be the trusted caller, not blank")

    instructions = memory.extraction.instructions if memory.extraction is not None else None
    if instructions is not None and not instructions.strip():
        raise ValueError("memory.extraction.instructions must be non-blank text")
    memory_store = _require_store_name(memory.store_name, "memory store_name")
    session_store_name = _require_store_name(session.store_name, "session store_name")

    from databricks_agentkit import AgentKitClient  # noqa: PLC0415

    client = AgentKitClient(ws)
    session_store = client.session_stores.get(session_store_name)
    managed_session = session_store.get(session_id)
    recorded_actor = managed_session.actor_id
    if not isinstance(recorded_actor, str) or not recorded_actor.strip():
        raise ValueError(
            f"Session {session_id!r} has no actor_id; extraction is refused"
        )
    if actor_id is not None and recorded_actor != actor_id:
        raise ValueError(
            f"Session {session_id!r} belongs to actor {recorded_actor!r}, "
            f"not the trusted caller {actor_id!r}"
        )

    if memory.strategies:
        entries = _extract_by_strategy(
            client, managed_session, memory, memory_store=memory_store,
            instructions=instructions, actor=recorded_actor, dry_run=dry_run,
        )
        return SessionExtractionResult(
            session_store=session_store_name, session_id=session_id, memory_store=memory_store,
            actor_id=recorded_actor, dry_run=dry_run, entries=tuple(entries),
        )

    extracted = managed_session.extract_memories(
        memory_store=memory_store,
        instructions=instructions,
        dry_run=dry_run,
    )
    if not dry_run:
        memory_resource = client.memory_stores.get(memory_store)
        for item in extracted:
            entry_id = _entry_id(item.name)
            # The service claims a write. Read it back before reporting success.
            read_back = memory_resource.get(entry_id, read_mask="*")
            if (
                read_back.content != item.content
                or read_back.actor_id != recorded_actor
                or read_back.path != item.path
                or read_back.session_id != managed_session.session_id
            ):
                raise ValueError(
                    f"Extraction reported {item.name!r} but the memory store "
                    "read-back did not match the session or actor"
                )
    entries = tuple(_entry(item, written=not dry_run) for item in extracted)
    logger.info(
        "Extracted %d memories from session %s into %s (dry_run=%s)",
        len(entries), session_id, memory_store, dry_run,
    )
    return SessionExtractionResult(
        session_store=session_store_name,
        session_id=session_id,
        memory_store=memory_store,
        actor_id=recorded_actor,
        dry_run=dry_run,
        entries=entries,
    )


def _extract_by_strategy(
    client: Any,
    managed_session: Any,
    memory: MemoryBackendConfig,
    *,
    memory_store: str,
    instructions: str | None,
    actor: str,
    dry_run: bool,
) -> list[ExtractedSessionMemory]:
    """M2 (#1088): one dry-run extraction per strategy, then apx writes each entry
    at ``/memories/<strategy>/<id>`` so the namespace is exact. Every strategy is
    extracted and checked before the first write; each write is read back."""
    strategies = memory.strategies if memory.strategies is not None else []
    planned: list[ExtractedSessionMemory] = []
    for strategy in strategies:
        prompt = "\n\n".join(p for p in (instructions, strategy_prompt(memory, strategy)) if p)
        found = managed_session.extract_memories(
            memory_store=memory_store, instructions=prompt, dry_run=True,
        )
        if strategy == "summary" and len(found) != 1:
            raise ValueError(f"memory strategy 'summary' must yield exactly one entry, got {len(found)}")
        for item in found:
            if item.content is None or not item.content.strip():
                raise ValueError(f"memory strategy {strategy!r} returned an entry with no content")
            planned.append(ExtractedSessionMemory(
                name=item.name, content=item.content, description=item.description,
                path=f"/memories/{strategy}/", actor_id=actor,
                session_id=managed_session.session_id, written=False,
            ))
    if dry_run:
        return planned

    resource = client.memory_stores.get(memory_store)
    if any(p.path == "/memories/summary/" for p in planned):
        # Re-extracting a session replaces its summary instead of adding a second.
        for stale in resource.list(actor_id=actor, path_prefix="/memories/summary/",
                                   session_id=managed_session.session_id, read_mask="*"):
            if stale.actor_id == actor and stale.session_id == managed_session.session_id:
                stale.delete()
    written: list[ExtractedSessionMemory] = []
    for entry in planned:
        content = str(entry.content)
        added = resource.add(
            actor_id=actor, path=f"{entry.path}{new_memory_id()}", content=content,
            description=_first_line(content), session_id=managed_session.session_id,
        )
        read_back = resource.get(added.id, read_mask="*")
        if (
            read_back.content != content
            or read_back.actor_id != actor
            or read_back.path != added.path
            or read_back.session_id != managed_session.session_id
        ):
            raise ValueError(
                f"Strategy extraction wrote {added.name!r} but the memory store "
                "read-back did not match the session or actor"
            )
        written.append(ExtractedSessionMemory(
            name=added.name, content=content, description=added.description, path=added.path,
            actor_id=actor, session_id=managed_session.session_id, written=True,
        ))
    return written


def extraction_payload(result: SessionExtractionResult) -> dict[str, Any]:
    """JSON-ready view of an extraction. ``written`` is per entry, not assumed."""
    return {
        "session_store": result.session_store,
        "session_id": result.session_id,
        "memory_store": result.memory_store,
        "actor_id": result.actor_id,
        "dry_run": result.dry_run,
        "written": not result.dry_run,
        "entries": [
            {
                "name": entry.name,
                "content": entry.content,
                "description": entry.description,
                "path": entry.path,
                "actor_id": entry.actor_id,
                "session_id": entry.session_id,
                "written": entry.written,
            }
            for entry in result.entries
        ],
    }
