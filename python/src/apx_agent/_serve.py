"""Packaged native ASGI entrypoint, shared by local run and Apps deployment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def create_app() -> Any:
    """Compile the current project's declared native runtime at server startup."""
    from ._defaults import _make_workspace_client
    from ._inspection import _load_agent_config
    from ._runtime_targets import compile_agent
    from ._wiring import resolve_agent

    pyproject = Path.cwd() / "pyproject.toml"
    config = _load_agent_config(pyproject_path=pyproject)
    if config is None or config.target != "durable_agent_server":
        raise ValueError("The native launcher requires target = 'durable_agent_server' in [tool.apx.agent]")
    # Pin tool/config loading too, including reload workers and inherited shells.
    os.environ["APX_PYPROJECT"] = str(pyproject)
    ws = _make_workspace_client()
    agent = resolve_agent("agent:agent", config, ws=ws)
    app = compile_agent(
        agent, config=config, model=os.environ.get("APX_MODEL", config.model),
        service_ws=ws, session_store=os.environ.get("AGENT_SESSION_STORE"),
    )
    app.state.workspace_client = ws
    return app


def main() -> None:
    """Start the native app on the port assigned by Databricks Apps."""
    import uvicorn

    uvicorn.run(
        "apx_agent._serve:create_app", factory=True, host="0.0.0.0",
        port=int(os.environ["DATABRICKS_APP_PORT"]), app_dir=str(Path.cwd()),
    )


if __name__ == "__main__":
    main()
