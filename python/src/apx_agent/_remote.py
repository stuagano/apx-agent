"""RemoteDatabricksAgent — card-based discovery for remote agents.

Fetches an A2A agent card from ``/.well-known/agent.json``, extracts
name/description/skills metadata, and proxies ``run()``/``stream()``
calls to the remote agent.

Prefers ``DatabricksOpenAI.responses.create(model="apps/<name>")`` for
automatic OBO token forwarding through the Supervisor gateway. Falls
back to direct ``POST /invocations`` when the app name cannot be resolved.

Extends ``BaseAgent`` so it can be composed in ``SequentialAgent``,
``ParallelAgent``, ``RouterAgent``, or ``HandoffAgent``.

Example::

    # From a base URL or a full agent card URL (both accepted, #441)
    remote = await RemoteDatabricksAgent.from_card_url(
        "https://data-inspector.workspace.databricksapps.com"
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
from typing import Any, cast
from urllib.parse import urlparse

from fastapi import Request

from ._agents import BaseAgent
from ._models import (
    A2ASkill,
    AgentCard,
    AgentTool,
    Message,
    message_input_schema,
    sanitize_tool_schema,
    structured_input_schema,
)

logger = logging.getLogger(__name__)


# Host suffixes whose origins are trusted to receive a forwarded OBO token,
# even when they differ from the configured ``card_url`` origin. Covers the
# ``from_app_name`` case where the card is fetched from the workspace host but
# ``card.url`` points at the app's own ``*.databricksapps.com`` address.
_TRUSTED_HOST_SUFFIXES = (".databricksapps.com",)

_WELL_KNOWN_CARD_PATH = "/.well-known/agent.json"


def _normalize_card_url(url: str) -> str:
    """Accept a base URL or a full card URL; return the full card URL.

    Mirrors the declarative ``sub_agents`` path, which appends the
    well-known path to a declared base URL (#441), so both entry points
    share one contract.
    """
    url = url.rstrip("/")
    if url.endswith(_WELL_KNOWN_CARD_PATH):
        return url
    return f"{url}{_WELL_KNOWN_CARD_PATH}"


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _trusted_origin(candidate: str, configured: str) -> bool:
    """True if ``candidate``'s origin is safe to forward credentials to.

    Safe means the origin matches the operator-supplied ``configured`` origin
    (same scheme + host + port), or its host falls under a trusted Databricks
    Apps suffix. A scheme downgrade (https → http) or alternate port of the same
    host is treated as untrusted.
    """
    try:
        c = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        ref = urlparse(configured if "://" in configured else f"https://{configured}")
    except Exception:
        return False
    if not c.hostname:
        return False

    same_origin = (
        c.scheme == ref.scheme
        and c.hostname == ref.hostname
        and (c.port or _default_port(c.scheme)) == (ref.port or _default_port(ref.scheme))
    )
    if same_origin:
        return True

    # Trusted Databricks Apps host, but only over https (never accept a downgrade).
    if c.scheme == "https" and any(
        c.hostname == suffix.lstrip(".") or c.hostname.endswith(suffix)
        for suffix in _TRUSTED_HOST_SUFFIXES
    ):
        return True

    return False


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


def _reply_text(data: Any, *, url: str, agent_name: str) -> str:
    """Extract the assistant text from either /invocations reply shape (#438).

    A deployed AgentServer runtime answers in the Responses shape
    (``{"output": [{"content": [{"text": ...}]}]}``); a locally served
    ``create_app`` peer answers in the MLflow ChatAgent shape
    (``{"messages": [..., {"role": "assistant", "content": ...}]}``). Accept
    both. Anything else is a contract violation — raise naming the URL and
    the shape received (with a truncated body for debugging) instead of
    silently returning a JSON blob the parent LLM would relay as an "answer".
    """
    try:
        # The final answer is the LAST message item — earlier output items
        # are often function_call/function_call_output entries.
        for item in reversed(data["output"]):
            if item.get("type") == "message" or "content" in item:
                return item["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        pass
    msgs = data.get("messages") if isinstance(data, dict) else None
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if (
                isinstance(m, dict)
                and m.get("role") == "assistant"
                and isinstance(m.get("content"), str)
                and m["content"]
            ):
                return m["content"]
    shape = sorted(data) if isinstance(data, dict) else type(data).__name__
    raise RuntimeError(
        f"Remote agent {agent_name} at {url} replied with an unrecognized "
        f"shape (top-level: {shape}); expected Responses ('output') or "
        f"ChatAgent ('messages'). Body (truncated): {_json.dumps(data)[:500]}"
    )


class RemoteDatabricksAgent(BaseAgent):
    """A remote agent discovered via its A2A agent card.

    Parameters
    ----------
    card_url:
        URL of the remote agent — either its base URL
        (``https://data-inspector.workspace.databricksapps.com``) or the
        full agent card URL ending in ``/.well-known/agent.json``. The
        well-known path is appended when missing (#441).
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
        self._card_url = _normalize_card_url(card_url)
        self._base_url = self._card_url[: -len(_WELL_KNOWN_CARD_PATH)]
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
        """Create a RemoteDatabricksAgent from a base URL or full card URL.

        Accepts either form — ``/.well-known/agent.json`` is appended when
        missing (#441). The card is fetched eagerly so metadata is
        available immediately.
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
        host = os.environ.get("DATABRICKS_HOST")
        if not host:
            raise ValueError(
                "RemoteDatabricksAgent.from_app_name requires DATABRICKS_HOST. "
                "Use from_card_url() with a full URL instead."
            )
        host = host.rstrip("/")
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

    async def _init_quietly(self) -> None:
        """init(), but card-fetch failure is non-fatal.

        A private Databricks App 302s the unauthenticated card request, yet
        run()/stream() still work — they forward the caller's OBO token and
        the base URL is already known from the constructor.
        """
        try:
            await self.init()
        except Exception as e:
            logger.warning(
                "Agent card fetch failed for %s: %s — proceeding without card",
                self._card_url,
                e,
            )

    async def _fetch_card(self) -> None:
        from httpx import AsyncClient

        async with AsyncClient(timeout=10.0) as client:
            response = await client.get(self._card_url, headers=self._extra_headers)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as e:
                # A dev-UI landing page answers 200 text/html here; a naked
                # JSONDecodeError gives no hint what was hit (#441).
                content_type = response.headers.get("content-type", "unknown")
                raise RuntimeError(
                    f"Expected agent card JSON at {self._card_url}, got "
                    f"{content_type} (HTTP {response.status_code}). "
                    f"Body (truncated): {response.text[:200]!r}"
                ) from e

        self._card = AgentCard(
            name=data.get("name", "remote-agent"),
            description=data.get("description", ""),
            url=data.get("url", self._base_url),
            skills=[
                A2ASkill(
                    id=s.get("id", s.get("name", "")),
                    name=s.get("name", ""),
                    description=s.get("description", ""),
                    # Carry the card's real schemas — dropping them here is
                    # what collapsed every remote tool to {message} (#442).
                    inputSchema=s.get("inputSchema"),
                    outputSchema=s.get("outputSchema"),
                )
                for s in data.get("skills", [])
            ],
        )

        # Update base URL from card only when the card's origin is trusted
        # (matches the configured card_url origin or a Databricks Apps host).
        # A malicious card could otherwise redirect the forwarded OBO token to
        # an arbitrary host, so an untrusted card.url is ignored.
        if self._card.url:
            candidate = self._card.url.rstrip("/")
            if _trusted_origin(candidate, self._card_url):
                self._base_url = candidate
            else:
                logger.warning(
                    "Ignoring card.url %s for %s: origin differs from configured "
                    "card_url %s and is not a trusted host; keeping %s",
                    candidate,
                    self._card.name,
                    self._card_url,
                    self._base_url,
                )

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
        await self._init_quietly()
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

        # Fallback: direct POST /invocations
        return await self._call_via_http(messages, obo_headers)

    async def stream(self, messages: list[Message], request: Request) -> AsyncGenerator[str, None]:
        await self._init_quietly()
        obo_headers = self._obo_headers(request)

        # Try DatabricksOpenAI first — true streaming, deltas relayed as
        # they arrive (#447). Fall back to HTTP only when nothing has been
        # yielded yet; a mid-stream failure must not replay from the start.
        if self._app_name:
            emitted = False
            try:
                async for chunk in self._stream_via_sdk(messages):
                    emitted = True
                    yield chunk
                return
            except Exception as exc:
                if emitted:
                    raise
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
        """Return the remote agent's skills as AgentTool descriptors.

        Each skill's ``inputSchema``/``outputSchema`` is carried through
        (sanitized — an untrusted card must not 500 the orchestrator's LLM
        calls via ``additionalProperties: true``, #439). The ``{message}``
        wrapper is the *fallback* for skills that advertise no usable
        structured schema, not the blanket interface (#442).
        """
        await self.init()
        if not self._card:
            return []

        tools: list[AgentTool] = []
        for skill in self._card.skills:
            structured = structured_input_schema(skill.inputSchema)
            if isinstance(skill.outputSchema, dict) and skill.outputSchema:
                output_schema = sanitize_tool_schema(skill.outputSchema)
            else:
                output_schema = {"type": "string"}
            tools.append(
                AgentTool(
                    name=skill.name.replace("-", "_").replace(" ", "_"),
                    description=skill.description,
                    input_schema=structured if structured is not None else message_input_schema(),
                    output_schema=output_schema,
                    sub_agent_url=self._base_url,
                )
            )
        return tools

    # ------------------------------------------------------------------
    # Internal: OBO header extraction
    # ------------------------------------------------------------------

    def _obo_headers(self, request: Request) -> dict[str, str]:
        """Extract OBO-relevant headers from the incoming request.

        Credential headers (the user's OBO token) are only forwarded when the
        outbound ``_base_url`` is a trusted origin relative to the operator-
        supplied ``card_url``. This is defense-in-depth against a base URL that
        was redirected to an untrusted host.
        """
        headers = dict(self._extra_headers)
        forward_credentials = _trusted_origin(self._base_url, self._card_url)
        if not forward_credentials:
            logger.warning(
                "Not forwarding OBO credentials to untrusted base_url %s (configured card_url %s)",
                self._base_url,
                self._card_url,
            )
        for key in ("Authorization", "X-Forwarded-Access-Token", "X-Forwarded-Host"):
            if not forward_credentials and key in ("Authorization", "X-Forwarded-Access-Token"):
                continue
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
        # Use EasyInputMessage form (no "type": "message") so string content
        # survives. With type="message" the Responses API expects content as a
        # list of InputContent parts and drops a plain string.
        response = await client.responses.create(
            model=f"apps/{self._app_name}",
            # The EasyInputMessage dict form (above comment) is intentional; the
            # openai stub types `input` narrowly, so cast past it.
            input=cast(
                Any,
                [{"role": m.role, "content": m.content} for m in messages],
            ),
        )
        return response.output_text

    async def _stream_via_sdk(
        self,
        messages: list[Message],
    ) -> AsyncGenerator[str, None]:
        """Stream via ``responses.create(model="apps/<name>", stream=True)``.

        Relays ``response.output_text.delta`` events as they arrive, so
        first-token latency matches the remote agent's instead of the old
        buffer-everything-then-yield-once behavior (#447).
        """
        from databricks_openai import AsyncDatabricksOpenAI

        client = AsyncDatabricksOpenAI()
        stream = await client.responses.create(
            model=f"apps/{self._app_name}",
            # Same EasyInputMessage dict form + cast as _call_via_sdk.
            input=cast(
                Any,
                [{"role": m.role, "content": m.content} for m in messages],
            ),
            stream=True,
        )
        async for event in stream:
            if event.type == "response.output_text.delta" and event.delta:
                yield event.delta

    # ------------------------------------------------------------------
    # Internal: direct HTTP path
    # ------------------------------------------------------------------

    async def _call_via_http(
        self,
        messages: list[Message],
        headers: dict[str, str],
    ) -> str:
        """Direct POST /responses fallback (with /invocations fallback).

        The payload here is Responses-protocol shaped (``input`` list in), so
        ``/responses`` is the natural target — a deployed AgentServer's
        ``/invocations`` speaks the ChatAgent protocol (``messages`` key) and
        would see zero messages and 400 (live-proven app-to-app). Peers that
        don't serve ``/responses`` (older or apx ``create_app`` servers, which
        accept the input shape on ``/invocations`` since #438) get the
        fallback POST. Reply parsing accepts both shapes either way.
        """
        from httpx import AsyncClient

        payload = {
            "input": [{"role": m.role, "content": m.content} for m in messages],
        }
        url = f"{self._base_url}/responses"

        async with AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", **headers},
            )
            if resp.status_code in (404, 405):
                url = f"{self._base_url}/invocations"
                resp = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json", **headers},
                )

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Remote agent {self.name} returned {resp.status_code}: {resp.text}"
            )

        return _reply_text(resp.json(), url=url, agent_name=self.name)

    async def _stream_via_http(
        self,
        messages: list[Message],
        headers: dict[str, str],
    ) -> AsyncGenerator[str, None]:
        """Direct POST /invocations with stream=true, parsing SSE."""
        from httpx import AsyncClient

        payload = {
            "input": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }

        async with AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/invocations",
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
                        elif (
                            isinstance(delta, dict)
                            and isinstance(delta.get("content"), str)
                            and delta["content"]
                        ):
                            # MLflow ChatAgentChunk: delta is a message dict —
                            # what a locally served create_app peer streams (#438).
                            yield delta["content"]
                        elif isinstance(event.get("text"), str):
                            yield event["text"]
                    except _json.JSONDecodeError:
                        if payload_str:
                            yield payload_str
