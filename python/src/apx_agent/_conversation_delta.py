"""DeltaConversationStore — Unity Catalog Delta-backed conversation persistence.

Persists :class:`~apx_agent._conversation.Conversation` and
:class:`~apx_agent._conversation.ConversationItem` objects to two UC Delta tables.
Suitable for Model Serving (stateless replicas) and multi-replica Databricks Apps
deployments where in-memory state is not shared.

Table schema (created on first use if absent)::

    CREATE TABLE {prefix}_conversations (
        conversation_id        STRING  NOT NULL,
        root_conversation_id   STRING  NOT NULL,
        parent_conversation_id STRING,
        kind                   STRING  NOT NULL,
        title                  STRING,
        agent_id               STRING,
        session_state          STRING,   -- JSON
        session_usage          STRING,   -- JSON
        reasoning_effort       STRING,
        model_override         STRING,
        created_at             BIGINT   NOT NULL,  -- epoch ms
        updated_at             BIGINT   NOT NULL   -- epoch ms
    ) USING DELTA;

    CREATE TABLE {prefix}_items (
        item_id         STRING  NOT NULL,
        conversation_id STRING  NOT NULL,
        position        BIGINT  NOT NULL,   -- 0-based insertion order per conversation
        type            STRING  NOT NULL,
        status          STRING  NOT NULL,
        response_id     STRING  NOT NULL,
        data            STRING  NOT NULL,   -- JSON
        search_text     STRING,             -- denormalised plain text for LIKE search
        created_by      STRING,
        created_at      BIGINT  NOT NULL    -- epoch ms
    ) USING DELTA;

Item position is assigned atomically within each ``append()`` batch starting
at ``MAX(position) + 1``.  Concurrent ``append()`` calls on the same
conversation are serialised by the single-request model (one in-flight request
per session), so the MAX read-then-write is safe in practice.

All reads and writes go through the SQL Statements API via :func:`~apx_agent._sql.run_sql`,
so per-request user identity flows through automatically when the WorkspaceClient
is user-scoped (OBO path).
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
    ItemData,
    NewConversationItem,
    PagedList,
    _new_id,
    _now_ms,
    compute_search_text,
    paginate_in_memory,
    parse_item_data,
)
from ._sql import run_sql

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


def _sql_str(value: str) -> str:
    """SQL-escape a string literal for Spark SQL (backslashes then single quotes).

    :param value: The raw string to escape, e.g. ``"it's a test"``.
    :returns: A quoted SQL literal, e.g. ``"'it''s a test'"``.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _like_escape(value: str) -> str:
    """Escape LIKE-pattern special characters for Spark SQL.

    :param value: The raw query string, e.g. ``"50% off"``.
    :returns: Escaped pattern suitable for wrapping in ``%...%``,
        e.g. ``"50\\% off"``.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class DeltaConversationStore(ConversationStore):
    """
    :class:`ConversationStore` backed by two Unity Catalog Delta tables.

    :param table_prefix: Three-part UC prefix for the backing tables,
        e.g. ``"main.myschema.apx_conv"`` — will create
        ``main.myschema.apx_conv_conversations`` and
        ``main.myschema.apx_conv_items``.
    :param ws: WorkspaceClient used for all SQL execution. For user-scoped
        persistence, pass a per-request OBO client.
    :param warehouse_id: Optional SQL warehouse to run statements on. When
        ``None``, the SDK auto-discovers one.
    :param auto_create: When ``True`` (default), ``CREATE TABLE IF NOT EXISTS``
        is issued on first use. Set ``False`` if tables are provisioned
        out-of-band.
    """

    def __init__(
        self,
        *,
        table_prefix: str,
        ws: "WorkspaceClient",
        warehouse_id: str | None = None,
        auto_create: bool = True,
    ) -> None:
        """
        Initialize the Delta conversation store.

        :param table_prefix: Three-part UC prefix, e.g.
            ``"main.myschema.apx_conv"``. Must contain exactly two dots.
        :param ws: WorkspaceClient for SQL execution.
        :param warehouse_id: SQL warehouse id, or ``None`` for auto-discovery.
        :param auto_create: Create tables on first use when ``True``.
        :raises ValueError: If ``table_prefix`` is not a three-part UC name.
        """
        if table_prefix.count(".") != 2:
            raise ValueError(
                f"DeltaConversationStore table_prefix must be a three-part UC name "
                f"(catalog.schema.base). Got {table_prefix!r}"
            )
        super().__init__(storage_location=table_prefix)
        self._prefix = table_prefix
        self.ws = ws
        self.warehouse_id = warehouse_id
        self._auto_create = auto_create
        self._tables_created = False

    @property
    def _conv_table(self) -> str:
        """Fully qualified conversations table name."""
        return f"{self._prefix}_conversations"

    @property
    def _items_table(self) -> str:
        """Fully qualified items table name."""
        return f"{self._prefix}_items"

    # ── Table creation ────────────────────────────────────────────────────────

    def _ensure_tables(self) -> None:
        """Issue CREATE TABLE IF NOT EXISTS on first call. No-op thereafter."""
        if self._tables_created or not self._auto_create:
            return
        self._create_conversations_table()
        self._create_items_table()
        self._tables_created = True

    def _create_conversations_table(self) -> None:
        """Create the conversations table if absent; log and continue on failure."""
        sql = (
            f"CREATE TABLE IF NOT EXISTS {self._conv_table} ("
            f"  conversation_id STRING NOT NULL,"
            f"  root_conversation_id STRING NOT NULL,"
            f"  parent_conversation_id STRING,"
            f"  kind STRING NOT NULL,"
            f"  title STRING,"
            f"  agent_id STRING,"
            f"  session_state STRING,"
            f"  session_usage STRING,"
            f"  reasoning_effort STRING,"
            f"  model_override STRING,"
            f"  created_at BIGINT NOT NULL,"
            f"  updated_at BIGINT NOT NULL"
            f") USING DELTA"
        )
        self._run_ddl(sql, self._conv_table)

    def _create_items_table(self) -> None:
        """Create the items table if absent; log and continue on failure."""
        sql = (
            f"CREATE TABLE IF NOT EXISTS {self._items_table} ("
            f"  item_id STRING NOT NULL,"
            f"  conversation_id STRING NOT NULL,"
            f"  position BIGINT NOT NULL,"
            f"  type STRING NOT NULL,"
            f"  status STRING NOT NULL,"
            f"  response_id STRING NOT NULL,"
            f"  data STRING NOT NULL,"
            f"  search_text STRING,"
            f"  created_by STRING,"
            f"  created_at BIGINT NOT NULL"
            f") USING DELTA"
        )
        self._run_ddl(sql, self._items_table)

    def _run_ddl(self, sql: str, table: str) -> None:
        """Execute a DDL statement, logging a hint on failure without raising."""
        try:
            run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        except Exception as exc:
            schema_parts = table.rsplit(".", 1)[0]
            logger.warning(
                "DeltaConversationStore: DDL failed for %s: %s\n"
                "  Fix: GRANT CREATE TABLE ON SCHEMA %s TO `<your-principal>`;\n"
                "  Or pre-create the tables and set auto_create=False.",
                table, exc, schema_parts,
            )
            self._tables_created = False  # retry on next call

    # ── Row serialisation helpers ─────────────────────────────────────────────

    def _conv_from_row(self, row: dict[str, Any]) -> Conversation:
        """
        Deserialise a SQL result row into a :class:`Conversation`.

        :param row: Dict from :func:`run_sql`, keyed by column name.
        :returns: A populated :class:`Conversation`.
        """
        return Conversation(
            id=row["conversation_id"],
            root_conversation_id=row["root_conversation_id"],
            parent_conversation_id=row.get("parent_conversation_id"),
            kind=row["kind"],
            title=row.get("title"),
            agent_id=row.get("agent_id"),
            session_state=json.loads(row.get("session_state") or "{}"),
            session_usage=json.loads(row.get("session_usage") or "{}"),
            reasoning_effort=row.get("reasoning_effort"),
            model_override=row.get("model_override"),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def _item_from_row(self, row: dict[str, Any]) -> ConversationItem:
        """
        Deserialise a SQL result row into a :class:`ConversationItem`.

        :param row: Dict from :func:`run_sql`, keyed by column name.
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
            created_by=row.get("created_by"),
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
        now = _now_ms()
        cid = id if id is not None else _new_id("conv")
        root_id = self._resolve_root_id(cid, parent_conversation_id)
        sql = self._build_conv_insert(cid, root_id, parent_conversation_id, kind, title, agent_id, now)
        run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        return Conversation(
            id=cid,
            created_at=now,
            updated_at=now,
            root_conversation_id=root_id,
            title=title,
            kind=kind,
            parent_conversation_id=parent_conversation_id,
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

    def _build_conv_insert(
        self,
        cid: str,
        root_id: str,
        parent_id: str | None,
        kind: str,
        title: str | None,
        agent_id: str | None,
        now: int,
    ) -> str:
        """
        Build the INSERT SQL for a new conversation row.

        :param cid: Conversation id.
        :param root_id: Root conversation id.
        :param parent_id: Parent conversation id or ``None``.
        :param kind: Conversation kind string.
        :param title: Optional title.
        :param agent_id: Optional agent id.
        :param now: Current epoch ms timestamp.
        :returns: A SQL INSERT statement string.
        """
        parent_sql = _sql_str(parent_id) if parent_id else "NULL"
        title_sql = _sql_str(title) if title else "NULL"
        agent_sql = _sql_str(agent_id) if agent_id else "NULL"
        return (
            f"INSERT INTO {self._conv_table} ("
            f"  conversation_id, root_conversation_id, parent_conversation_id,"
            f"  kind, title, agent_id, session_state, session_usage,"
            f"  reasoning_effort, model_override, created_at, updated_at"
            f") VALUES ("
            f"  {_sql_str(cid)}, {_sql_str(root_id)}, {parent_sql},"
            f"  {_sql_str(kind)}, {title_sql}, {agent_sql},"
            f"  {_sql_str('{}')}, {_sql_str('{}')}, NULL, NULL,"
            f"  {now}, {now}"
            f")"
        )

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Return the conversation row, or ``None`` if it does not exist.

        :param conversation_id: Unique conversation identifier.
        :returns: The :class:`Conversation` if found, otherwise ``None``.
        """
        self._ensure_tables()
        sql = (
            f"SELECT * FROM {self._conv_table} "
            f"WHERE conversation_id = {_sql_str(conversation_id)} LIMIT 1"
        )
        rows = run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        return self._conv_from_row(rows[0]) if rows else None

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
        filters = [f"conversation_id = {_sql_str(conversation_id)}"]
        if type is not None:
            filters.append(f"type = {_sql_str(type)}")
        filters.extend(self._position_filters(conversation_id, after, before, order))
        where = " AND ".join(filters)
        sort_dir = "ASC" if order == "asc" else "DESC"
        sql = (
            f"SELECT item_id, type, status, response_id, data, created_by, created_at, position "
            f"FROM {self._items_table} "
            f"WHERE {where} "
            f"ORDER BY position {sort_dir} "
            f"LIMIT {limit + 1}"
        )
        rows = run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [self._item_from_row(r) for r in page_rows]
        return PagedList(
            data=items,
            first_id=items[0].id if items else None,
            last_id=items[-1].id if items else None,
            has_more=has_more,
        )

    def _position_filters(
        self,
        conversation_id: str,
        after: str | None,
        before: str | None,
        order: str,
    ) -> list[str]:
        """
        Build SQL position filter clauses for cursor-based item pagination.

        :param conversation_id: Conversation id for scoping cursor lookups.
        :param after: Cursor item ID for the ``after`` filter.
        :param before: Cursor item ID for the ``before`` filter.
        :param order: Sort direction (``"asc"`` or ``"desc"``).
        :returns: List of SQL WHERE clause fragments.
        """
        filters = []
        if after is not None:
            op = ">" if order == "asc" else "<"
            filters.append(
                f"position {op} (SELECT position FROM {self._items_table} "
                f"WHERE item_id = {_sql_str(after)} "
                f"AND conversation_id = {_sql_str(conversation_id)} LIMIT 1)"
            )
        if before is not None:
            op = "<" if order == "asc" else ">"
            filters.append(
                f"position {op} (SELECT position FROM {self._items_table} "
                f"WHERE item_id = {_sql_str(before)} "
                f"AND conversation_id = {_sql_str(conversation_id)} LIMIT 1)"
            )
        return filters

    def append(
        self,
        conversation_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        """
        Persist items and bump the conversation's ``updated_at``.

        Position values are assigned starting at ``MAX(position) + 1`` for the
        conversation. Concurrent appends to the same conversation are safe in
        the single-request-per-session model.

        :param conversation_id: Unique conversation identifier.
        :param items: Items to persist.
        :returns: Persisted items with store-assigned IDs and timestamps.
        :raises ConversationNotFoundError: If the conversation does not exist.
        """
        self._ensure_tables()
        if not self.get_conversation(conversation_id):
            raise ConversationNotFoundError(f"conversation {conversation_id!r} not found")
        now = _now_ms()
        base_pos = self._next_base_position(conversation_id)
        persisted = self._insert_items(conversation_id, items, base_pos, now)
        self._bump_conversation_updated_at(conversation_id, now)
        return persisted

    def _next_base_position(self, conversation_id: str) -> int:
        """
        Return the next available position index for a conversation's items.

        :param conversation_id: Unique conversation identifier.
        :returns: ``MAX(position) + 1``, or ``0`` when the conversation has no items.
        """
        sql = (
            f"SELECT COALESCE(MAX(position), -1) AS max_pos "
            f"FROM {self._items_table} "
            f"WHERE conversation_id = {_sql_str(conversation_id)}"
        )
        rows = run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        return int(rows[0]["max_pos"]) + 1 if rows else 0

    def _insert_items(
        self,
        conversation_id: str,
        items: list[NewConversationItem],
        base_pos: int,
        now: int,
    ) -> list[ConversationItem]:
        """
        INSERT a batch of items and return the persisted list.

        :param conversation_id: Unique conversation identifier.
        :param items: New items to persist.
        :param base_pos: First position to assign (increments per item).
        :param now: Current epoch ms timestamp.
        :returns: :class:`ConversationItem` list with assigned ids/timestamps.
        """
        if not items:
            return []
        persisted: list[ConversationItem] = []
        value_parts: list[str] = []
        for i, item in enumerate(items):
            item_id = _new_id("item")
            data_json = item.data.model_dump_json(by_alias=True, exclude_none=True)
            search_text = compute_search_text(item.type, item.data)
            created_by_sql = _sql_str(item.created_by) if item.created_by else "NULL"
            search_sql = _sql_str(search_text) if search_text else "NULL"
            value_parts.append(
                f"({_sql_str(item_id)}, {_sql_str(conversation_id)}, {base_pos + i},"
                f" {_sql_str(item.type)}, {_sql_str('completed')},"
                f" {_sql_str(item.response_id)}, {_sql_str(data_json)},"
                f" {search_sql}, {created_by_sql}, {now})"
            )
            persisted.append(ConversationItem(
                id=item_id,
                type=item.type,
                status="completed",
                response_id=item.response_id,
                created_at=now,
                data=item.data,
                created_by=item.created_by,
            ))
        sql = (
            f"INSERT INTO {self._items_table} "
            f"(item_id, conversation_id, position, type, status, response_id,"
            f" data, search_text, created_by, created_at) "
            f"VALUES {', '.join(value_parts)}"
        )
        run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        return persisted

    def _bump_conversation_updated_at(self, conversation_id: str, now: int) -> None:
        """
        Update ``updated_at`` on the conversations row after an item append.

        :param conversation_id: Unique conversation identifier.
        :param now: New ``updated_at`` value in epoch ms.
        """
        sql = (
            f"UPDATE {self._conv_table} "
            f"SET updated_at = {now} "
            f"WHERE conversation_id = {_sql_str(conversation_id)}"
        )
        run_sql(self.ws, sql, warehouse_id=self.warehouse_id)

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

        Conversations are fetched from the DB with filters applied; cursor-based
        pagination is then applied in-memory (conversations are typically few
        compared to items).

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
        filters = self._conv_list_filters(kind, agent_id, search_query)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        sort_col = "updated_at" if sort_by == "updated_at" else "created_at"
        sql = (
            f"SELECT c.* FROM {self._conv_table} c {where} "
            f"ORDER BY c.{sort_col} ASC"
        )
        rows = run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        convs = [self._conv_from_row(r) for r in rows]
        return paginate_in_memory(
            convs, id_fn=lambda c: c.id, limit=limit, after=after, before=before, order=order
        )

    def _conv_list_filters(
        self,
        kind: str | None,
        agent_id: str | None,
        search_query: str | None,
    ) -> list[str]:
        """
        Build SQL WHERE fragments for ``list_conversations``.

        :param kind: Optional kind filter value.
        :param agent_id: Optional agent id filter value.
        :param search_query: Optional full-text search string.
        :returns: List of SQL WHERE clause fragments.
        """
        filters: list[str] = []
        if kind is not None:
            filters.append(f"c.kind = {_sql_str(kind)}")
        if agent_id is not None:
            filters.append(f"c.agent_id = {_sql_str(agent_id)}")
        if search_query:
            q_sql = _sql_str(f"%{_like_escape(search_query.lower())}%")
            filters.append(
                f"(LOWER(c.title) LIKE {q_sql} OR c.conversation_id IN ("
                f"  SELECT DISTINCT conversation_id FROM {self._items_table} "
                f"  WHERE LOWER(search_text) LIKE {q_sql}"
                f"))"
            )
        return filters

    def search(
        self,
        query: str,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[ConversationItem]:
        """
        Full-text search over conversation items via LIKE on ``search_text``.

        :param query: Case-insensitive search string.
        :param conversation_id: Optional scope to one conversation.
        :param limit: Maximum results to return.
        :returns: Matching :class:`ConversationItem` objects.
        """
        self._ensure_tables()
        q_sql = _sql_str(f"%{_like_escape(query.lower())}%")
        filters = [f"LOWER(search_text) LIKE {q_sql}"]
        if conversation_id is not None:
            filters.append(f"conversation_id = {_sql_str(conversation_id)}")
        where = " AND ".join(filters)
        sql = (
            f"SELECT item_id, type, status, response_id, data, created_by, created_at "
            f"FROM {self._items_table} "
            f"WHERE {where} "
            f"ORDER BY created_at DESC "
            f"LIMIT {limit}"
        )
        rows = run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
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
        now = _now_ms()
        assignments = [f"updated_at = {now}"]
        assignments.extend(self._update_assignments(
            title, reasoning_effort, _unset_reasoning_effort,
            model_override, _unset_model_override,
        ))
        sql = (
            f"UPDATE {self._conv_table} "
            f"SET {', '.join(assignments)} "
            f"WHERE conversation_id = {_sql_str(conversation_id)}"
        )
        run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        return self.get_conversation(conversation_id)

    def _update_assignments(
        self,
        title: str | None,
        reasoning_effort: str | None,
        unset_effort: bool,
        model_override: str | None,
        unset_model: bool,
    ) -> list[str]:
        """
        Build SET clause fragments for ``update_conversation``.

        :param title: New title or ``None`` (leave unchanged).
        :param reasoning_effort: New effort hint or ``None``.
        :param unset_effort: When ``True``, SET reasoning_effort = NULL.
        :param model_override: New model override or ``None``.
        :param unset_model: When ``True``, SET model_override = NULL.
        :returns: List of SQL SET clause fragments.
        """
        parts: list[str] = []
        if title is not None:
            parts.append(f"title = {_sql_str(title)}")
        if unset_effort:
            parts.append("reasoning_effort = NULL")
        elif reasoning_effort is not None:
            parts.append(f"reasoning_effort = {_sql_str(reasoning_effort)}")
        if unset_model:
            parts.append("model_override = NULL")
        elif model_override is not None:
            parts.append(f"model_override = {_sql_str(model_override)}")
        return parts

    def delete_conversation(self, conversation_id: str) -> bool:
        """
        Delete a conversation and all its items.

        :param conversation_id: Unique conversation identifier.
        :returns: ``True`` if the conversation existed and was deleted,
            ``False`` if it did not exist.
        """
        self._ensure_tables()
        if not self.get_conversation(conversation_id):
            return False
        conv_sql = (
            f"DELETE FROM {self._conv_table} "
            f"WHERE conversation_id = {_sql_str(conversation_id)}"
        )
        items_sql = (
            f"DELETE FROM {self._items_table} "
            f"WHERE conversation_id = {_sql_str(conversation_id)}"
        )
        run_sql(self.ws, conv_sql, warehouse_id=self.warehouse_id)
        run_sql(self.ws, items_sql, warehouse_id=self.warehouse_id)
        return True
