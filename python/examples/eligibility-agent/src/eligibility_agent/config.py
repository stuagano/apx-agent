"""Deployment config — all environment-specific values come from env vars."""
from __future__ import annotations

import os
from functools import lru_cache

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Settings:
    def __init__(self) -> None:
        self.catalog = os.getenv("CATALOG", "main")
        self.schema = os.getenv("SCHEMA", "eligibility_demo")
        self.state_code = os.getenv("STATE_CODE", "CA").upper()
        self.program_name = os.getenv("PROGRAM_NAME", "Community Assistance Program")
        self.residency_recency_days = int(os.getenv("RESIDENCY_RECENCY_DAYS", "60"))

    def table(self, name: str) -> str:
        return f"{self.catalog}.{self.schema}.{name}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
