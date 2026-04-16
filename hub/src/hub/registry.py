"""In-memory agent registry with filtering and status tracking."""

from __future__ import annotations

from typing import Literal

from .models import HubAgent


class AgentRegistry:
    """Thread-safe in-memory store for discovered agents."""

    def __init__(self) -> None:
        self._agents: dict[str, HubAgent] = {}

    def add(self, agent: HubAgent) -> None:
        """Add or update an agent entry."""
        self._agents[agent.id] = agent

    def get(self, agent_id: str) -> HubAgent | None:
        """Get a single agent by ID."""
        return self._agents.get(agent_id)

    def remove(self, agent_id: str) -> None:
        """Remove an agent. No-op if not found."""
        self._agents.pop(agent_id, None)

    def update_status(self, agent_id: str, status: Literal["online", "offline", "unknown"]) -> None:
        """Update an agent's status in-place."""
        agent = self._agents.get(agent_id)
        if agent is not None:
            self._agents[agent_id] = agent.model_copy(update={"status": status})

    def list(
        self,
        source: str | None = None,
        query: str | None = None,
    ) -> list[HubAgent]:
        """List agents with optional filtering by source and text query."""
        agents = list(self._agents.values())
        if source is not None:
            agents = [a for a in agents if a.source == source]
        if query is not None:
            q = query.lower()
            agents = [a for a in agents if q in a.name.lower() or q in a.description.lower()]
        return agents
