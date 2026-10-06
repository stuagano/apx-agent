"""Workspace-scoped managed memory through AgentKit.

Actor IDs partition memories; store permissions are the security boundary.
Tags, importance and metadata are not persisted by the service.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from itertools import islice
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ._memory import Memory, MemoryFilter, MemoryStore, RecallOptions, RecallResult, iso_now, new_memory_id

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from databricks_agentkit.memory_store import Memory as ManagedMemory

logger = logging.getLogger(__name__)


@runtime_checkable
class ApiCaller(Protocol):
    """Databricks transport protocol, retained for existing public imports."""

    def do(self, method: str, path: str, *, query: dict[str, Any] | None = None,
           body: dict[str, Any] | None = None) -> Any: ...


def validate_memory_store_name(name: str) -> str:
    """Reject legacy UC names rather than silently redirecting their data."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,54}[a-z0-9]", name):
        raise ValueError(
            "Managed memory store_name must be a workspace display name "
            "(3-56 lowercase letters, numbers or hyphens). Legacy UC "
            "catalog.schema.name stores require explicit data migration."
        )
    return name


def _first_line(text: str) -> str:
    return next((line.strip()[:120] for line in text.splitlines() if line.strip()), "(memory)")


def _is_not_found(exc: Exception) -> bool:
    from databricks.sdk.errors import NotFound  # noqa: PLC0415

    return isinstance(exc, NotFound) or isinstance(exc.__cause__, NotFound) or getattr(
        exc, "error_code", None
    ) in {"NOT_FOUND", "RESOURCE_DOES_NOT_EXIST"}


class ManagedMemoryStore:
    """Bind an existing AgentKit store using an explicit workspace client."""

    def __init__(self, *, ws: WorkspaceClient, store_name: str,
                 scope_resolver: Callable[[], str | None] | None = None) -> None:
        from databricks_agentkit import AgentKitClient  # noqa: PLC0415

        self._store = validate_memory_store_name(store_name)
        self._client = AgentKitClient(workspace_client=ws)
        self._scope_resolver = scope_resolver

    def _resource(self) -> Any:
        return self._client.memory_stores.get(self._store)

    def store_exists(self) -> bool:
        """Probe access without provisioning or hiding a degraded backend."""
        try:
            self._resource()
        except Exception as exc:
            logger.warning("Managed memory store %s not reachable: %s", self._store, exc)
            return False
        return True

    def _trusted_scope(self) -> str | None:
        return self._scope_resolver() if self._scope_resolver else None

    def add(self, memory: Mapping[str, Any]) -> Memory:
        actor = memory["principal_id"]
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("Managed memory requires a trusted principal_id")
        content = str(memory["content"])
        namespace = str(memory.get("namespace") or "default")
        entry = self._resource().add(
            actor_id=actor, path=f"/memories/{namespace}/{new_memory_id()}",
            content=content, description=_first_line(content),
        )
        return self._to_memory(entry)

    def add_batch(self, memories: Sequence[Mapping[str, Any]]) -> list[Memory]:
        return [self.add(memory) for memory in memories]

    def _owned_entry(self, memory_id: str) -> ManagedMemory | None:
        actor = self._trusted_scope()
        if not actor or not actor.strip():
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]+", memory_id):
            raise ValueError("Expected a managed memory entry ID, not a path")
        try:
            entry = self._resource().get(memory_id, read_mask="*")
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        # GET-by-ID is not actor scoped. Check before exposing or mutating data.
        return entry if entry.actor_id == actor else None

    def get(self, memory_id: str) -> Memory | None:
        entry = self._owned_entry(memory_id)
        return self._to_memory(entry) if entry is not None else None

    def update(self, memory_id: str, patch: Mapping[str, Any]) -> Memory | None:
        entry = self._owned_entry(memory_id)
        if entry is None:
            return None
        if "content" in patch:
            content = str(patch["content"])
            entry = entry.update(content=content, description=_first_line(content))
        return self._to_memory(entry)

    def delete(self, memory_id: str) -> bool:
        entry = self._owned_entry(memory_id)
        if entry is None:
            return False
        entry.delete()
        return True

    def list(self, filter: MemoryFilter) -> list[Memory]:
        if not filter.principal_id.strip():
            raise ValueError("Managed memory requires a trusted principal_id")
        prefix = f"/memories/{filter.namespace}/" if filter.namespace is not None else None
        entries = self._resource().list(actor_id=filter.principal_id, path_prefix=prefix, read_mask="*")
        return [self._to_memory(entry) for entry in islice(entries, max(0, filter.limit))
                if entry.actor_id == filter.principal_id]

    def recall(self, opts: RecallOptions) -> list[RecallResult]:
        """BM25 retrieval: at most 100 results, with no search pagination."""
        if not opts.principal_id.strip():
            raise ValueError("Managed memory requires a trusted principal_id")
        if opts.k <= 0:
            return []
        prefix = f"/memories/{opts.namespace}/" if opts.namespace is not None else None
        results = self._resource().search(
            actor_id=opts.principal_id, query=opts.query, limit=min(opts.k, 100),
            path_prefix=prefix, read_mask="*",
        )
        return [RecallResult(memory=self._to_memory(row.memory), score=float(row.score or 0.0))
                for row in results if row.memory.actor_id == opts.principal_id]

    @staticmethod
    def _to_memory(entry: ManagedMemory) -> Memory:
        segments = entry.path.split("/")
        namespace = segments[2] if len(segments) >= 4 and segments[1] == "memories" else "default"
        now = iso_now()
        return Memory(
            id=entry.id, principal_id=entry.actor_id, namespace=namespace,
            content=entry.content if entry.content is not None else (entry.description or ""),
            tags=(), importance=0.5, embedding=None, metadata={},
            created_at=entry.create_time.isoformat() if entry.create_time else now,
            updated_at=entry.update_time.isoformat() if entry.update_time else now,
        )


_: type[MemoryStore] = ManagedMemoryStore


def provision_managed_memory(
    ws: WorkspaceClient, store_name: str, *, description: str = "apx-agent managed agent memory",
) -> str:
    """Explicit provisioning: create only on 404, never on permission errors."""
    from databricks_agentkit import AgentKitClient  # noqa: PLC0415

    validate_memory_store_name(store_name)
    client = AgentKitClient(workspace_client=ws)
    try:
        client.memory_stores.get(store_name)
    except Exception as exc:
        if not _is_not_found(exc):
            raise
        client.memory_stores.create(store_name, description=description)
        status = f"Created memory store {store_name}."
    else:
        status = f"Memory store {store_name} already exists."
    return status + "\nGrant the serving principal access with memory_store.grant_permission(principal_id)."


__all__ = ["ApiCaller", "ManagedMemoryStore", "provision_managed_memory"]
