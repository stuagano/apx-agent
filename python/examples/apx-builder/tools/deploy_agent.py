from databricks.sdk.errors import NotFound
from apx_agent import Dependencies


def deploy_agent(app_name: str, workspace_path: str, ws: Dependencies.UserClient) -> str:
    """Create (if needed) and deploy an apx-agent project as a Databricks App. Returns app_name."""
    try:
        ws.apps.get(app_name)
    except NotFound:
        ws.apps.create(name=app_name, description="Agent built by apx-builder")

    ws.apps.deploy(app_name=app_name, source_code_path=workspace_path)
    return app_name
