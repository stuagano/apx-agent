"""Standalone Dependencies class and Databricks client factories.

Provides the same FastAPI dependency injection type aliases that APX's base
template offers, but without requiring the APX template scaffolding.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, MutableMapping
from typing import Annotated, Any, TypeAlias
from uuid import UUID

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from fastapi import Depends, Header, Request
from pydantic import BaseModel, SecretStr

from ._state_marker import _STATE_DEP

# Cap the Databricks SDK retry window for agent-runtime clients. The SDK
# default is 300s, so a flaky/unreachable workspace API — e.g. FEVM/private-link
# egress failing to reach the SQL warehouse API, or the host-metadata probe —
# freezes an interactive agent for a full 5 minutes before erroring. 120s sits
# well above a serverless SQL-warehouse cold-start (~20-30s) so legitimate
# cold-starts still complete, but a genuine egress failure surfaces in ~2 min.
_AGENT_RETRY_TIMEOUT_S = 120

logger = logging.getLogger(__name__)


def _make_workspace_client(**kwargs: Any) -> WorkspaceClient:
    """Create a WorkspaceClient, resolving the Databricks Apps auth conflict.

    Databricks Apps injects both OAuth M2M credentials (DATABRICKS_CLIENT_ID /
    DATABRICKS_CLIENT_SECRET) and a PAT (DATABRICKS_TOKEN) simultaneously.
    ``WorkspaceClient()`` fails with ``validate: more than one authorization
    method configured: oauth and pat`` when both are present.

    ``auth_type`` pins exactly one credential method so the SDK ignores the rest:

    - Explicit kwargs (e.g. OBO ``token=``): ``auth_type="pat"``
    - App-level SP (OAuth M2M env vars present): ``auth_type="oauth-m2m"``
    - Local dev (neither conflict): no auth_type, SDK auto-detects as usual.
    """
    if kwargs:
        kwargs.setdefault("retry_timeout_seconds", _AGENT_RETRY_TIMEOUT_S)
        return WorkspaceClient(config=Config(auth_type="pat", **kwargs))
    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET")
    if client_id and client_secret:
        return WorkspaceClient(
            config=Config(
                auth_type="oauth-m2m",
                retry_timeout_seconds=_AGENT_RETRY_TIMEOUT_S,
            )
        )
    return WorkspaceClient(config=Config(retry_timeout_seconds=_AGENT_RETRY_TIMEOUT_S))


# ---------------------------------------------------------------------------
# Databricks Apps headers
# ---------------------------------------------------------------------------


class DatabricksAppsHeaders(BaseModel):
    """Structured model for Databricks Apps HTTP headers.

    See: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/http-headers
    """

    host: str | None
    user_name: str | None
    user_id: str | None
    user_email: str | None
    request_id: UUID | None
    token: SecretStr | None


def get_databricks_headers(
    host: Annotated[str | None, Header(alias="X-Forwarded-Host")] = None,
    user_name: Annotated[str | None, Header(alias="X-Forwarded-Preferred-Username")] = None,
    user_id: Annotated[str | None, Header(alias="X-Forwarded-User")] = None,
    user_email: Annotated[str | None, Header(alias="X-Forwarded-Email")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    token: Annotated[str | None, Header(alias="X-Forwarded-Access-Token")] = None,
) -> DatabricksAppsHeaders:
    """Extract Databricks Apps headers from the incoming request."""
    return DatabricksAppsHeaders(
        host=host,
        user_name=user_name,
        user_id=user_id,
        user_email=user_email,
        request_id=UUID(request_id) if request_id else None,
        token=SecretStr(token) if token else None,
    )


HeadersDependency: TypeAlias = Annotated[DatabricksAppsHeaders, Depends(get_databricks_headers)]


# ---------------------------------------------------------------------------
# Principal dependency — per-request OBO identity
# ---------------------------------------------------------------------------


def _get_principal(headers: HeadersDependency) -> str | None:
    """Return the OBO user identity (X-Forwarded-User) for the current request.

    Used by config-built memory tools to resolve the per-request principal.
    Returns ``None`` when running locally without Databricks Apps headers.
    """
    return headers.user_id


PrincipalDependency: TypeAlias = Annotated[str | None, Depends(_get_principal)]


# ---------------------------------------------------------------------------
# Progress dependency — emit trace progress markers
# ---------------------------------------------------------------------------


ProgressFn: TypeAlias = Callable[..., None]
"""Callable a tool calls to emit a progress marker into the trace."""


def _get_progress() -> ProgressFn:
    """Return the progress emitter (records a span event on the active span)."""
    from ._mlflow_tracing import emit_progress

    return emit_progress


ProgressDependency: TypeAlias = Annotated[ProgressFn, Depends(_get_progress)]


# ---------------------------------------------------------------------------
# Workspace client factories
# ---------------------------------------------------------------------------


def _get_workspace_client(request: Request) -> WorkspaceClient:
    """Return the app-level WorkspaceClient from app.state."""
    return request.app.state.workspace_client


def _obo_ws_from_headers(headers: DatabricksAppsHeaders) -> WorkspaceClient:
    """Build a user-scoped WorkspaceClient from request headers.

    Parity with the compiled ChatAgent/ResponsesAgent path:

    * **Fail closed** when no OBO token is present in the Databricks Apps
      runtime (via ``resolve_no_obo_or_raise``) instead of silently falling back
      to the app service principal — a token-less ``/tools/<name>`` or MCP call
      must not run with more privilege than the caller. Outside Apps (local dev,
      Model Serving) this is a no-op, so CLI-credential dev is unchanged.
    * **Ignore ``X-Forwarded-Host`` in the Apps runtime** — there that header is
      the App's own public hostname (…databricksapps.com), NOT the workspace API
      host, so using it loops back to the app and hangs. Fall back to
      ``DATABRICKS_HOST`` (host=None → SDK resolves it). Outside Apps the header
      may legitimately carry the workspace host (non-Apps proxy), so keep it.
    """
    from ._obo import _in_databricks_app, resolve_no_obo_or_raise

    if not headers.token:
        resolve_no_obo_or_raise()
        logger.info("No OBO token — falling back to CLI credentials for local dev")
        return _make_workspace_client()
    host = (
        None
        if _in_databricks_app()
        else (f"https://{headers.host}" if headers.host else None)
    )
    return _make_workspace_client(token=headers.token.get_secret_value(), host=host)


def _get_user_client(headers: HeadersDependency) -> WorkspaceClient:
    """Return a WorkspaceClient authenticated on behalf of the current user.

    Uses the OBO token from X-Forwarded-Access-Token when running inside a
    Databricks App.  Fails closed in the Apps runtime when no token is present;
    falls back to CLI-configured credentials for local development.
    """
    return _obo_ws_from_headers(headers)


def _ws_prefer_obo(request: Request) -> WorkspaceClient:
    """Workspace client for Discover / Topology catalog reads.

    Prefer the caller's OBO token when present so inventory matches what the
    signed-in user can see. In the Databricks Apps runtime, **fail closed**
    when OBO is missing or unusable (#612 / G2) — never list under App SP
    while the UI implies user-scoped Discover. Local ``apx-agent run`` still
    falls back to the app/CLI client. Operators that intentionally want App
    SP inventory can set ``APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK=true``.
    """
    from fastapi import HTTPException

    from ._obo import ApxIdentityError, _in_databricks_app, resolve_no_obo_or_raise

    token = (request.headers.get("X-Forwarded-Access-Token") or "").strip()
    if token:
        try:
            return _make_workspace_client(token=token)
        except Exception as exc:
            logger.debug(
                "OBO workspace client failed for Discover; not falling back to App SP on Apps",
                exc_info=True,
            )
            if _in_databricks_app():
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "Discover requires a usable OBO token "
                        f"({type(exc).__name__}); not listing under App SP (#612)."
                    ),
                ) from exc
            # Local / non-Apps: fall through to app/CLI client.
    else:
        try:
            resolve_no_obo_or_raise()
        except ApxIdentityError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    return request.app.state.workspace_client


ClientDependency: TypeAlias = Annotated[WorkspaceClient, Depends(_get_workspace_client)]
UserClientDependency: TypeAlias = Annotated[WorkspaceClient, Depends(_get_user_client)]


# ---------------------------------------------------------------------------
# Request dependency
# ---------------------------------------------------------------------------


def _get_request(request: Request) -> Request:
    """Identity dependency — lets tools declare ``request: Dependencies.Request``."""
    return request


RequestDependency: TypeAlias = Annotated[Request, Depends(_get_request)]


# ---------------------------------------------------------------------------
# SQL runner dependency
# ---------------------------------------------------------------------------


SqlRunnerFn: TypeAlias = Callable[[str], list[dict[str, Any]]]
"""Callable that executes SQL and returns rows as list of dicts."""


def _get_sql_runner(headers: HeadersDependency) -> SqlRunnerFn:
    """Return a SQL runner bound to the current user's workspace client.

    Usage in tool functions::

        def my_tool(query: str, sql: Dependencies.Sql) -> list[dict]:
            return sql(f"SELECT * FROM t WHERE col = '{query}'")
    """
    from ._sql import run_sql

    ws = _obo_ws_from_headers(headers)

    def _runner(sql_statement: str) -> list[dict[str, Any]]:
        return run_sql(ws, sql_statement)

    return _runner


SqlDependency: TypeAlias = Annotated[SqlRunnerFn, Depends(_get_sql_runner)]


# Not a FastAPI Depends — resolved per-call from LangGraph state, not the request
# cycle. See _compile._make_stateful_langchain_tool and
# docs/design/keyed-state-tool-access.md.
StateDependency: TypeAlias = Annotated[MutableMapping[str, Any], _STATE_DEP]


# ---------------------------------------------------------------------------
# Dependencies class — public API
# ---------------------------------------------------------------------------


class Dependencies:
    """FastAPI dependency injection shorthand for route handler parameters.

    Usage in tool functions::

        def my_tool(query: str, ws: Dependencies.Client) -> str:
            rows = ws.statement_execution.execute_statement(...)
            ...

        def my_tool(query: str, sql: Dependencies.Sql) -> list[dict]:
            return sql(f"SELECT * FROM t WHERE col = '{query}'")
    """

    Client: TypeAlias = ClientDependency
    """Databricks WorkspaceClient using app-level service principal credentials.
    Recommended usage: ``ws: Dependencies.Client``"""

    UserClient: TypeAlias = UserClientDependency
    """WorkspaceClient authenticated on behalf of the current user via OBO token.
    Requires the X-Forwarded-Access-Token header.
    Recommended usage: ``user_ws: Dependencies.UserClient``"""

    Headers: TypeAlias = HeadersDependency
    """Databricks Apps HTTP headers for the current request.
    Recommended usage: ``headers: Dependencies.Headers``"""

    Workspace: TypeAlias = UserClientDependency
    """Workspace client authenticated on behalf of the current user (OBO).
    Shorthand for Dependencies.UserClient in agent tool functions.
    Recommended usage: ``ws: Dependencies.Workspace``"""

    Request: TypeAlias = RequestDependency
    """The raw FastAPI Request object — excluded from tool input schemas.
    Recommended usage: ``request: Dependencies.Request``"""

    Sql: TypeAlias = SqlDependency
    """SQL runner bound to the current user's workspace — excluded from schemas.
    Recommended usage: ``sql: Dependencies.Sql``"""

    Principal: TypeAlias = PrincipalDependency
    """Per-request OBO user identity from X-Forwarded-User.
    Returns the username string when running inside a Databricks App, or
    ``None`` for local development without the header.
    Recommended usage: ``principal: Dependencies.Principal``"""

    Progress: TypeAlias = ProgressDependency
    """Emit a progress marker into the trace: ``progress("Loading…")``.
    Recommended usage: ``progress: Dependencies.Progress``."""

    State: TypeAlias = StateDependency
    """In-graph keyed state, read/written like a dict; excluded from the LLM
    schema. Writes are harvested into the graph state after the tool returns.
    In-place mutation of a nested value is not tracked — reassign the key.
    Recommended usage: ``state: Dependencies.State``"""
