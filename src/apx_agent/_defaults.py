"""Standalone Dependencies class and Databricks client factories.

Provides the same FastAPI dependency injection type aliases that APX's base
template offers, but without requiring the APX template scaffolding.
"""

from __future__ import annotations

import logging
from typing import Annotated, TypeAlias
from uuid import UUID

from databricks.sdk import WorkspaceClient
from fastapi import Depends, Header, Request
from pydantic import BaseModel, SecretStr

logger = logging.getLogger(__name__)


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
        return WorkspaceClient()
    return WorkspaceClient(
        token=headers.token.get_secret_value(),
        host=f"https://{headers.host}" if headers.host else None,
    )


ClientDependency: TypeAlias = Annotated[WorkspaceClient, Depends(_get_workspace_client)]
UserClientDependency: TypeAlias = Annotated[WorkspaceClient, Depends(_get_user_client)]


# ---------------------------------------------------------------------------
# Dependencies class — public API
# ---------------------------------------------------------------------------


class Dependencies:
    """FastAPI dependency injection shorthand for route handler parameters.

    Usage in tool functions::

        def my_tool(query: str, ws: Dependencies.Client) -> str:
            rows = ws.statement_execution.execute_statement(...)
            ...
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
