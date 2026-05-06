"""Re-exports from apx-agent with OBO auth fix for Databricks Apps."""
from typing import Annotated, TypeAlias

from apx_agent import create_app as create_app  # noqa: F401
from apx_agent._defaults import HeadersDependency
from databricks.sdk import WorkspaceClient
from fastapi import Depends


def _get_obo_workspace_client(headers: HeadersDependency) -> WorkspaceClient:
    if headers.token:
        return WorkspaceClient(
            token=headers.token.get_secret_value(),
            auth_type="pat",
        )
    return WorkspaceClient()


_OboClientDep: TypeAlias = Annotated[WorkspaceClient, Depends(_get_obo_workspace_client)]


class Dependencies:
    UserClient: TypeAlias = _OboClientDep
    Workspace: TypeAlias = _OboClientDep
