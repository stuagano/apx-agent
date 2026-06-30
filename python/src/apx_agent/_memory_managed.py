"""ManagedMemoryStore — Databricks **Managed Agent Memory** as a :class:`MemoryStore`.

Managed Agent Memory (GA) is a Unity Catalog *memory store* securable, driven
entirely through the UC REST API (there is no Python SDK for it). This backend
adapts that REST surface to apx-agent's :class:`~apx_agent._memory.MemoryStore`
protocol so the existing ``recall`` / ``remember`` / ``forget`` tools and
``[tool.apx.agent.memory]`` wiring work unchanged — only the backend differs.

REST surface (base ``/api/2.1/unity-catalog/memory-stores/{store}/entries``):

  * create  ``POST   /entries?scope=…``      body ``{path, contents, description}``
  * search  ``POST   /entries:search?scope=…`` body ``{query}`` → relevance-ranked
  * get     ``GET    /entries:get?scope=…&path=…`` → ``{contents, description, …}``
  * list    ``GET    /entries?scope=…``       → ``{entries: [{path, description, has_contents}]}``
  * update  ``PATCH  /entries``               body ``{scope, path, [description], [edit op]}``
  * delete  ``DELETE /entries?scope=…&path=…``

Mapping onto :class:`~apx_agent._memory.Memory`:

  * ``scope``      ← ``principal_id`` (per-user isolation boundary)
  * ``path``       ← ``/{namespace}/{uuid}`` and IS the memory ``id``
  * ``contents``   ← ``content``
  * ``description``← first line of ``content`` (the store uses it to improve
    retrieval; the entry model has no separate summary field)

**Lossy by design** — the managed entry model has no field for ``tags``,
``importance``, or ``metadata``. This backend does not persist them; on read
they come back as ``()`` / ``0.5`` / ``{}``. ``recall`` therefore **ignores**
the ``tags`` and ``min_importance`` filters rather than silently returning an
empty set (callers needing tag/importance filtering should use the Delta or
Lakebase backend).

**Scope is never model-supplied.** ``add`` / ``recall`` / ``list`` take the
trusted ``principal_id`` from their (dependency-injected) arguments. The
id-only ``get`` / ``update`` / ``delete`` resolve scope through the injected
``scope_resolver`` (wired to the per-request OBO principal). With no resolver
and no resolvable scope they **fail closed** — never cross the isolation
boundary by trusting an id the model handed back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from ._memory import (
    Memory,
    MemoryFilter,
    MemoryStore,
    RecallOptions,
    RecallResult,
    iso_now,
    new_memory_id,
    validate_table_name,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class ApiCaller(Protocol):
    """Duck-typed Databricks REST caller — the shape of ``ws.api_client``.

    Injected so the caller owns auth (user-scoped OBO vs. service principal),
    the host, and the transport (real SDK client, REST shim, or test fake).
    No Databricks SDK is imported here.
    """

    def do(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Execute one REST call and return the decoded JSON (a dict)."""
        ...


def _first_line(text: str, *, limit: int = 120) -> str:
    """A one-line description derived from ``content`` for retrieval.

    The managed entry has no summary field of its own, so the first non-empty
    line of the content (truncated) stands in. Never empty — the store rejects
    an entry with neither contents nor description.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return text[:limit] or "(memory)"


class ManagedMemoryStore:
    """Databricks Managed Agent Memory (UC memory store) as a ``MemoryStore``.

    See the module docstring for the REST surface, the field mapping, and the
    two deliberate constraints (lossy tags/importance; scope never trusted from
    a model-supplied id).
    """

    def __init__(
        self,
        *,
        api: ApiCaller,
        store_name: str,
        scope_resolver: Callable[[], str | None] | None = None,
    ) -> None:
        """Construct a managed-memory store.

        Args:
            api: Databricks REST caller (``ws.api_client``-shaped). All calls
                flow through ``api.do(method, path, query=..., body=...)``.
            store_name: The UC memory store's three-part name
                (``catalog.schema.name``). Validated as a safe identifier and
                interpolated into the request path.
            scope_resolver: Trusted, zero-arg callable returning the current
                principal (OBO user id) — used only by the id-only ``get`` /
                ``update`` / ``delete`` methods. ``None`` (or a ``None``
                return) makes those methods fail closed.
        """
        validate_table_name(store_name)
        self._api = api
        self._store = store_name
        self._scope_resolver = scope_resolver

    # -- request helpers ----------------------------------------------------

    def _entries(self) -> str:
        """The entries endpoint path for this store (callers append ``:get`` etc.)."""
        return f"/api/2.1/unity-catalog/memory-stores/{self._store}/entries"

    def _trusted_scope(self) -> str | None:
        """Resolve the current scope from the trusted resolver, or ``None``."""
        if self._scope_resolver is None:
            return None
        return self._scope_resolver()

    # -- write --------------------------------------------------------------

    def add(self, memory: Mapping[str, Any]) -> Memory:
        """Create one entry; ``id`` is the entry ``path``.

        ``scope`` comes from the (trusted) ``principal_id`` in ``memory``.
        ``tags`` / ``importance`` / ``metadata`` are accepted but **not
        persisted** (the managed entry model has no field for them), so the
        returned row carries their defaults — an ``add`` then ``get`` round-trip
        stays consistent rather than claiming values the store dropped.
        """
        scope = str(memory["principal_id"])
        namespace = str(memory.get("namespace") or "default")
        content = str(memory["content"])
        leaf = new_memory_id()
        path = f"/{namespace}/{leaf}"
        self._api.do(
            "POST",
            self._entries(),
            query={"scope": scope},
            body={
                "path": path,
                "contents": content,
                "description": _first_line(content),
            },
        )
        return self._entry_to_memory(
            {"contents": content}, scope=scope, path=path
        )

    def add_batch(self, memories: Sequence[Mapping[str, Any]]) -> list[Memory]:
        """Insert many entries — one POST per entry, in input order."""
        return [self.add(m) for m in memories]

    # -- id-only reads/mutations (scope from the trusted resolver) ----------

    def get(self, memory_id: str) -> Memory | None:
        """Fetch the entry at ``path == memory_id`` within the resolved scope.

        Fails closed (returns ``None``) when no trusted scope is available —
        scope is never taken from ``memory_id``.
        """
        scope = self._trusted_scope()
        if not scope:
            return None
        entry = self._api.do(
            "GET",
            self._entries() + ":get",
            query={"scope": scope, "path": memory_id},
        )
        if not entry:
            return None
        return self._entry_to_memory(entry, scope=scope, path=memory_id)

    def update(self, memory_id: str, patch: Mapping[str, Any]) -> Memory | None:
        """Update the entry's contents/description within the resolved scope.

        Only ``content`` is persistable (PATCH replaces contents and refreshes
        the derived description); ``tags`` / ``importance`` / ``namespace``
        changes are ignored. Fails closed without a trusted scope.
        """
        scope = self._trusted_scope()
        if not scope:
            return None
        existing = self.get(memory_id)
        if existing is None:
            return None
        if "content" not in patch:
            return existing
        content = str(patch["content"])
        self._api.do(
            "PATCH",
            self._entries(),
            body={
                "scope": scope,
                "path": memory_id,
                "description": _first_line(content),
                "replace_all": content,
            },
        )
        return Memory(
            id=existing.id,
            principal_id=existing.principal_id,
            namespace=existing.namespace,
            content=content,
            tags=existing.tags,
            importance=existing.importance,
            embedding=None,
            metadata=existing.metadata,
            created_at=existing.created_at,
            updated_at=iso_now(),
        )

    def delete(self, memory_id: str) -> bool:
        """Delete the entry at ``path == memory_id`` within the resolved scope.

        Returns ``False`` (no cross-scope action) when scope is unresolvable.
        """
        scope = self._trusted_scope()
        if not scope:
            return False
        self._api.do(
            "DELETE",
            self._entries(),
            query={"scope": scope, "path": memory_id},
        )
        return True

    # -- list / recall (scope from the trusted caller arg) ------------------

    def list(self, filter: MemoryFilter) -> list[Memory]:
        """List entries for ``filter.principal_id``, newest first, capped at limit.

        Namespace is honored as a ``/{namespace}/`` path-prefix filter.
        ``tags`` / ``min_importance`` are not supported by the managed model
        and are ignored. Infra errors propagate (an empty result means a
        genuinely empty store, never a swallowed failure).
        """
        scope = filter.principal_id
        resp = self._api.do("GET", self._entries(), query={"scope": scope})
        entries = (resp or {}).get("entries") or []
        prefix = f"/{filter.namespace}/" if filter.namespace is not None else None
        out: list[Memory] = []
        for e in entries:
            path = str(e.get("path") or "")
            if prefix is not None and not path.startswith(prefix):
                continue
            out.append(self._entry_to_memory(e, scope=scope, path=path))
            if len(out) >= int(filter.limit):
                break
        return out

    def recall(self, opts: RecallOptions) -> list[RecallResult]:
        """Semantic recall via ``entries:search`` for ``opts.principal_id``.

        ``namespace`` is applied as a path-prefix post-filter. ``tags`` /
        ``min_importance`` are **ignored** (the managed model has no such
        fields) — never used to post-filter, which would wrongly empty the
        result. Infra errors propagate.
        """
        scope = opts.principal_id
        resp = self._api.do(
            "POST",
            self._entries() + ":search",
            query={"scope": scope},
            body={"query": opts.query},
        )
        rows = (resp or {}).get("entries") or (resp or {}).get("results") or []
        prefix = f"/{opts.namespace}/" if opts.namespace is not None else None
        out: list[RecallResult] = []
        for row in rows:
            path = str(row.get("path") or "")
            if prefix is not None and not path.startswith(prefix):
                continue
            score = (
                row.get("score")
                if row.get("score") is not None
                else row.get("_score")
                if row.get("_score") is not None
                else 0.0
            )
            out.append(
                RecallResult(
                    memory=self._entry_to_memory(row, scope=scope, path=path),
                    score=float(score or 0.0),
                )
            )
            if len(out) >= int(opts.k):
                break
        return out

    # -- mapping ------------------------------------------------------------

    def _entry_to_memory(
        self, entry: Mapping[str, Any], *, scope: str, path: str
    ) -> Memory:
        """Reconstruct a :class:`Memory` from a UC entry dict.

        ``content`` prefers ``contents``, falling back to ``description`` (a
        brief memory may live entirely in its description). ``tags`` /
        ``importance`` / ``metadata`` carry their defaults — the managed store
        does not persist them.
        """
        content = str(entry.get("contents") or entry.get("description") or "")
        namespace = path[1:].split("/", 1)[0] if path.startswith("/") else "default"
        now = iso_now()
        return Memory(
            id=path,
            principal_id=scope,
            namespace=namespace or "default",
            content=content,
            tags=(),
            importance=0.5,
            embedding=None,
            metadata={},
            created_at=str(entry.get("created_at") or now),
            updated_at=str(entry.get("updated_at") or now),
        )


# Force the runtime check at import time — surfaces protocol drift early.
_: type[MemoryStore] = ManagedMemoryStore


__all__ = [
    "ApiCaller",
    "ManagedMemoryStore",
]
