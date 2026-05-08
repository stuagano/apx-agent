from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    databricks_host: str = ""
    databricks_client_id: str = ""
    databricks_client_secret: str = ""
    app_url: str = ""
    slack_signing_secret: str = ""
    slack_bot_token: str = ""


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
