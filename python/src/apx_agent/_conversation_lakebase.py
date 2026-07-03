"""LakebaseConversationStore — Postgres-backed conversation persistence over Databricks Lakebase.

Lakebase is Databricks' managed Postgres. Compared to ``DeltaConversationStore``:

  * Lower per-operation latency (Postgres point lookups vs. SQL warehouse round-trips).
  * Better for high-frequency turns (chat UIs, interactive agents).
  * Worse for cheap durability across long-idle sessions (Delta scales storage
    independently; Lakebase charges for the running instance).

Both stores satisfy the same :class:`~apx_agent._conversation.ConversationStore` interface.
Pick by workload: chat-style interactive agents → Lakebase; analytics-style
multi-step pipelines → Delta.

This store takes a SQLAlchemy ``Engine`` rather than embedding token-rotation logic.
The caller wires up the engine with whatever auth pattern they prefer — typically a
``do_connect`` event listener that mints fresh OAuth tokens via
``ws.database.generate_database_credential()``. See :func:`~apx_agent.build_lakebase_engine`.

Schema (created on first use when ``auto_create=True``)::

    CREATE TABLE IF NOT EXISTS {conversations_table} (
        conversation_id        TEXT   PRIMARY KEY,
        root_conversation_id   TEXT   NOT NULL,
        parent_conversation_id TEXT,
        kind                   TEXT   NOT NULL,
        title                  TEXT,
        agent_id               TEXT,
        session_state          TEXT   NOT NULL,   -- JSON
        session_usage          TEXT   NOT NULL,   -- JSON
        reasoning_effort       TEXT,
        model_override         TEXT,
        created_at             BIGINT NOT NULL,   -- epoch ms
        updated_at             BIGINT NOT NULL    -- epoch ms
    );

    CREATE TABLE IF NOT EXISTS {items_table} (
        item_id         TEXT   PRIMARY KEY,
        conversation_id TEXT   NOT NULL,
        position        BIGINT NOT NULL,           -- 0-based per conversation
        type            TEXT   NOT NULL,
        status          TEXT   NOT NULL,
        response_id     TEXT   NOT NULL,
        data            TEXT   NOT NULL,           -- JSON
        search_text     TEXT,                      -- denormalised for ILIKE search
        created_by      TEXT,
        created_at      BIGINT NOT NULL            -- epoch ms
    );

Requires the ``lakebase`` extra::

    pip install 'apx-agent[lakebase]'
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ._conversation import (
    Conversation,
    ConversationItem,
    ConversationNotFoundError,
    ConversationStore,
    NewConversationItem,
    PagedList,
    _new_id,
    _now_ms,
    compute_search_text,
    paginate_in_memory,
    parse_item_data,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_SQLALCHEMY_MISSING = (
    "LakebaseConversationStore requires SQLAlchemy. "
    "Install with: pip install 'apx-agent[lakebase]'"
)


def _ilike_escape(value: str) -> str:
    """Escape ILIKE wildcards so a literal ``%`` or ``_`` in a query stays literal.

    Postgres ILIKE uses ``%``/``_`` as wildcards and ``\\`` as the default escape
    character, so a bare ``%`` in a user query would otherwise match anything.
    Mirrors the Delta store's ``_like_escape`` (:mod:`apx_agent._conversation_delta`).
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _require_sqlalchemy() -> Any:
    """
    Import and return sqlalchemy, raising a friendly error if absent.

    :returns: The ``sqlalchemy`` module.
    :raises ImportError: If sqlalchemy is not installed.
    """
    try:
        import sqlalchemy  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(_SQLALCHEMY_MISSING) from exc
    return sqlalchemy


class LakebaseConversationStore(ConversationStore):
    """
    :class:`ConversationStore` backed by two Postgres tables on Databricks Lakebase.

    :param engine: SQLAlchemy ``Engine`` connected to the Lakebase instance. The
        caller is responsible for token rotation — typically a ``do_connect``
        listener that calls ``ws.database.generate_database_credential()``.
        Use :func:`~apx_agent.build_lakebase_engine` to build one.
    :param conversations_table: Table name for conversation rows, e.g.
        ``"public.apx_conversations"``. Inlined as a SQL identifier (not a bind
        param), so it must be a trusted, developer-supplied name — never user input.
    :param items_table: Table name for item rows, e.g. ``"public.apx_conv_items"``.
    :param auto_create: When ``True`` (default), issues ``CREATE TABLE IF NOT EXISTS``
        on first use. Set ``False`` when tables are provisioned out-of-band.

    Example::

        from apx_agent import build_lakebase_engine, LakebaseConversationStore

        engine = build_lakebase_engine(
            ws=ws, database="agentdb",
            host="ep-xxxx.database.us-east-1.cloud.databricks.com",
        )
        store = LakebaseConversationStore(engine=engine)
    """

    def __init__(
        self,
        *,
        engine: "Engine",
        conversations_table: str = "apx_conversations",
        items_table: str = "apx_conversation_items",
        auto_create: bool = True,
    ) -> None:
        """
        Initialize the Lakebase conversation store.

        :param engine: SQLAlchemy Engine for the Lakebase Postgres instance.
        :param conversations_table: Name of the conversations table.
        :param items_table: Name of the conversation items table.
        :param auto_create: Create tables on first use when ``True``.
        """
        _require_sqlalchemy()
        super().__init__(storage_location=f"lakebase://{conversations_table}")
        self.engine = engine
        self._conv_table = conversations_table
        self._items_table = items_table
        self._auto_create = auto_create
        self._tables_created = False

    # ── Table creation ────────────────────────────────────────────────────────

    def _ensure_tables(self) -> None:
        """Issue ``CREATE TABLE IF NOT EXISTS`` on first call. No-op thereafter.

        Only latches as created when BOTH DDLs succeed, so a transient or
        permission DDL failure self-heals on the next call instead of being
        permanently skipped by the early-return above.
        """
        if self._tables_created or not self._auto_create:
            return
        sa = _require_sqlalchemy()
        ok_conv = self._run_ddl(sa, self._ddl_conversations())
        ok_items = self._run_ddl(sa, self._ddl_items())
        self._tables_created = ok_conv and ok_items

    def _ddl_conversations(self) -> str:
        """Return CREATE TABLE DDL for the conversations table."""
        return (
            f"CREATE TABLE IF NOT EXISTS {self._conv_table} ("
            f"  conversation_id        TEXT   PRIMARY KEY,"
            f"  root_conversation_id   TEXT   NOT NULL,"
            f"  parent_conversation_id TEXT,"
            f"  kind                   TEXT   NOT NULL,"
            f"  title                  TEXT,"
            f"  agent_id               TEXT,"
            f"  session_state          TEXT   NOT NULL,"
            f"  session_usage          TEXT   NOT NULL,"
            f"  reasoning_effort       TEXT,"
            f"  model_override         TEXT,"
            f"  created_at             BIGINT NOT NULL,"
            f"  updated_at             BIGINT NOT NULL"
            f")"
        )

    def _ddl_items(self) -> str:
        """Return CREATE TABLE DDL for the items table."""
        return (
            f"CREATE TABLE IF NOT EXISTS {self._items_table} ("
            f"  item_id         TEXT   PRIMARY KEY,"
            f"  conversation_id TEXT   NOT NULL,"
            f"  position        BIGINT NOT NULL,"
            f"  type            TEXT   NOT NULL,"
            f"  status          TEXT   NOT NULL,"
            f"  response_id     TEXT   NOT NULL,"
            f"  data            TEXT   NOT NULL,"
            f"  search_text     TEXT,"
            f"  created_by      TEXT,"
            f"  created_at      BIGINT NOT NULL"
            f")"
        )

    def _run_ddl(self, sa: Any, ddl: str) -> bool:
        """Execute a DDL statement, logging a warning on failure without raising.

        Returns True on success, False on failure so the caller can avoid
        latching the store as created when the tables don't actually exist.
        """
        try:
            with self.engine.begin() as conn:
                conn.execute(sa.text(ddl))
            return True
        except Exception as exc:
            logger.warning(
                "LakebaseConversationStore: DDL failed: %s — "
                "subsequent reads/writes may fail until the tables exist.",
                exc,
            )
            return False

    # ── Row deserialisation helpers ───────────────────────────────────────────

    def _conv_from_row(self, row: Any) -> Conversation:
        """
        Deserialise a Postgres result row into a :class:`Conversation`.

        :param row: Row mapping from SQLAlchemy ``mappings().first()``.
        :returns: A populated :class:`Conversation`.
        """
        return Conversation(
            id=row["conversation_id"],
            root_conversation_id=row["root_conversation_id"],
            parent_conversation_id=row["parent_conversation_id"],
            kind=row["kind"],
            title=row["title"],
            agent_id=row["agent_id"],
            session_state=json.loads(row["session_state"] or "{}"),
            session_usage=json.loads(row["session_usage"] or "{}"),
            reasoning_effort=row["reasoning_effort"],
            model_override=row["model_override"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def _item_from_row(self, row: Any) -> ConversationItem:
        """
        Deserialise a Postgres result row into a :class:`ConversationItem`.

        :param row: Row mapping from SQLAlchemy ``mappings()``.
        :returns: A populated :class:`ConversationItem`.
        """
        item_type = row["type"]
        raw_data = json.loads(row["data"])
        data = parse_item_data(item_type, raw_data)
        return ConversationItem(
            id=row["item_id"],
            type=item_type,
            status=row["status"],
            response_id=row["response_id"],
            created_at=int(row["created_at"]),
            data=data,
            created_by=row["created_by"],
        )

    # ── ConversationStore implementation ──────────────────────────────────────

    def create_conversation(
        self,
        id: str | None = None,
        kind: str = "default",
        title: str | None = None,
        parent_conversation_id: str | None = None,
        agent_id: str | None = None,
    ) -> Conversation:
        """
        Create and persist a new conversation.

        :param id: Caller-specified conversation id, e.g. ``"session_abc123"``.
            When ``None``, a unique id is generated.
        :param kind: Conversation kind, e.g. ``"default"``.
        :param title: Optional title.
        :param parent_conversation_id: Parent id for child conversations.
        :param agent_id: Optional agent id to bind.
        :returns: The newly created :class:`Conversation`.
        :raises ConversationNotFoundError: If ``parent_conversation_id`` is set
            but the parent row does not exist.
        """
        self._ensure_tables()
        sa = _require_sqlalchemy()
        now = _now_ms()
        cid = id if id is not None else _new_id("conv")
        root_id = self._resolve_root_id(cid, parent_conversation_id)
        sql = sa.text(
            f"INSERT INTO {self._conv_table} ("
            f"  conversation_id, root_conversation_id, parent_conversation_id,"
            f"  kind, title, agent_id, session_state, session_usage,"
            f"  reasoning_effort, model_override, created_at, updated_at"
            f") VALUES ("
            f"  :cid, :root_id, :parent_id, :kind, :title, :agent_id,"
            f"  '{{}}', '{{}}', NULL, NULL, :now, :now"
            f")"
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {
                "cid": cid, "root_id": root_id, "parent_id": parent_conversation_id,
                "kind": kind, "title": title, "agent_id": agent_id, "now": now,
            })
        return Conversation(
            id=cid, created_at=now, updated_at=now,
            root_conversation_id=root_id, title=title,
            kind=kind, parent_conversation_id=parent_conversation_id,
            agent_id=agent_id,
        )

    def _resolve_root_id(self, new_id: str, parent_id: str | None) -> str:
        """
        Look up the root conversation id for a new conversation.

        :param new_id: The newly generated conversation id.
        :param parent_id: Parent conversation id, or ``None`` for top-level.
        :returns: ``new_id`` for top-level, or the parent's root id.
        :raises ConversationNotFoundError: If ``parent_id`` is set but not found.
        """
        if parent_id is None:
            return new_id
        parent = self.get_conversation(parent_id)
        if parent is None:
            raise ConversationNotFoundError(
                f"parent conversation {parent_id!r} not found"
            )
        return parent.root_conversation_id

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Return the conversation row, or ``None`` if it does not exist.

        :param conversation_id: Unique conversation identifier.
        :returns: The :class:`Conversation` if found, otherwise ``None``.
        """
        self._ensure_tables()
        sa = _require_sqlalchemy()
        sql = sa.text(
            f"SELECT * FROM {self._conv_table} WHERE conversation_id = :cid LIMIT 1"
        )
        with self.engine.connect() as conn:
            row = conn.execute(sql, {"cid": conversation_id}).mappings().first()
        return self._conv_from_row(row) if row is not None else None

    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        """
        Return items in a conversation with cursor-based pagination.

        :param conversation_id: Unique conversation identifier.
        :param limit: Maximum items to return.
        :param after: Cursor item ID; return items after this one.
        :param before: Cursor item ID; return items before this one.
        :param order: ``"asc"`` (chronological) or ``"desc"``.
        :param type: Optional type filter, e.g. ``"message"``.
        :returns: A :class:`PagedList` of :class:`ConversationItem`.
        """
        self._ensure_tables()
        sa = _require_sqlalchemy()
        params: dict[str, Any] = {"conv_id": conversation_id, "fetch": limit + 1}
        predicates = ["conversation_id = :conv_id"]
        if type is not None:
            predicates.append("type = :type")
            params["type"] = type
        predicates.extend(self._position_predicates(params, after, before, order))
        sort_dir = "ASC" if order == "asc" else "DESC"
        sql = sa.text(
            f"SELECT item_id, type, status, response_id, data, created_by, created_at "
            f"FROM {self._items_table} "
            f"WHERE {' AND '.join(predicates)} "
            f"ORDER BY position {sort_dir} "
            f"LIMIT :fetch"
        )
        with self.engine.connect() as conn:
            rows = list(conn.execute(sql, params).mappings())
        has_more = len(rows) > limit
        items = [self._item_from_row(r) for r in rows[:limit]]
        return PagedList(
            data=items,
            first_id=items[0].id if items else None,
            last_id=items[-1].id if items else None,
            has_more=has_more,
        )

    def _position_predicates(
        self,
        params: dict[str, Any],
        after: str | None,
        before: str | None,
        order: str,
    ) -> list[str]:
        """
        Build SQL predicates for cursor-based item pagination and populate ``params``.

        :param params: Mutable dict of bind parameters (mutated in-place).
        :param after: Cursor item ID for the ``after`` filter, or ``None``.
        :param before: Cursor item ID for the ``before`` filter, or ``None``.
        :param order: Sort direction (``"asc"`` or ``"desc"``).
        :returns: List of SQL predicate strings.
        """
        predicates: list[str] = []
        if after is not None:
            params["after_id"] = after
            op = ">" if order == "asc" else "<"
            predicates.append(
                f"position {op} (SELECT position FROM {self._items_table} "
                f"WHERE item_id = :after_id AND conversation_id = :conv_id LIMIT 1)"
            )
        if before is not None:
            params["before_id"] = before
            op = "<" if order == "asc" else ">"
            predicates.append(
                f"position {op} (SELECT position FROM {self._items_table} "
                f"WHERE item_id = :before_id AND conversation_id = :conv_id LIMIT 1)"
            )
        return predicates

    def append(
        self,
        conversation_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        """
        Persist items and bump the conversation's ``updated_at``.

        :param conversation_id: Unique conversation identifier.
        :param items: Items to persist.
        :returns: Persisted items with store-assigned IDs and timestamps.
        :raises ConversationNotFoundError: If the conversation does not exist.
        """
        self._ensure_tables()
        if not self.get_conversation(conversation_id):
            raise ConversationNotFoundError(f"conversation {conversation_id!r} not found")
        now = _now_ms()
        sa = _require_sqlalchemy()
        with self.engine.begin() as conn:
            base_pos = self._next_base_position(sa, conn, conversation_id)
            persisted = self._insert_items(sa, conn, conversation_id, items, base_pos, now)
            self._bump_updated_at(sa, conn, conversation_id, now)
        return persisted

    def _next_base_position(self, sa: Any, conn: Any, conversation_id: str) -> int:
        """
        Return the next available position index for a conversation's items.

        :param sa: The sqlalchemy module.
        :param conn: An open SQLAlchemy connection.
        :param conversation_id: Unique conversation identifier.
        :returns: ``MAX(position) + 1``, or ``0`` when no items exist.
        """
        sql = sa.text(
            f"SELECT COALESCE(MAX(position), -1) AS max_pos "
            f"FROM {self._items_table} WHERE conversation_id = :cid"
        )
        row = conn.execute(sql, {"cid": conversation_id}).mappings().first()
        return int(row["max_pos"]) + 1 if row else 0

    def _insert_items(
        self,
        sa: Any,
        conn: Any,
        conversation_id: str,
        items: list[NewConversationItem],
        base_pos: int,
        now: int,
    ) -> list[ConversationItem]:
        """
        INSERT a batch of items in a single statement and return the persisted list.

        :param sa: The sqlalchemy module.
        :param conn: An open SQLAlchemy connection (within a transaction).
        :param conversation_id: Unique conversation identifier.
        :param items: New items to persist.
        :param base_pos: First position to assign (increments per item).
        :param now: Current epoch ms timestamp.
        :returns: :class:`ConversationItem` list with assigned ids/timestamps.
        """
        if not items:
            return []
        sql = sa.text(
            f"INSERT INTO {self._items_table} "
            f"(item_id, conversation_id, position, type, status, response_id,"
            f" data, search_text, created_by, created_at) "
            f"VALUES (:item_id, :conv_id, :pos, :type, :status, :response_id,"
            f" :data, :search_text, :created_by, :now)"
        )
        persisted: list[ConversationItem] = []
        for i, item in enumerate(items):
            item_id = _new_id("item")
            data_json = item.data.model_dump_json(by_alias=True, exclude_none=True)
            search_text = compute_search_text(item.type, item.data) or None
            conn.execute(sql, {
                "item_id": item_id, "conv_id": conversation_id,
                "pos": base_pos + i, "type": item.type, "status": "completed",
                "response_id": item.response_id, "data": data_json,
                "search_text": search_text, "created_by": item.created_by, "now": now,
            })
            persisted.append(ConversationItem(
                id=item_id, type=item.type, status="completed",
                response_id=item.response_id, created_at=now,
                data=item.data, created_by=item.created_by,
            ))
        return persisted

    def _bump_updated_at(self, sa: Any, conn: Any, conversation_id: str, now: int) -> None:
        """
        Update ``updated_at`` on the conversation row after an item append.

        :param sa: The sqlalchemy module.
        :param conn: An open SQLAlchemy connection (within a transaction).
        :param conversation_id: Unique conversation identifier.
        :param now: New ``updated_at`` value in epoch ms.
        """
        conn.execute(
            sa.text(
                f"UPDATE {self._conv_table} SET updated_at = :now "
                f"WHERE conversation_id = :cid"
            ),
            {"now": now, "cid": conversation_id},
        )

    def list_conversations(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        kind: str | None = "default",
        agent_id: str | None = None,
        order: str = "desc",
        sort_by: str = "created_at",
        search_query: str | None = None,
    ) -> PagedList[Conversation]:
        """
        List conversations with cursor-based pagination.

        Fetches all matching conversations from Postgres, then applies in-memory
        cursor pagination (conversations are typically few compared to items).

        :param limit: Maximum conversations to return.
        :param after: Cursor conversation ID.
        :param before: Cursor conversation ID.
        :param kind: Kind filter. ``None`` disables the filter.
        :param agent_id: Agent id filter. ``None`` disables the filter.
        :param order: ``"desc"`` (newest-first) or ``"asc"``.
        :param sort_by: ``"created_at"`` or ``"updated_at"``.
        :param search_query: Substring filter on title or item content.
        :returns: A :class:`PagedList` of :class:`Conversation`.
        """
        self._ensure_tables()
        sa = _require_sqlalchemy()
        params: dict[str, Any] = {}
        predicates = self._conv_list_predicates(kind, agent_id, search_query, params)
        sort_col = "updated_at" if sort_by == "updated_at" else "created_at"
        where = f"WHERE {' AND '.join(predicates)}" if predicates else ""
        sql = sa.text(
            f"SELECT c.* FROM {self._conv_table} c {where} ORDER BY c.{sort_col} ASC"
        )
        with self.engine.connect() as conn:
            rows = list(conn.execute(sql, params).mappings())
        convs = [self._conv_from_row(r) for r in rows]
        return paginate_in_memory(
            convs, id_fn=lambda c: c.id, limit=limit, after=after, before=before, order=order
        )

    def _conv_list_predicates(
        self,
        kind: str | None,
        agent_id: str | None,
        search_query: str | None,
        params: dict[str, Any],
    ) -> list[str]:
        """
        Build SQL WHERE predicates for ``list_conversations`` and populate ``params``.

        :param kind: Optional kind filter value.
        :param agent_id: Optional agent id filter value.
        :param search_query: Optional search string.
        :param params: Mutable bind-param dict populated in-place.
        :returns: List of SQL predicate strings.
        """
        predicates: list[str] = []
        if kind is not None:
            predicates.append("c.kind = :kind")
            params["kind"] = kind
        if agent_id is not None:
            predicates.append("c.agent_id = :agent_id")
            params["agent_id"] = agent_id
        if search_query:
            predicates.append(
                f"(c.title ILIKE :sq OR c.conversation_id IN ("
                f"  SELECT DISTINCT conversation_id FROM {self._items_table} "
                f"  WHERE search_text ILIKE :sq"
                f"))"
            )
            params["sq"] = f"%{_ilike_escape(search_query)}%"
        return predicates

    def search(
        self,
        query: str,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[ConversationItem]:
        """
        Case-insensitive search over conversation items via ``ILIKE``.

        :param query: Search string, e.g. ``"deployment error"``.
        :param conversation_id: Optional scope to one conversation.
        :param limit: Maximum results to return.
        :returns: Matching :class:`ConversationItem` objects, newest first.
        """
        self._ensure_tables()
        sa = _require_sqlalchemy()
        predicates = ["search_text ILIKE :sq"]
        params: dict[str, Any] = {"sq": f"%{_ilike_escape(query)}%", "lim": limit}
        if conversation_id is not None:
            predicates.append("conversation_id = :conv_id")
            params["conv_id"] = conversation_id
        where = " AND ".join(predicates)
        sql = sa.text(
            f"SELECT item_id, type, status, response_id, data, created_by, created_at "
            f"FROM {self._items_table} "
            f"WHERE {where} "
            f"ORDER BY created_at DESC "
            f"LIMIT :lim"
        )
        with self.engine.connect() as conn:
            rows = list(conn.execute(sql, params).mappings())
        return [self._item_from_row(r) for r in rows]

    def update_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
        reasoning_effort: str | None = None,
        _unset_reasoning_effort: bool = False,
        model_override: str | None = None,
        _unset_model_override: bool = False,
    ) -> Conversation | None:
        """
        Update mutable fields on a conversation row.

        :param conversation_id: Unique conversation identifier.
        :param title: New title, or ``None`` to leave unchanged.
        :param reasoning_effort: New reasoning effort, or ``None`` to leave unchanged.
        :param _unset_reasoning_effort: When ``True``, set ``reasoning_effort`` to NULL.
        :param model_override: New model override, or ``None`` to leave unchanged.
        :param _unset_model_override: When ``True``, set ``model_override`` to NULL.
        :returns: Updated :class:`Conversation`, or ``None`` if not found.
        """
        self._ensure_tables()
        if not self.get_conversation(conversation_id):
            return None
        sa = _require_sqlalchemy()
        now = _now_ms()
        params: dict[str, Any] = {}
        assignments = self._update_assignments(
            title, reasoning_effort, _unset_reasoning_effort,
            model_override, _unset_model_override, now, params,
        )
        params["cid"] = conversation_id
        sql = sa.text(
            f"UPDATE {self._conv_table} "
            f"SET {', '.join(assignments)} "
            f"WHERE conversation_id = :cid"
        )
        with self.engine.begin() as conn:
            conn.execute(sql, params)
        return self.get_conversation(conversation_id)

    def _update_assignments(
        self,
        title: str | None,
        reasoning_effort: str | None,
        unset_effort: bool,
        model_override: str | None,
        unset_model: bool,
        now: int,
        params: dict[str, Any],
    ) -> list[str]:
        """
        Build SET clause fragments for ``update_conversation`` and populate ``params``.

        :param title: New title or ``None``.
        :param reasoning_effort: New effort hint or ``None``.
        :param unset_effort: When ``True``, NULL out ``reasoning_effort``.
        :param model_override: New model override or ``None``.
        :param unset_model: When ``True``, NULL out ``model_override``.
        :param now: Current epoch ms for ``updated_at``.
        :param params: Mutable bind-param dict populated in-place.
        :returns: List of SQL SET clause fragments.
        """
        params["now"] = now
        assignments = ["updated_at = :now"]
        if title is not None:
            assignments.append("title = :title")
            params["title"] = title
        if unset_effort:
            assignments.append("reasoning_effort = NULL")
        elif reasoning_effort is not None:
            assignments.append("reasoning_effort = :effort")
            params["effort"] = reasoning_effort
        if unset_model:
            assignments.append("model_override = NULL")
        elif model_override is not None:
            assignments.append("model_override = :model")
            params["model"] = model_override
        return assignments

    def set_session_state(
        self, conversation_id: str, session_state: dict[str, Any]
    ) -> None:
        """Overwrite the persisted ``session_state`` for a conversation.

        Stamps ``updated_at`` for consistency with :meth:`update_conversation`.
        No-op when the conversation does not exist (SQL UPDATE matches zero rows).

        :param conversation_id: Unique conversation identifier.
        :param session_state: JSON-serializable dict to persist.
        """
        # Self-create tables on first use like every other write method (#381) —
        # otherwise a resume/approval flow that persists state before any
        # create_conversation/append hits "relation ... does not exist".
        self._ensure_tables()
        import json as _json

        sa = _require_sqlalchemy()
        now = _now_ms()
        sql = sa.text(
            f"UPDATE {self._conv_table} "
            f"SET session_state = :ss, updated_at = :now "
            f"WHERE conversation_id = :cid"
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {"ss": _json.dumps(session_state), "cid": conversation_id, "now": now})

    def delete_conversation(self, conversation_id: str) -> bool:
        """
        Delete a conversation and all its items.

        :param conversation_id: Unique conversation identifier.
        :returns: ``True`` if found and deleted, ``False`` if not found.
        """
        self._ensure_tables()
        if not self.get_conversation(conversation_id):
            return False
        sa = _require_sqlalchemy()
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(f"DELETE FROM {self._conv_table} WHERE conversation_id = :cid"),
                {"cid": conversation_id},
            )
            conn.execute(
                sa.text(f"DELETE FROM {self._items_table} WHERE conversation_id = :cid"),
                {"cid": conversation_id},
            )
        return True
