"""Deployment config — all environment-specific values come from env vars."""
from __future__ import annotations

import os
from functools import lru_cache

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Demo defaults, matching the README setup (main.eligibility_demo, a California
# program, a 60-day residency-proof window). Override each via its env var.
_DEFAULT_CATALOG = "main"
_DEFAULT_SCHEMA = "eligibility_demo"
_DEFAULT_STATE_CODE = "CA"
_DEFAULT_PROGRAM_NAME = "Community Assistance Program"
_DEFAULT_RESIDENCY_RECENCY_DAYS = "60"


class Settings:
    def __init__(self) -> None:
        self.catalog = os.getenv("CATALOG", _DEFAULT_CATALOG)
        self.schema = os.getenv("SCHEMA", _DEFAULT_SCHEMA)
        self.state_code = os.getenv("STATE_CODE", _DEFAULT_STATE_CODE).upper()
        self.program_name = os.getenv("PROGRAM_NAME", _DEFAULT_PROGRAM_NAME)
        self.residency_recency_days = int(os.getenv("RESIDENCY_RECENCY_DAYS", _DEFAULT_RESIDENCY_RECENCY_DAYS))

    def table(self, name: str) -> str:
        return f"{self.catalog}.{self.schema}.{name}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
