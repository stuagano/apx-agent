"""Declared session-to-memory extraction through AgentKit.

One finished managed session is distilled into the bound memory store by
``Session.extract_memories``. This is not consolidation of memories that
already exist, and it does not start a cross-session memory pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._memory_managed import validate_memory_store_name

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from databricks_agentkit.session_store import ExtractedMemory

    from ._models import AgentConfig

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
    """True only for an explicit enabled managed-memory extraction policy."""
    memory = config.memory
    policy = None if memory is None else memory.extraction
    return memory is not None and memory.type == "managed" and policy is not None and policy.enabled


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
    if not extraction_enabled(config) or memory is None or memory.extraction is None:
        raise ValueError(
            "Session extraction is off. Declare [tool.apx.agent.memory] "
            "type='managed' with [tool.apx.agent.memory.extraction] enabled=true."
        )
    if session is None or session.type != "managed":
        raise ValueError(
            "Session extraction requires [tool.apx.agent.session] type='managed'. "
            "The service reads the completed transcript from that store."
        )
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id is required")
    if actor_id is not None and (not isinstance(actor_id, str) or not actor_id.strip()):
        raise ValueError("actor_id must be the trusted caller, not blank")

    instructions = memory.extraction.instructions
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
