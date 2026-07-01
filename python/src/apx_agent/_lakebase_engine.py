"""Shared Lakebase (Databricks managed Postgres) engine builder.

``LakebaseMemoryStore`` and ``LakebaseConversationStore`` require a SQLAlchemy
``Engine`` whose ``do_connect`` listener injects a fresh Databricks OAuth token
as the password on every connect. This module is the single source of truth.

**Credential:** Lakebase accepts a Databricks OAuth access token (the
``/oidc/v1/token`` the workspace UI's "Copy OAuth token" uses) as the Postgres
password — obtained from ``ws.config.authenticate()``. It is instance-agnostic,
so it works for both classic AND autoscaling-project Lakebase (the newer model);
the older ``ws.database.generate_database_credential(instance_names=…)`` only
resolves classic instances. Connect as the authenticated principal (a user's
email / an SP's identity), with ``sslmode=require``. Verified live against a
project and a classic instance.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_SQLALCHEMY_MISSING = (
    "Lakebase engine requires SQLAlchemy. "
    "Install with: pip install 'apx-agent[lakebase]'"
)


def _require_sqlalchemy() -> Any:
    try:
        import sqlalchemy
    except ImportError as e:
        raise ImportError(_SQLALCHEMY_MISSING) from e
    return sqlalchemy


def build_lakebase_engine(
    *,
    ws: Any,
    instance_name: str,
    database: str,
    host: str | None = None,
    port: int = 5432,
    pool_pre_ping: bool = True,
    pool_recycle: int = 1800,
) -> "Engine":
    """Build a SQLAlchemy ``Engine`` for a Databricks Lakebase instance.

    Attaches a ``do_connect`` listener that mints a fresh OAuth token via
    ``ws.database.generate_database_credential`` on every connection.

    Raises ``ImportError`` if sqlalchemy is not installed.
    """
    _require_sqlalchemy()
    from sqlalchemy import URL, create_engine, event as sa_event  # noqa: PLC0415

    # The Postgres role IS the authenticated Databricks principal (the token is
    # minted for it), NOT a literal "apx-agent". Lakebase also requires SSL.
    # URL.create handles percent-encoding the principal's "@". (Validated live in
    # _checkpoint_lakebase — the Lakebase connect dialog confirms Role=<principal>.)
    url = URL.create(
        "postgresql+psycopg",
        username=ws.current_user.me().user_name,
        host=host or "localhost",
        port=port,
        database=database,
        query={"sslmode": "require"},
    )

    engine = create_engine(url, pool_pre_ping=pool_pre_ping, pool_recycle=pool_recycle)

    @sa_event.listens_for(engine, "do_connect")
    def _mint_token(_dialect: Any, _conn_rec: Any, _cargs: Any, ckwargs: dict[str, Any]) -> None:
        """Inject a fresh Databricks OAuth token as the password on every connect.

        Lakebase takes a Databricks OAuth access token (the ``/oidc/v1/token`` the
        UI's "Copy OAuth token" uses) as the password — instance-agnostic, so it
        works for BOTH classic and autoscaling-project instances. The older
        ``generate_database_credential(instance_names=…)`` only resolves classic
        instances. The SDK refreshes the underlying token; ``pool_recycle``
        recycles connections before it expires.
        """
        ckwargs["password"] = ws.config.authenticate()["Authorization"].split(" ", 1)[1]

    return engine
