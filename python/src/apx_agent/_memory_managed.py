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
  * ``path``       ← ``/memories/{namespace}/{uuid}`` and IS the memory ``id``
    (the store requires paths under ``/memories/``)
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
from collections.abc import Callable, Iterator, Mapping, Sequence
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

# Hard backstop on REST pagination: bounds the pathological case (a very
# selective namespace filter over a huge store) and any API that hands back a
# fresh token forever. Realistic stores fit in a handful of pages.
_MANAGED_MAX_PAGES = 1000


def _is_not_found(exc: Exception) -> bool:
    """True when *exc* is the REST API's not-found signal, not a real failure.

    The managed memory REST API surfaces a missing entry as an EXCEPTION (no
    distinct return value), so ``get()`` must tell that apart from a genuine
    infra error (auth, timeout, 5xx) — conflating them turns a transient
    failure into a false "not found" (#486).

    ``ApiCaller`` is deliberately SDK-free (a duck-typed ``ws.api_client``), so
    this only imports ``databricks.sdk.errors.NotFound`` lazily, on the error
    path, and falls back to a duck-typed class-name check for any other
    caller-supplied transport (e.g. a test fake) that raises its own
    NotFound-shaped exception.
    """
    try:
        from databricks.sdk.errors import NotFound  # noqa: PLC0415

        if isinstance(exc, NotFound):
            return True
    except ImportError:
        pass
    return type(exc).__name__ == "NotFound"


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

    def store_exists(self) -> bool:
        """Return ``True`` if the UC memory store exists and is reachable.

        Boot-time probe so an un-provisioned (or ungranted) store fails **loud**
        — ``/readyz`` degraded — instead of silently no-op'ing at recall time.
        Any failure (404 missing, 403 perms, transport) is treated as "not
        usable" and returns ``False``; the cause is logged.
        """
        try:
            resp = self._api.do(
                "GET", f"/api/2.1/unity-catalog/memory-stores/{self._store}"
            )
        except Exception as exc:
            logger.warning(
                "ManagedMemoryStore: memory store %s not reachable: %s",
                self._store, exc,
            )
            return False
        return resp is not None

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
        # The managed store requires entry paths under /memories/ (verified
        # live: a path outside it is rejected with "path must start with
        # /memories/"). apx's namespace becomes the folder under it.
        path = f"/memories/{namespace}/{leaf}"
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
        scope is never taken from ``memory_id``. A not-found REST error also
        returns ``None`` (the protocol contract is None-for-missing) — but any
        OTHER exception (infra error) propagates, so a transient failure is
        never misreported as "not found" (#486).
        """
        scope = self._trusted_scope()
        if not scope:
            return None
        try:
            entry = self._api.do(
                "GET",
                self._entries() + ":get",
                query={"scope": scope, "path": memory_id},
            )
        except Exception as exc:
            if not _is_not_found(exc):
                raise
            logger.debug("ManagedMemoryStore.get(%s): not found.", memory_id)
            return None
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
        """List entries for ``filter.principal_id`` in the store's REST order.

        The list endpoint does not expose per-entry timestamps, so results are
        returned in whatever order the API yields — this backend cannot promise
        "newest first" the way the SQL backends do. ``namespace`` is honored as
        a ``/{namespace}/`` path-prefix filter; ``tags`` / ``min_importance``
        are not supported by the managed model and are ignored. Infra errors
        propagate (an empty result means a genuinely empty store).

        Pages through ``next_page_token`` so a namespace filter can't miss
        matches that live beyond the first page. (Pagination follows the
        Databricks ``page_token``/``next_page_token`` convention; re-verify
        against the live managed API when it stabilises. The dedup + unchanged-
        token guard make an unsupported/ignored token degrade to a single page,
        i.e. the pre-pagination behaviour — never an infinite loop or dupes.)
        """
        scope = filter.principal_id
        prefix = f"/memories/{filter.namespace}/" if filter.namespace is not None else None
        out: list[Memory] = []
        for entry in self._iter_entries("GET", self._entries(), {"scope": scope}, "entries"):
            path = str(entry.get("path") or "")
            if prefix is not None and not path.startswith(prefix):
                continue
            out.append(self._entry_to_memory(entry, scope=scope, path=path))
            if len(out) >= int(filter.limit):
                break
        return out

    def recall(self, opts: RecallOptions) -> list[RecallResult]:
        """Semantic recall via ``entries:search`` for ``opts.principal_id``.

        ``namespace`` is applied as a path-prefix post-filter. ``tags`` /
        ``min_importance`` are **ignored** (the managed model has no such
        fields) — never used to post-filter, which would wrongly empty the
        result. Infra errors propagate.

        Pages through ``next_page_token`` (same convention/guards as
        :meth:`list`) so a namespace post-filter can't under-return by dropping
        matches that ranked onto a later page.
        """
        scope = opts.principal_id
        # Verified live: scope AND query are query params (a body `query` is
        # ignored → empty). Results come back as {results: [{memory_entry: {…},
        # score: <float>}]}.
        prefix = f"/memories/{opts.namespace}/" if opts.namespace is not None else None
        out: list[RecallResult] = []
        for row in self._iter_entries(
            "POST", self._entries() + ":search", {"scope": scope, "query": opts.query}, "results"
        ):
            entry = row.get("memory_entry") or {}
            path = str(entry.get("path") or "")
            if prefix is not None and not path.startswith(prefix):
                continue
            score = row.get("score")
            out.append(
                RecallResult(
                    memory=self._entry_to_memory(entry, scope=scope, path=path),
                    score=float(score or 0.0),
                )
            )
            if len(out) >= int(opts.k):
                break
        return out

    def _iter_entries(
        self, method: str, path: str, query: Mapping[str, Any], key: str
    ) -> "Iterator[Mapping[str, Any]]":
        """Yield items across all REST pages, following ``next_page_token``.

        Deduplicates by the item's ``path`` and stops when the token repeats or
        is absent, so an API that ignores ``page_token`` degrades to a single
        page (no dupes, no infinite loop). A hard page cap backstops both.
        """
        seen: set[str] = set()
        page_token: str | None = None
        for _ in range(_MANAGED_MAX_PAGES):
            q = dict(query)
            if page_token:
                q["page_token"] = page_token
            resp = self._api.do(method, path, query=q) or {}
            items = resp.get(key) or []
            for item in items:
                entry = item.get("memory_entry") if key == "results" else item
                item_path = str((entry or {}).get("path") or "")
                if item_path and item_path in seen:
                    continue
                if item_path:
                    seen.add(item_path)
                yield item
            next_token = resp.get("next_page_token")
            if not next_token or next_token == page_token or not items:
                return
            page_token = next_token

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
        # path is /memories/{namespace}/{leaf}; recover the namespace folder.
        segments = [s for s in path.split("/") if s]
        namespace = (
            segments[1] if len(segments) >= 2 and segments[0] == "memories" else "default"
        )
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
            created_at=str(entry.get("create_time") or entry.get("created_at") or now),
            updated_at=str(entry.get("update_time") or entry.get("updated_at") or now),
        )


# Force the runtime check at import time — surfaces protocol drift early.
_: type[MemoryStore] = ManagedMemoryStore


def provision_managed_memory(
    api: ApiCaller,
    store_name: str,
    *,
    description: str = "apx-agent managed agent memory",
) -> str:
    """Create the UC memory store ``store_name`` (idempotent). Deploy/admin step.

    This is **admin provisioning by the developer at deploy time**, not a
    runtime action — the served app principal won't hold ``CREATE MEMORY
    STORE``, so apx never auto-creates the store on first write (that would
    silently degrade). Mirrors ``_publish.py``'s ``CREATE SCHEMA`` precedent.

    Idempotent via :meth:`ManagedMemoryStore.store_exists` — re-running is a
    no-op when the store is already present.

    Grants are intentionally NOT executed here (the grantee — usually the app's
    service principal — and the privilege set are deploy-specific). The returned
    status names the grants the serving principal needs.

    :param api: Databricks REST caller (``ws.api_client``), with admin rights.
    :param store_name: Three-part ``catalog.schema.name`` of the store.
    :returns: A human-readable status string (created / already-exists) plus the
        grant the serving principal requires.
    """
    validate_table_name(store_name)
    parts = store_name.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"store_name must be a 3-part 'catalog.schema.name'; got {store_name!r}"
        )
    catalog, schema, name = parts

    grant_hint = (
        f"\nGrant the serving principal access:\n"
        f"  GRANT READ MEMORY STORE, WRITE MEMORY STORE ON {store_name} "
        f"TO `<app-service-principal>`;"
    )

    if ManagedMemoryStore(api=api, store_name=store_name).store_exists():
        return f"Memory store {store_name} already exists.{grant_hint}"

    api.do(
        "POST",
        "/api/2.1/unity-catalog/memory-stores",
        body={
            "name": name,
            "catalog_name": catalog,
            "schema_name": schema,
            "description": description,
        },
    )
    return f"Created memory store {store_name}.{grant_hint}"


__all__ = [
    "ApiCaller",
    "ManagedMemoryStore",
    "provision_managed_memory",
]
