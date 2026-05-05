from __future__ import annotations

from datetime import datetime
from importlib.metadata import version

from pydantic import BaseModel


class AgentTool(BaseModel):
    name: str
    description: str


class AgentCard(BaseModel):
    id: str
    name: str
    display_name: str
    description: str
    status: str
    url: str
    tools: list[AgentTool]
    tags: list[str] = []
    mcp_endpoint: str | None = None
    last_seen: datetime | None = None
    supports_invoke: bool = False


class RegisterRequest(BaseModel):
    url: str
    tags: list[str] = []


class InvokeRequest(BaseModel):
    input: str


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls) -> "VersionOut":
        try:
            v = version("agent-hub")
        except Exception:
            v = "dev"
        return cls(version=v)
