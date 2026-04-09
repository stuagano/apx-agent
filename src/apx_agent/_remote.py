"""RemoteDatabricksAgent — card-based discovery for remote agents.

Fetches an A2A agent card from ``/.well-known/agent.json``, extracts
name/description/skills metadata, and proxies ``run()``/``stream()``
calls to the remote agent.

Prefers ``DatabricksOpenAI.responses.create(model="apps/<name>")`` for
automatic OBO token forwarding through the Supervisor gateway. Falls
back to direct ``POST /responses`` when the app name cannot be resolved.

Extends ``BaseAgent`` so it can be composed in ``SequentialAgent``,
``ParallelAgent``, ``RouterAgent``, or ``HandoffAgent``.

Example::

    # From a full agent card URL
    remote = await RemoteDatabricksAgent.from_card_url(
        "https://data-inspector.workspace.databricksapps.com/.well-known/agent.json"
    )

    # From a Databricks App name
    remote = await RemoteDatabricksAgent.from_app_name("data-inspector")

    # Compose in a pipeline
    pipeline = SequentialAgent([local_analyzer, remote])
"""

from __future__ import annotations

import json as _json
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

from fastapi import Request

from ._agents import BaseAgent
from ._models import AgentCard, AgentTool, A2ASkill, Message

logger = logging.getLogger(__name__)


def _url_to_app_name(url: str) -> str | None:
    """Extract Databricks App name from URL.

    Pattern: ``https://<app-name>-<workspace-id>.cloud.databricksapps.com``
    or:      ``https://<app-name>.workspace.databricksapps.com``

    Returns None if the URL is not a Databricks Apps URL.
    """
    if not url or "databricksapps.com" not in url:
        return None
    try:
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        if parts:
            name_with_id = parts[0]
            segments = name_with_id.split("-")
            for i in range(len(segments) - 1, 0, -1):
                if segments[i].isdigit() and len(segments[i]) > 8:
                    return "-".join(segments[:i])
            return name_with_id
    except Exception:
        pass
    return None


class RemoteDatabricksAgent(BaseAgent):
    """A remote agent discovered via its A2A agent card.

    Parameters
    ----------
    card_url:
        Full URL to the agent card, e.g.
        ``https://data-inspector.workspace.databricksapps.com/.well-known/agent.json``
    app_name:
        Databricks App name (e.g. ``"data-inspector"``). If provided
        alongside ``card_url``, used for the ``model="apps/<name>"``
        shortcut via ``DatabricksOpenAI``.
    headers:
        Extra headers to forward on every request (merged with OBO
        headers extracted from the incoming FastAPI ``Request``).
    timeout:
        HTTP request timeout in seconds. Default 120.
    """

    def __init__(
        self,
        card_url: str,
        *,
        app_name: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._card_url = card_url
        self._base_url = card_url.rsplit("/.well-known/agent.json", 1)[0].rstrip("/")
        self._app_name = app_name or _url_to_app_name(self._base_url)
        self._extra_headers = headers or {}
        self._timeout = timeout
        self._card: AgentCard | None = None

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    async def from_card_url(
        cls,
        card_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> RemoteDatabricksAgent:
        """Create a RemoteDatabricksAgent from a full agent card URL.

        The card is fetched eagerly so metadata is available immediately.
        """
        agent = cls(card_url, headers=headers, timeout=timeout)
        await agent.init()
        return agent

    @classmethod
    async def from_app_name(
        cls,
        app_name: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> RemoteDatabricksAgent:
        """Create a RemoteDatabricksAgent from a Databricks App name.

        Constructs the card URL from ``DATABRICKS_HOST``::

            https://<host>/apps/<app_name>/.well-known/agent.json

        Raises ``ValueError`` if ``DATABRICKS_HOST`` is not set.
        """
        host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
        if not host:
            raise ValueError(
                "RemoteDatabricksAgent.from_app_name requires DATABRICKS_HOST. "
                "Use from_card_url() with a full URL instead."
            )
        card_url = f"{host}/apps/{app_name}/.well-known/agent.json"
        agent = cls(card_url, app_name=app_name, headers=headers, timeout=timeout)
        await agent.init()
        return agent

    # ------------------------------------------------------------------
    # Initialization — fetch agent card
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Fetch the agent card. Safe to call multiple times (idempotent)."""
        if self._card is not None:
            return
        await self._fetch_card()

    async def _fetch_card(self) -> None:
        from httpx import AsyncClient

        async with AsyncClient(timeout=10.0) as client:
            response = await client.get(self._card_url, headers=self._extra_headers)
            response.raise_for_status()
            data = response.json()

        self._card = AgentCard(
            name=data.get("name", "remote-agent"),
            description=data.get("description", ""),
            url=data.get("url", self._base_url),
            skills=[
                A2ASkill(
                    id=s.get("id", s.get("name", "")),
                    name=s.get("name", ""),
                    description=s.get("description", ""),
                )
                for s in data.get("skills", [])
            ],
        )

        # Update base URL from card if provided
        if self._card.url:
            self._base_url = self._card.url.rstrip("/")

        # Try to infer app name if not already set
        if not self._app_name:
            self._app_name = _url_to_app_name(self._base_url)

        logger.info(
            "RemoteDatabricksAgent initialized: name=%s, app_name=%s, skills=%d",
            self._card.name,
            self._app_name,
            len(self._card.skills),
        )

    # ------------------------------------------------------------------
    # Metadata accessors
    # ------------------------------------------------------------------

    @property
    def card(self) -> AgentCard | None:
        return self._card

    @property
    def name(self) -> str:
        return self._card.name if self._card else "remote-agent"

    @property
    def description(self) -> str:
        return self._card.description if self._card else ""

    @property
    def app_name(self) -> str | None:
        return self._app_name

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    async def run(self, messages: list[Message], request: Request) -> str:
        await self.init()
        obo_headers = self._obo_headers(request)

        # Try DatabricksOpenAI first (automatic OBO via Supervisor)
        if self._app_name:
            try:
                return await self._call_via_sdk(messages, obo_headers)
            except Exception as exc:
                logger.warning(
                    "DatabricksOpenAI call to apps/%s failed (%s), falling back to direct HTTP",
                    self._app_name,
                    exc,
                )

        # Fallback: direct POST /responses
        return await self._call_via_http(messages, obo_headers)

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        await self.init()
        obo_headers = self._obo_headers(request)

        # Try DatabricksOpenAI first
        if self._app_name:
            try:
                text = await self._call_via_sdk(messages, obo_headers)
                yield text
                return
            except Exception as exc:
                logger.warning(
                    "DatabricksOpenAI stream to apps/%s failed (%s), falling back to direct HTTP",
                    self._app_name,
                    exc,
                )

        # Fallback: direct HTTP with SSE parsing
        async for chunk in self._stream_via_http(messages, obo_headers):
            yield chunk

    def collect_tools(self) -> list[AgentTool]:
        """Remote agents don't expose local tools."""
        return []

    async def fetch_remote_tools(self) -> list[AgentTool]:
        """Return the remote agent's skills as AgentTool descriptors."""
        await self.init()
        if not self._card:
            return []

        return [
            AgentTool(
                name=skill.name.replace("-", "_").replace(" ", "_"),
                description=skill.description,
                input_schema={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Message to send"},
                    },
                    "required": ["message"],
                },
                output_schema={"type": "string"},
                sub_agent_url=self._base_url,
            )
            for skill in self._card.skills
        ]

    # ------------------------------------------------------------------
    # Internal: OBO header extraction
    # ------------------------------------------------------------------

    def _obo_headers(self, request: Request) -> dict[str, str]:
        """Extract OBO-relevant headers from the incoming request."""
        headers = dict(self._extra_headers)
        for key in ("Authorization", "X-Forwarded-Access-Token", "X-Forwarded-Host"):
            value = request.headers.get(key, "")
            if value:
                headers[key] = value
        return headers

    # ------------------------------------------------------------------
    # Internal: DatabricksOpenAI SDK path
    # ------------------------------------------------------------------

    async def _call_via_sdk(
        self,
        messages: list[Message],
        obo_headers: dict[str, str],
    ) -> str:
        """Call via ``DatabricksOpenAI.responses.create(model="apps/<name>")``."""
        from databricks_openai import AsyncDatabricksOpenAI

        client = AsyncDatabricksOpenAI()
        response = await client.responses.create(
            model=f"apps/{self._app_name}",
            input=[
                {"type": "message", "role": m.role, "content": m.content}
                for m in messages
            ],
        )
        return response.output_text

    # ------------------------------------------------------------------
    # Internal: direct HTTP path
    # ------------------------------------------------------------------

    async def _call_via_http(
        self,
        messages: list[Message],
        headers: dict[str, str],
    ) -> str:
        """Direct POST /responses fallback."""
        from httpx import AsyncClient

        payload = {
            "input": [{"role": m.role, "content": m.content} for m in messages],
        }

        async with AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/responses",
                json=payload,
                headers={"Content-Type": "application/json", **headers},
            )

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Remote agent {self.name} returned {resp.status_code}: {resp.text}"
            )

        data = resp.json()
        try:
            return data["output"][0]["content"][0]["text"]
        except (KeyError, IndexError):
            return _json.dumps(data)

    async def _stream_via_http(
        self,
        messages: list[Message],
        headers: dict[str, str],
    ) -> AsyncGenerator[str, None]:
        """Direct POST /responses with stream=true, parsing SSE."""
        from httpx import AsyncClient

        payload = {
            "input": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }

        async with AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/responses",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    **headers,
                },
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"Remote agent {self.name} stream returned {resp.status_code}: {body.decode()}"
                    )

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload_str = line[6:].strip()
                    if payload_str == "[DONE]":
                        return

                    try:
                        event: dict[str, Any] = _json.loads(payload_str)
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            yield delta
                        elif isinstance(event.get("text"), str):
                            yield event["text"]
                    except _json.JSONDecodeError:
                        if payload_str:
                            yield payload_str
