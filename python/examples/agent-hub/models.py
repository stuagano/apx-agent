from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

__version__ = "0.1.0"


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls):
        return cls(version=__version__)


class AgentTool(BaseModel):
    name: str
    description: str


class AgentCard(BaseModel):
    id: str
    name: str
    display_name: str
    description: str
    status: str  # "live" | "unreachable" | "stub"
    url: str
    tools: list[AgentTool]
    tags: list[str] = []
    mcp_endpoint: str | None = None
    last_seen: datetime | None = None
    supports_invoke: bool = False  # True if agent exposes POST /responses


class RegisterRequest(BaseModel):
    url: str
    tags: list[str] = []


class InvokeRequest(BaseModel):
    input: str
