"""Schema-driven config for the chatbot-contracts agent.

Deployment-specific values (catalog, schema, volumes, sub_agents) come from
environment variables. See .env.example. All other config lives in
agent.config.yaml, which is the only file a cloning team needs to edit for
system prompt, extraction schema, and demo questions.
"""

from __future__ import annotations

import importlib.resources
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Tables(BaseModel):
    primary: str
    ground_truth: str


class Volumes(BaseModel):
    raw: str
    uploads: str


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog: str
    schema: str
    tables: Tables
    volumes: Volumes
    model: str
    system_prompt: str
    demo_questions: list[str] = Field(default_factory=list)
    sub_agents: list[str] = Field(default_factory=list)
    extraction_schema: dict[str, Any]

    def qualified_table(self, name: str) -> str:
        table = getattr(self.tables, name)
        return f"{self.catalog}.{self.schema}.{table}"


def load_settings(path: Path) -> Settings:
    with path.open() as f:
        data = yaml.safe_load(f)

    # Env vars override YAML for deployment-specific values.
    for key, env_var in [("catalog", "CATALOG"), ("schema", "SCHEMA")]:
        if val := os.getenv(env_var):
            data[key] = val

    vols = data.setdefault("volumes", {})
    if val := os.getenv("VOLUMES_RAW"):
        vols["raw"] = val
    if val := os.getenv("VOLUMES_UPLOADS"):
        vols["uploads"] = val

    if val := os.getenv("SUB_AGENTS"):
        data["sub_agents"] = [u.strip() for u in val.split(",") if u.strip()]

    return Settings.model_validate(data)


def _find_config() -> Path:
    # Wheel install: config is bundled as package data (works deployed or locally)
    try:
        ref = importlib.resources.files("contract_parsing_agent") / "agent.config.yaml"
        with importlib.resources.as_file(ref) as p:
            if p.exists():
                return p
    except Exception:
        pass
    # Dev editable install: repo root is parents[3] from src/pkg/backend/config.py
    candidates: list[Path] = [
        Path(__file__).resolve().parents[3] / "agent.config.yaml",
        Path.cwd() / "agent.config.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "agent.config.yaml not found. Tried:\n" + "\n".join(str(p) for p in candidates)
    )


_DEFAULT_PATH = _find_config()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings(_DEFAULT_PATH)
