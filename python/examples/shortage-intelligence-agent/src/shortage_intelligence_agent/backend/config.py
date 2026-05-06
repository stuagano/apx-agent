from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM model serving endpoint
    agent_model: str = "databricks-claude-sonnet-4-6"

    # Genie space for internal demand orders (alternative to direct SQL)
    demand_genie_space_id: str = ""

    # Knowledge Assistant endpoint for market report validation
    ka_endpoint: str = ""

    # Unity Catalog tables — configure per environment
    historical_demand_table: str = ""   # catalog.schema.shortage_history
    demand_orders_table: str = ""        # catalog.schema.demand_orders
    parts_catalog_table: str = ""        # catalog.schema.parts_catalog

    # DigiKey API OAuth2 credentials
    digikey_client_id: str = ""
    digikey_client_secret: str = ""

    # Slack delivery webhooks
    slack_webhook_sourcing: str = ""
    slack_webhook_sales: str = ""

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
