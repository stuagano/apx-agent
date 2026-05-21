from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jira_base_url: str = ""
    jira_service_account_email: str = ""
    jira_api_token: str = ""
    jira_webhook_secret: str = ""
    data_triage_job_id: int = 0
    data_inspector_url: str = "http://localhost:9000"

    def require_webhook_secret(self) -> None:
        if not self.jira_webhook_secret:
            raise ValueError("JIRA_WEBHOOK_SECRET is not configured")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
