"""Packaged native ASGI entrypoint, shared by local run and Apps deployment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers


_NOT_DURABLE = (
    "The native launcher requires target='durable_agent_server' on the root agent or in the project's settings")


class _RelativeRedirects:
    """Make same-host redirect Locations path-only.

    The Apps proxy hands the app its loopback Host, so Starlette's absolute
    trailing-slash redirect (``/_apx/agent/`` -> 307) pointed at localhost.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        host = scope["type"] == "http" and Headers(scope=scope).get("host")
        if not host:
            return await self.app(scope, receive, send)
        own = f"{scope['scheme']}://{host}".encode()

        async def _send(message: Any) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (k, v[len(own):] or b"/") if k == b"location" and v.startswith(own) else (k, v)
                    for k, v in message["headers"]
                ]
            await send(message)

        await self.app(scope, receive, _send)


def create_app() -> Any:
    """Compile the current project's declared native runtime at server startup."""
    from ._agents import LlmAgent
    from ._defaults import _make_workspace_client
    from ._inspection import _load_agent_config
    from ._runtime_targets import compile_agent
    from ._settings import resolve_settings
    from ._wiring import resolve_agent

    pyproject = Path.cwd() / "pyproject.toml"
    toml = _load_agent_config(pyproject_path=pyproject)
    if toml is not None and "target" in toml.model_fields_set and toml.target != "durable_agent_server":
        raise ValueError(_NOT_DURABLE)  # before auth: no credentials for an incompatible runtime
    # Pin tool/config loading too, including reload workers and inherited shells.
    os.environ["APX_PYPROJECT"] = str(pyproject)
    ws = _make_workspace_client()
    agent = resolve_agent("agent:agent", toml, ws=ws)
    config = resolve_settings(agent, toml)
    if config.target != "durable_agent_server":
        raise ValueError(_NOT_DURABLE)
    model = os.environ.get("APX_MODEL", config.model)
    checkpointer = None
    if (os.environ.get("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL") == "true" and isinstance(agent, LlmAgent)
            and config.session is not None and config.session.type == "managed"):
        # agentbricks dev keeps sessions in-process; a declared store binds only at deploy (#1145).
        # ponytail: in-memory runtime store too, so recovery has nothing to replay from locally.
        from langgraph.checkpoint.memory import InMemorySaver

        config = config.model_copy(update={"session": None, "recovery": False})
        checkpointer = InMemorySaver()
    app = compile_agent(
        agent, config=config, model=model, checkpointer=checkpointer,
        service_ws=ws, session_store=os.environ.get("AGENT_SESSION_STORE"), project_root=Path.cwd(),
    )
    app.state.workspace_client = ws
    return app


def create_served_app() -> _RelativeRedirects:
    """The Apps-served app: ``create_app`` behind the proxy-Host redirect fix."""
    return _RelativeRedirects(create_app())


def main() -> None:
    """Start the native app on the port assigned by Databricks Apps."""
    import uvicorn

    uvicorn.run(
        "apx_agent._serve:create_served_app", factory=True, host="0.0.0.0",
        port=int(os.environ["DATABRICKS_APP_PORT"]), app_dir=str(Path.cwd()),
    )


if __name__ == "__main__":
    main()
