"""Native APX DurableAgentServer target. Install databricks-agentbricks to use it."""
import os
from pathlib import Path

os.environ.setdefault("APX_RUNTIME", "apps")

from agent import agent
from apx_agent import compile_agent
from apx_agent._defaults import _make_workspace_client
from apx_agent._inspection import _load_agent_config
from apx_agent._settings import resolve_settings

config = resolve_settings(agent, _load_agent_config())
ws = _make_workspace_client()
app = compile_agent(
    agent, config=config, target="durable_agent_server",
    model=os.environ.get("APX_MODEL", config.model), service_ws=ws,
    session_store=os.environ.get("AGENT_SESSION_STORE"), project_root=Path.cwd(),
)
