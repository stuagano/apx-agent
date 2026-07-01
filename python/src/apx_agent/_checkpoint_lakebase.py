"""Durable LangGraph checkpointer backed by Databricks Lakebase (managed Postgres).

The served short-term-memory path keys thread state by ``session_id`` /
``thread_id`` in a LangGraph checkpointer. The process-default is an
``InMemorySaver`` — fast, but it forgets on restart and never spans replicas. A
**pending mid-turn approval** lives entirely in checkpoint state (not in the
ConversationStore, which only persists *completed* turns), so with an in-process
saver an approval waiting for a human decision dies on any restart.

This module builds the durable alternative: LangGraph's official
``PostgresSaver`` over a Lakebase instance, so a pending approval (and short-term
memory generally) survives a restart and is shared across replicas. See #329
Slice C.

**Credential rotation.** Lakebase auth is a short-lived OAuth token (~1h), minted
via ``ws.database.generate_database_credential`` — the same call
:mod:`apx_agent._lakebase_engine` uses. A long-lived pooled connection would
outlive its token, so the pool mints a *fresh* token per connection attempt (a
custom ``connection_class``) and recycles connections every ``max_lifetime``
seconds (< token TTL), mirroring the SQLAlchemy engine's ``pool_recycle=1800``.

Requires the ``lakebase`` extra (``PostgresSaver`` + ``psycopg_pool``)::

    pip install 'apx-agent[lakebase]'
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres import PostgresSaver

logger = logging.getLogger(__name__)

# Recycle pooled connections well before the ~1h Lakebase token expires, matching
# _lakebase_engine's pool_recycle. Each new connection mints a fresh token.
_MAX_LIFETIME_SECONDS = 1800

_MISSING = (
    "Durable Lakebase checkpointer requires the langgraph Postgres saver. "
    "Install with: pip install 'apx-agent[lakebase]'"
)


def build_lakebase_checkpointer(
    *,
    ws: Any,
    instance_name: str,
    database: str,
    host: str | None = None,
    port: int = 5432,
) -> "PostgresSaver":
    """Build a durable ``PostgresSaver`` for a Databricks Lakebase instance.

    The returned saver owns a psycopg connection pool that mints a fresh OAuth
    token per connection (via ``ws.database.generate_database_credential``) and
    recycles connections before the token expires. ``.setup()`` has already run,
    so the checkpoint tables exist.

    The pool lives for the app lifetime; process exit reclaims it (no explicit
    teardown — mirrors how the served agent holds its saver).

    :raises ImportError: if the ``lakebase`` extra (langgraph Postgres saver /
        psycopg_pool) is not installed.
    """
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as e:
        raise ImportError(_MISSING) from e

    def _mint_token() -> str:
        # NOTE: generate_database_credential resolves only CLASSIC database
        # instances; it returns "not found" for the new autoscaling-project
        # Lakebase. Project-instance credentials need a different API (tracked,
        # #329). Everything else here is validated live against a project instance
        # (see scripts/lakebase_checkpointer_smoke.py --token-file).
        cred = ws.database.generate_database_credential(
            instance_names=[instance_name],
            request_id="apx-agent-lakebase",
        )
        return cred.token

    # The Postgres role IS the authenticated Databricks principal (the token is
    # minted for it) — a user connects as its email, a service principal as its
    # own identity. NOT a literal "apx-agent".
    principal = ws.current_user.me().user_name

    # Fresh token per connection attempt — a pooled connection must never outlive
    # its short-lived Lakebase token. This is psycopg's supported rotation hook.
    class _LakebaseConnection(psycopg.Connection):  # type: ignore[type-arg]
        @classmethod
        def connect(cls, conninfo: str, **kwargs: Any) -> Any:
            # The pool always calls connect() with a real conninfo, so no default
            # is needed (and an empty-string default is banned by lint).
            kwargs["password"] = _mint_token()
            return super().connect(conninfo, **kwargs)

    hostname = host or "localhost"
    pool = ConnectionPool(
        conninfo="",  # connection params passed as kwargs (avoids URL-encoding the principal's @)
        connection_class=_LakebaseConnection,
        # Lakebase requires SSL and connects as the principal. PostgresSaver
        # requires autocommit (so .setup() commits DDL) and dict_row (it reads
        # rows by name). prepare_threshold=None DISABLES server-side prepared
        # statements — Lakebase fronts Postgres with a transaction-mode pooler
        # that reassigns backends per transaction, and a prepared statement bound
        # to one backend then errors ("prepared statement already exists"/"does
        # not exist") on the next. (langgraph's docs use 0, but that assumes a
        # direct/session-pooled Postgres.)
        kwargs={
            "host": hostname,
            "port": port,
            "dbname": database,
            "user": principal,
            "sslmode": "require",
            "autocommit": True,
            "row_factory": dict_row,
            "prepare_threshold": None,
        },
        min_size=1,
        max_size=5,
        max_lifetime=_MAX_LIFETIME_SECONDS,
        open=True,
    )
    # cast: ConnectionPool is invariant in its connection type, so a
    # ConnectionPool[_LakebaseConnection] isn't assignable to PostgresSaver's
    # ConnectionPool[Connection] param even though _LakebaseConnection IS a
    # Connection. Runtime-correct; only visible to pyright with the extra installed.
    saver = PostgresSaver(cast("Any", pool))
    saver.setup()  # idempotent — creates the checkpoint tables on first use
    logger.info(
        "Durable short-term memory: Lakebase PostgresSaver on instance %r db %r — "
        "pending approvals and thread state survive restarts and span replicas.",
        instance_name, database,
    )
    return saver
