"""Thin compatibility shim — re-exports from the apx-agent package, with
a local fix for the OBO-vs-OAuth-M2M auth conflict on Databricks Apps.

When apx-agent's _get_user_client passes `token=` to WorkspaceClient,
the Databricks SDK still probes env-level OAuth credentials (CLIENT_ID/
CLIENT_SECRET injected by the Apps platform) and refuses with
"more than one authorization method configured: oauth and pat".

Fix: explicitly pass `auth_type="pat"` when constructing with a token,
so the SDK ignores the env-level OAuth credentials.
"""
from typing import Annotated, TypeAlias

from apx_agent import create_app as create_app  # noqa: F401
from apx_agent._defaults import HeadersDependency
from databricks.sdk import WorkspaceClient
from fastapi import Depends


def _get_obo_workspace_client(headers: HeadersDependency) -> WorkspaceClient:
    """Per-request OBO WorkspaceClient that disambiguates auth on Databricks Apps.

    Two important deviations from apx-agent's default _get_user_client:
    - auth_type="pat" disambiguates the OAuth-vs-PAT conflict
    - host= is NOT overridden from X-Forwarded-Host (which is the *app's* URL,
      not the workspace URL). The SDK uses env DATABRICKS_HOST (workspace URL)
      when host is not passed explicitly.
    """
    if headers.token:
        return WorkspaceClient(
            token=headers.token.get_secret_value(),
            auth_type="pat",
        )
    return WorkspaceClient()


_OboClientDep: TypeAlias = Annotated[WorkspaceClient, Depends(_get_obo_workspace_client)]


class Dependencies:
    """Drop-in replacement for apx_agent.Dependencies with the auth fix."""
    UserClient: TypeAlias = _OboClientDep
    Workspace: TypeAlias = _OboClientDep
