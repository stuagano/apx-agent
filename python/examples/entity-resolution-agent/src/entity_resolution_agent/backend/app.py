from apx_agent import create_app

from .agent_router import agent
from .router import router

app = create_app(agent)
app.include_router(router)
