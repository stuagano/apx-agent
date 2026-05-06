from apx_agent import create_app
from apx_agent._dev import build_dev_ui_router

from .agent_router import agent
from .router import router

app = create_app(agent)
app.include_router(router)
app.include_router(build_dev_ui_router())
