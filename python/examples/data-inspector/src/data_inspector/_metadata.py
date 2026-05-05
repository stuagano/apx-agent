from pathlib import Path

app_name = "mcp-data-inspector"
app_entrypoint = "data_inspector.backend.app:app"
app_slug = "data_inspector"
api_prefix = "/api"
dist_dir = Path(__file__).parent / "__dist__"