"""Standalone Dependencies class and Databricks client factories.

Provides the same FastAPI dependency injection type aliases that APX's base
template offers, but without requiring the APX template scaffolding.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Annotated, Any, TypeAlias
from uuid import UUID

from databricks.sdk import WorkspaceClient
from fastapi import Depends, Header, Request
from pydantic import BaseModel, SecretStr

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
        return WorkspaceClient(auth_type="pat", **kwargs)
    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET")
    if client_id and client_secret:
        return WorkspaceClient(auth_type="oauth-m2m")
    return WorkspaceClient()


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
# Workspace client factories
# ---------------------------------------------------------------------------


def _get_workspace_client(request: Request) -> WorkspaceClient:
    """Return the app-level WorkspaceClient from app.state."""
    return request.app.state.workspace_client


def _get_user_client(headers: HeadersDependency) -> WorkspaceClient:
    """Return a WorkspaceClient authenticated on behalf of the current user.

    Uses the OBO token from X-Forwarded-Access-Token when running inside a
    Databricks App.  Falls back to CLI-configured credentials for local
    development (``apx dev`` / ``uvicorn --reload``).
    """
    if not headers.token:
        logger.info("No OBO token — falling back to CLI credentials for local dev")
        return _make_workspace_client()
    return _make_workspace_client(
        token=headers.token.get_secret_value(),
        host=f"https://{headers.host}" if headers.host else None,
    )


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

    if not headers.token:
        logger.info("No OBO token — SQL runner using CLI credentials for local dev")
        ws = _make_workspace_client()
    else:
        ws = _make_workspace_client(
            token=headers.token.get_secret_value(),
            host=f"https://{headers.host}" if headers.host else None,
        )

    def _runner(sql_statement: str) -> list[dict[str, Any]]:
        return run_sql(ws, sql_statement)

    return _runner


SqlDependency: TypeAlias = Annotated[SqlRunnerFn, Depends(_get_sql_runner)]


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
