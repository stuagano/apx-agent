from pathlib import Path

app_name = "contract-parsing-agent"
app_entrypoint = "contract_parsing_agent.backend.app:app"
app_slug = "contract_parsing_agent"
api_prefix = "/api"
dist_dir = Path(__file__).parent / "__dist__"