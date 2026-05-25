from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM model serving endpoint
    agent_model: str = "databricks-claude-opus-4-7"

    # SQL warehouse ID (run_sql auto-discovers if unset)
    databricks_warehouse_id: str = ""

    # Unity Catalog tables — configure per environment
    historical_demand_table: str = ""   # catalog.schema.shortage_history
    demand_orders_table: str = ""        # catalog.schema.demand_orders
    parts_catalog_table: str = ""        # catalog.schema.parts_catalog

    # Genie space for natural-language SQL exploration
    demand_genie_space_id: str = ""

    # Vector Search for market intelligence validation
    vs_endpoint: str = ""
    vs_index: str = ""

    # DigiKey API OAuth2 credentials
    digikey_client_id: str = ""
    digikey_client_secret: str = ""

    # Slack delivery webhooks
    slack_webhook_sourcing: str = ""
    slack_webhook_sales: str = ""

    # Watchdog compliance integration (optional — noop when unset)
    watchdog_mcp_url: str = ""
    watchdog_violations_table: str = ""  # catalog.schema.watchdog_violations

    # Lakebase session persistence (optional — stateless when unset)
    # Use postgresql+psycopg://user@host:5432/dbname — password injected via OAuth
    lakebase_connection_url: str = ""
    lakebase_instance_name: str = ""   # Lakebase instance name for token rotation
    lakebase_table: str = "apx_sessions"

    def require(self, *fields: str) -> None:
        missing = [f for f in fields if not getattr(self, f)]
        if missing:
            raise ValueError(f"Required env vars not configured: {', '.join(f.upper() for f in missing)}")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
