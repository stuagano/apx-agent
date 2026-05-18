"""DeltaSessionStore — UC Delta-backed session persistence.

Persists ``Session`` objects to a Unity Catalog Delta table. Suitable for
Model Serving (stateless replicas, durable storage required) and
multi-replica Apps deployments where in-memory state isn't shared.

Table schema (created on first put if absent)::

    CREATE TABLE <table_path> (
        session_id  STRING NOT NULL,
        history     STRING,   -- JSON-encoded list of message dicts
        state       STRING,   -- JSON-encoded dict
        created_at  DOUBLE,   -- Unix epoch seconds
        updated_at  DOUBLE
    ) USING DELTA

All reads and writes go through the SQL Statements API via ``run_sql``,
so per-request user identity flows through automatically when the
WorkspaceClient is user-scoped (OBO path).

Upserts use ``MERGE INTO ... USING (SELECT ...) AS src ON
target.session_id = src.session_id WHEN MATCHED UPDATE ... WHEN NOT
MATCHED INSERT``. Idempotent under concurrent writes on different
session_ids; concurrent writes on the same session_id last-write-wins
(no optimistic locking yet).
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from ._session import Session
from ._sql import run_sql

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


def _sql_str(value: str) -> str:
    """SQL-escape a string literal — single quotes doubled."""
    return "'" + value.replace("'", "''") + "'"


class DeltaSessionStore:
    """``SessionStore`` backed by a Unity Catalog Delta table.

    Args:
        table_path: Fully-qualified three-part name
            (``"catalog.schema.sessions"``) for the backing Delta table.
            Created on first ``put`` if absent.
        ws: WorkspaceClient used for all SQL execution. For user-scoped
            persistence, pass a per-request OBO client.
        warehouse_id: Optional SQL warehouse to run statements on. When
            omitted, the SDK auto-discovers one — set this explicitly in
            production for predictable cost and routing.
        auto_create: When True (default), the constructor issues a
            ``CREATE TABLE IF NOT EXISTS`` on first use. Set False if the
            table is provisioned out-of-band by a platform team.
    """

    def __init__(
        self,
        *,
        table_path: str,
        ws: "WorkspaceClient",
        warehouse_id: str | None = None,
        auto_create: bool = True,
    ) -> None:
        if table_path.count(".") != 2:
            raise ValueError(
                f"DeltaSessionStore table_path must be a three-part UC name "
                f"(catalog.schema.table). Got {table_path!r}"
            )
        self.table_path = table_path
        self.ws = ws
        self.warehouse_id = warehouse_id
        self._auto_create = auto_create
        self._created = False

    # -- table creation -----------------------------------------------------

    def _ensure_table(self) -> None:
        if self._created or not self._auto_create:
            return
        sql = (
            f"CREATE TABLE IF NOT EXISTS {self.table_path} ("
            f"  session_id STRING NOT NULL,"
            f"  history STRING,"
            f"  state STRING,"
            f"  created_at DOUBLE,"
            f"  updated_at DOUBLE"
            f") USING DELTA"
        )
        try:
            run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        except Exception as e:
            logger.warning(
                "DeltaSessionStore: CREATE TABLE IF NOT EXISTS %s failed: %s — "
                "subsequent reads/writes may fail until the table exists.",
                self.table_path, e,
            )
        self._created = True

    # -- SessionStore protocol ---------------------------------------------

    def get(self, session_id: str) -> Session | None:
        self._ensure_table()
        sql = (
            f"SELECT session_id, history, state, created_at, updated_at "
            f"FROM {self.table_path} "
            f"WHERE session_id = {_sql_str(session_id)} LIMIT 1"
        )
        try:
            rows = run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        except Exception as e:
            logger.warning(
                "DeltaSessionStore.get(%s) failed: %s — returning None.",
                session_id, e,
            )
            return None
        if not rows:
            return None
        row = rows[0]
        try:
            history = json.loads(row.get("history") or "[]")
            state = json.loads(row.get("state") or "{}")
        except json.JSONDecodeError as e:
            logger.warning(
                "DeltaSessionStore.get(%s): malformed JSON in row: %s — "
                "returning empty session.",
                session_id, e,
            )
            history, state = [], {}
        return Session(
            session_id=row["session_id"],
            history=history,
            state=state,
            created_at=float(row.get("created_at") or 0.0),
            updated_at=float(row.get("updated_at") or 0.0),
        )

    def put(self, session: Session) -> None:
        self._ensure_table()
        session.updated_at = time.time()
        history_json = json.dumps(session.history, default=str)
        state_json = json.dumps(session.state, default=str)
        # MERGE keeps the upsert atomic. Source is a single-row inline
        # SELECT so we don't need a staging table.
        sql = (
            f"MERGE INTO {self.table_path} AS target "
            f"USING (SELECT "
            f"  {_sql_str(session.session_id)} AS session_id, "
            f"  {_sql_str(history_json)} AS history, "
            f"  {_sql_str(state_json)} AS state, "
            f"  {session.created_at} AS created_at, "
            f"  {session.updated_at} AS updated_at"
            f") AS src "
            f"ON target.session_id = src.session_id "
            f"WHEN MATCHED THEN UPDATE SET "
            f"  history = src.history, "
            f"  state = src.state, "
            f"  updated_at = src.updated_at "
            f"WHEN NOT MATCHED THEN INSERT ("
            f"  session_id, history, state, created_at, updated_at"
            f") VALUES ("
            f"  src.session_id, src.history, src.state, src.created_at, src.updated_at"
            f")"
        )
        run_sql(self.ws, sql, warehouse_id=self.warehouse_id)

    def delete(self, session_id: str) -> None:
        self._ensure_table()
        sql = (
            f"DELETE FROM {self.table_path} "
            f"WHERE session_id = {_sql_str(session_id)}"
        )
        try:
            run_sql(self.ws, sql, warehouse_id=self.warehouse_id)
        except Exception as e:
            logger.warning(
                "DeltaSessionStore.delete(%s) failed: %s",
                session_id, e,
            )
