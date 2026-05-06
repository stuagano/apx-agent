import time
import httpx
from apx_agent import Dependencies


def poll_deployment(app_name: str, ws: Dependencies.UserClient) -> str:
    """Wait for agent to be fully live. Returns URL when ready, or URL with warning on Stage 2 timeout."""
    # Stage 1: API readiness (up to 120s)
    deadline = time.time() + 120
    app = None
    while time.time() < deadline:
        app = ws.apps.get(app_name)
        api_state = app.app_status.state.value if app.app_status else ""
        deploy_state = (
            app.active_deployment.status.state.value
            if app.active_deployment and app.active_deployment.status
            else ""
        )
        if deploy_state == "FAILED":
            raise RuntimeError(f"Deployment of '{app_name}' failed — check app logs in Databricks for details")
        if api_state == "RUNNING" and deploy_state == "SUCCEEDED":
            break
        time.sleep(5)
    else:
        raise TimeoutError(f"App '{app_name}' did not reach RUNNING state within 120s")

    app_url = app.url
    if not app_url:
        raise RuntimeError(f"App '{app_name}' deployed successfully but has no URL — check the app in Databricks")

    # Stage 2: HTTP readiness (up to 60s)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            r = httpx.get(f"{app_url}/health", timeout=5.0)
            if r.status_code == 200:
                return app_url
        except Exception:
            pass
        time.sleep(5)

    return f"{app_url} (warning: health check timed out — try in 30 seconds)"
