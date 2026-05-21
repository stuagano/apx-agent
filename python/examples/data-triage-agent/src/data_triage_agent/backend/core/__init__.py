"""Re-exports from apx-agent.

This module previously shipped a local ``_get_obo_workspace_client`` shim
to work around a Databricks Apps OAuth/PAT auth conflict. apx-agent's
``_make_workspace_client`` now handles that conflict directly (commit
``a3bb76bc`` — pins ``auth_type='pat'`` when constructing with a token),
so the shim is obsolete. Pipeline + tool code keeps using
``Dependencies.Workspace`` — they're re-exported here unchanged.
"""

from apx_agent import Dependencies as Dependencies  # noqa: F401
from apx_agent import create_app as create_app  # noqa: F401
