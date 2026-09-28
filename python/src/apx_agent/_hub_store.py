from __future__ import annotations

from ._hub_models import AgentCard


class HubStore:
    """Per-principal in-memory registry of A2A agent cards.

    Keyed principal -> agent_id -> card so one caller's registrations are
    never visible to another (the hub's old process-global dict leaked
    across users in a multi-user App).
    """

    def __init__(self) -> None:
        self._by_principal: dict[str, dict[str, AgentCard]] = {}

    def put(self, principal: str, card: AgentCard) -> None:
        self._by_principal.setdefault(principal, {})[card.id] = card

    def list(self, principal: str) -> list[AgentCard]:
        return list(self._by_principal.get(principal, {}).values())

    def get(self, principal: str, agent_id: str) -> AgentCard | None:
        return self._by_principal.get(principal, {}).get(agent_id)

    def delete(self, principal: str, agent_id: str) -> bool:
        bucket = self._by_principal.get(principal)
        if bucket is None or agent_id not in bucket:
            return False
        del bucket[agent_id]
        return True


HUB_STORE = HubStore()
