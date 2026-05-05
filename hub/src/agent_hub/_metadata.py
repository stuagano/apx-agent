from pathlib import Path

app_name = "agent-hub"
app_entrypoint = "agent_hub.backend.app:app"
app_slug = "agent_hub"
api_prefix = "/api"
dist_dir = Path(__file__).parent / "__dist__"