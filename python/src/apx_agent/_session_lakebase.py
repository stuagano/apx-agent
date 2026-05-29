"""LakebaseSessionStore — Postgres-backed session persistence over Databricks Lakebase.

Lakebase is Databricks' managed Postgres. Compared to ``DeltaSessionStore``:

  * Lower per-operation latency (Postgres point lookups vs. SQL warehouse
    round-trips).
  * Better for high-frequency turns (chat UIs, interactive agents).
  * Worse for cheap durability across long-idle sessions (Delta scales
    storage independently; Lakebase charges for the running instance).

Both stores satisfy the same ``SessionStore`` protocol. Pick by workload:
chat-style → Lakebase; analytics-style multi-step pipelines → Delta.

This store takes a SQLAlchemy ``Engine`` rather than embedding the
Lakebase OAuth token rotation logic. The caller wires up the engine with
whatever auth pattern they prefer (typically a ``do_connect`` event
listener that calls ``ws.postgres.generate_database_credential()`` to
mint a fresh token). The store stays narrow: SQL only.

Schema (created on first put when ``auto_create=True``)::

    CREATE TABLE IF NOT EXISTS {table_name} (
        session_id  TEXT PRIMARY KEY,
        history     TEXT NOT NULL,   -- JSON-encoded list of message dicts
        state       TEXT NOT NULL,   -- JSON-encoded dict
        created_at  DOUBLE PRECISION,
        updated_at  DOUBLE PRECISION
    )

Requires the ``lakebase`` extra::

    pip install 'apx-agent[lakebase]'
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from ._session import Session, StoreError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


_SQLALCHEMY_MISSING = (
    "LakebaseSessionStore requires SQLAlchemy. "
    "Install with: pip install 'apx-agent[lakebase]'"
)


def _require_sqlalchemy() -> Any:
    try:
        import sqlalchemy
    except ImportError as e:  # pragma: no cover — exercised only without extra
        raise ImportError(_SQLALCHEMY_MISSING) from e
    return sqlalchemy


class LakebaseSessionStore:
    """``SessionStore`` backed by a Lakebase (or any Postgres) instance.

    Args:
        engine: SQLAlchemy ``Engine`` connected to the Lakebase instance.
            The caller is responsible for token rotation — typically a
            ``do_connect`` event listener that mints fresh OAuth tokens
            via ``ws.postgres.generate_database_credential()``.
        table_name: Table name for session rows. Defaults to
            ``"apx_sessions"``. Quoted as a SQL identifier in queries,
            so it can include a schema prefix (e.g. ``"public.apx_sessions"``).
        auto_create: When True (default), runs ``CREATE TABLE IF NOT EXISTS``
            on first use. Set False when the table is provisioned by
            platform tooling.

    Example::

        from sqlalchemy import create_engine, event
        from databricks.sdk import WorkspaceClient
        from apx_agent import LakebaseSessionStore

        ws = WorkspaceClient()

        def _build_url() -> str:
            return "postgresql+psycopg://user@lakebase-host:5432/agentdb"

        engine = create_engine(_build_url(), pool_pre_ping=True, pool_recycle=1800)

        @event.listens_for(engine, "do_connect")
        def add_oauth_token(_dialect, _record, _args, kwargs):
            cred = ws.database.generate_database_credential(
                instance_names=["my-lakebase-instance"],
                request_id="apx-sessions",
            )
            kwargs["password"] = cred.token

        store = LakebaseSessionStore(engine=engine)
    """

    def __init__(
        self,
        *,
        engine: "Engine",
        table_name: str = "apx_sessions",
        auto_create: bool = True,
    ) -> None:
        _require_sqlalchemy()  # surfaces a friendly ImportError early
        self.engine = engine
        self.table_name = table_name
        self._auto_create = auto_create
        self._created = False

    # -- helpers -----------------------------------------------------------

    def _ensure_table(self) -> None:
        if self._created or not self._auto_create:
            return
        sa = _require_sqlalchemy()
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {self.table_name} ("
            f"  session_id TEXT PRIMARY KEY,"
            f"  history TEXT NOT NULL,"
            f"  state TEXT NOT NULL,"
            f"  created_at DOUBLE PRECISION,"
            f"  updated_at DOUBLE PRECISION"
            f")"
        )
        try:
            with self.engine.begin() as conn:
                conn.execute(sa.text(ddl))
        except Exception as e:
            logger.warning(
                "LakebaseSessionStore: CREATE TABLE IF NOT EXISTS %s failed: %s — "
                "subsequent reads/writes may fail until the table exists.",
                self.table_name, e,
            )
        self._created = True

    # -- SessionStore protocol --------------------------------------------

    def get(self, session_id: str) -> Session | None:
        self._ensure_table()
        sa = _require_sqlalchemy()
        sql = sa.text(
            f"SELECT session_id, history, state, created_at, updated_at "
            f"FROM {self.table_name} WHERE session_id = :sid LIMIT 1"
        )
        try:
            with self.engine.connect() as conn:
                row = conn.execute(sql, {"sid": session_id}).mappings().first()
        except Exception as e:
            logger.warning(
                "LakebaseSessionStore.get(%s) failed: %s — raising StoreError "
                "(read failure is not a missing session).",
                session_id, e,
            )
            raise StoreError(
                f"LakebaseSessionStore.get({session_id!r}) failed: {e}"
            ) from e
        if row is None:
            return None
        try:
            history = json.loads(row["history"] or "[]")
            state = json.loads(row["state"] or "{}")
        except json.JSONDecodeError as e:
            logger.warning(
                "LakebaseSessionStore.get(%s): malformed JSON: %s — "
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
        sa = _require_sqlalchemy()
        session.updated_at = time.time()
        # Postgres UPSERT via ON CONFLICT. The table_name is a controlled
        # identifier (set at construction), not user input, so it's safe
        # to inline. session_id and JSON payloads go through bind params.
        sql = sa.text(
            f"INSERT INTO {self.table_name} "
            f"(session_id, history, state, created_at, updated_at) "
            f"VALUES (:sid, :history, :state, :created_at, :updated_at) "
            f"ON CONFLICT (session_id) DO UPDATE SET "
            f"  history = EXCLUDED.history, "
            f"  state = EXCLUDED.state, "
            f"  updated_at = EXCLUDED.updated_at"
        )
        params = {
            "sid": session.session_id,
            "history": json.dumps(session.history, default=str),
            "state": json.dumps(session.state, default=str),
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
        with self.engine.begin() as conn:
            conn.execute(sql, params)

    def delete(self, session_id: str) -> None:
        self._ensure_table()
        sa = _require_sqlalchemy()
        sql = sa.text(f"DELETE FROM {self.table_name} WHERE session_id = :sid")
        try:
            with self.engine.begin() as conn:
                conn.execute(sql, {"sid": session_id})
        except Exception as e:
            logger.warning(
                "LakebaseSessionStore.delete(%s) failed: %s",
                session_id, e,
            )
