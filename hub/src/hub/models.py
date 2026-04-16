"""Hub data models — unified agent representation across all sources."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, computed_field


class HubSkill(BaseModel):
    name: str
    description: str


class HubAgent(BaseModel):
    name: str
    description: str
    source: Literal["apx", "serving_endpoint", "genie_space"]
    url: str | None = None
    skills: list[HubSkill] = []
    status: Literal["online", "offline", "unknown"] = "unknown"
    metadata: dict[str, Any] = {}

    @computed_field
    @property
    def id(self) -> str:
        """Deterministic ID: hash of source + name."""
        raw = f"{self.source}:{self.name}"
        return f"{self.source}:{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


class RegisterRequest(BaseModel):
    url: str


class RegisterResponse(BaseModel):
    id: str


class ChatRequest(BaseModel):
    agent_id: str
    message: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    agent_id: str
    message: str
    conversation_id: str
