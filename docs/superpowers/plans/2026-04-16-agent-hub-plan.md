# Agent Hub — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Databricks App that serves as a unified catalog and chat interface for all agents (apx-agent apps, workspace serving endpoints, Genie spaces) in a workspace.

**Architecture:** A Python app built with apx-agent's own `create_app`. The hub maintains an in-memory registry of agents from three sources. A vanilla HTML/JS/CSS frontend provides a searchable catalog and a chat interface that proxies messages to the selected agent. No build step for the frontend.

**Tech Stack:** Python, FastAPI (via apx-agent), Databricks SDK, httpx, vanilla HTML/JS/CSS

**Spec:** `docs/superpowers/specs/2026-04-16-agent-hub-design.md`

---

## File Map

| File | Responsibility |
|------|----------------|
| `hub/src/hub/__init__.py` | Package marker |
| `hub/src/hub/models.py` | `HubAgent`, `HubSkill`, `RegisterRequest`, `RegisterResponse`, `ChatRequest`, `ChatResponse` |
| `hub/src/hub/registry.py` | `AgentRegistry` — in-memory agent store, list/get/register, health check, filtering |
| `hub/src/hub/discovery.py` | `discover_serving_endpoints()`, `discover_genie_spaces()` — workspace scanning |
| `hub/src/hub/chat.py` | `proxy_chat()` — routes chat messages to apx agents, serving endpoints, or Genie spaces |
| `hub/src/hub/app.py` | FastAPI app with hub routes, background tasks, static file serving |
| `hub/tests/test_models.py` | Tests for model construction and ID generation |
| `hub/tests/test_registry.py` | Tests for registry add/get/list/filter/health |
| `hub/tests/test_discovery.py` | Tests for endpoint and Genie space discovery |
| `hub/tests/test_chat.py` | Tests for chat proxy routing |
| `hub/tests/test_app.py` | Integration tests for API endpoints |
| `hub/frontend/index.html` | SPA shell |
| `hub/frontend/style.css` | Styles for catalog and chat |
| `hub/frontend/app.js` | Catalog grid, chat UI, agent switching |
| `hub/pyproject.toml` | Package config with apx-agent local path dep |
| `hub/app.yml` | Databricks App manifest |
| `hub/databricks.yml` | DABs bundle config |

---

## Task 1: Project scaffold and models

**Files:**
- Create: `hub/src/hub/__init__.py`
- Create: `hub/src/hub/models.py`
- Create: `hub/tests/__init__.py`
- Create: `hub/tests/test_models.py`
- Create: `hub/pyproject.toml`

- [ ] **Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "agent-hub"
version = "0.1.0"
description = "Unified catalog and chat interface for agents on Databricks"
requires-python = ">=3.11"
license = "Apache-2.0"
dependencies = [
    "apx-agent @ file:///${PROJECT_ROOT}/../python",
    "databricks-sdk>=0.74.0",
    "httpx>=0.27.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24.0",
]

[tool.hatch.build.targets.wheel]
packages = ["src/hub"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.apx.agent]
name = "agent-hub"
description = "Unified catalog and chat for all workspace agents"
model = "databricks-meta-llama-3-3-70b-instruct"
```

- [ ] **Step 2: Create package init**

```python
# hub/src/hub/__init__.py
```

(Empty file — package marker.)

- [ ] **Step 3: Write failing tests for models**

```python
# hub/tests/__init__.py
```

```python
# hub/tests/test_models.py
from hub.models import HubAgent, HubSkill, RegisterRequest, RegisterResponse, ChatRequest, ChatResponse


def test_hub_skill_construction():
    skill = HubSkill(name="query", description="Run a SQL query")
    assert skill.name == "query"
    assert skill.description == "Run a SQL query"


def test_hub_agent_construction():
    agent = HubAgent(
        name="billing-agent",
        description="Handles billing questions",
        source="apx",
        url="https://billing-agent.workspace.databricksapps.com",
    )
    assert agent.name == "billing-agent"
    assert agent.source == "apx"
    assert agent.status == "unknown"
    assert agent.skills == []
    assert agent.metadata == {}
    # ID should be deterministic based on source + name
    assert agent.id != ""


def test_hub_agent_id_deterministic():
    a1 = HubAgent(name="test", description="d", source="apx")
    a2 = HubAgent(name="test", description="d", source="apx")
    assert a1.id == a2.id


def test_hub_agent_id_differs_by_source():
    a1 = HubAgent(name="test", description="d", source="apx")
    a2 = HubAgent(name="test", description="d", source="serving_endpoint")
    assert a1.id != a2.id


def test_register_request():
    req = RegisterRequest(url="https://my-agent.workspace.databricksapps.com")
    assert req.url == "https://my-agent.workspace.databricksapps.com"


def test_register_response():
    resp = RegisterResponse(id="apx:billing-agent")
    assert resp.id == "apx:billing-agent"


def test_chat_request():
    req = ChatRequest(agent_id="apx:billing", message="Hello")
    assert req.conversation_id is None


def test_chat_response():
    resp = ChatResponse(agent_id="apx:billing", message="Hi!", conversation_id="conv-1")
    assert resp.conversation_id == "conv-1"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd hub && uv sync --group dev && uv run pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.models'`

- [ ] **Step 5: Implement models**

```python
# hub/src/hub/models.py
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

    @computed_field  # type: ignore[prop-decorator]
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd hub && uv run pytest tests/test_models.py -v`
Expected: All 8 tests PASS

- [ ] **Step 7: Commit**

```bash
git add hub/pyproject.toml hub/src/hub/__init__.py hub/src/hub/models.py hub/tests/__init__.py hub/tests/test_models.py
git commit -m "feat(hub): project scaffold and unified agent models"
```

---

## Task 2: Agent registry

**Files:**
- Create: `hub/src/hub/registry.py`
- Create: `hub/tests/test_registry.py`

- [ ] **Step 1: Write failing tests for registry**

```python
# hub/tests/test_registry.py
import pytest
from hub.models import HubAgent
from hub.registry import AgentRegistry


@pytest.fixture
def registry() -> AgentRegistry:
    return AgentRegistry()


@pytest.fixture
def sample_agent() -> HubAgent:
    return HubAgent(
        name="billing-agent",
        description="Handles billing",
        source="apx",
        url="https://billing.example.com",
        status="online",
    )


def test_empty_registry(registry: AgentRegistry):
    assert registry.list() == []


def test_add_and_get(registry: AgentRegistry, sample_agent: HubAgent):
    registry.add(sample_agent)
    assert registry.get(sample_agent.id) == sample_agent


def test_add_duplicate_updates(registry: AgentRegistry):
    agent_v1 = HubAgent(name="a", description="v1", source="apx", status="online")
    agent_v2 = HubAgent(name="a", description="v2", source="apx", status="offline")
    registry.add(agent_v1)
    registry.add(agent_v2)
    assert len(registry.list()) == 1
    assert registry.get(agent_v1.id).description == "v2"


def test_get_missing_returns_none(registry: AgentRegistry):
    assert registry.get("nonexistent") is None


def test_list_all(registry: AgentRegistry):
    registry.add(HubAgent(name="a1", description="d", source="apx"))
    registry.add(HubAgent(name="a2", description="d", source="serving_endpoint"))
    registry.add(HubAgent(name="a3", description="d", source="genie_space"))
    assert len(registry.list()) == 3


def test_list_filter_by_source(registry: AgentRegistry):
    registry.add(HubAgent(name="a1", description="d", source="apx"))
    registry.add(HubAgent(name="a2", description="d", source="serving_endpoint"))
    registry.add(HubAgent(name="a3", description="d", source="genie_space"))
    apx_agents = registry.list(source="apx")
    assert len(apx_agents) == 1
    assert apx_agents[0].name == "a1"


def test_list_filter_by_query(registry: AgentRegistry):
    registry.add(HubAgent(name="billing-agent", description="Handles billing", source="apx"))
    registry.add(HubAgent(name="data-triage", description="Investigates data issues", source="apx"))
    results = registry.list(query="billing")
    assert len(results) == 1
    assert results[0].name == "billing-agent"


def test_list_query_searches_description(registry: AgentRegistry):
    registry.add(HubAgent(name="agent-x", description="Handles billing questions", source="apx"))
    results = registry.list(query="billing")
    assert len(results) == 1


def test_list_query_case_insensitive(registry: AgentRegistry):
    registry.add(HubAgent(name="Billing-Agent", description="d", source="apx"))
    results = registry.list(query="billing")
    assert len(results) == 1


def test_remove(registry: AgentRegistry, sample_agent: HubAgent):
    registry.add(sample_agent)
    registry.remove(sample_agent.id)
    assert registry.get(sample_agent.id) is None


def test_remove_missing_is_noop(registry: AgentRegistry):
    registry.remove("nonexistent")  # should not raise


def test_update_status(registry: AgentRegistry, sample_agent: HubAgent):
    registry.add(sample_agent)
    registry.update_status(sample_agent.id, "offline")
    assert registry.get(sample_agent.id).status == "offline"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hub && uv run pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.registry'`

- [ ] **Step 3: Implement registry**

```python
# hub/src/hub/registry.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hub && uv run pytest tests/test_registry.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add hub/src/hub/registry.py hub/tests/test_registry.py
git commit -m "feat(hub): in-memory agent registry with filtering"
```

---

## Task 3: Workspace discovery

**Files:**
- Create: `hub/src/hub/discovery.py`
- Create: `hub/tests/test_discovery.py`

- [ ] **Step 1: Write failing tests for discovery**

```python
# hub/tests/test_discovery.py
import pytest
from unittest.mock import MagicMock, AsyncMock
from hub.discovery import discover_serving_endpoints, discover_genie_spaces
from hub.models import HubAgent


def _make_endpoint(name: str, task: str = "llm/v1/chat", state: str = "READY", tags: dict | None = None):
    """Create a mock serving endpoint."""
    ep = MagicMock()
    ep.name = name
    ep.task = task
    ep.state = MagicMock()
    ep.state.ready = state
    ep.tags = tags or {}
    return ep


def test_discover_serving_endpoints_chat_task():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = [
        _make_endpoint("my-agent", task="llm/v1/chat"),
    ]
    agents = discover_serving_endpoints(ws)
    assert len(agents) == 1
    assert agents[0].name == "my-agent"
    assert agents[0].source == "serving_endpoint"
    assert agents[0].status == "online"


def test_discover_serving_endpoints_skips_non_agent():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = [
        _make_endpoint("embeddings-model", task="llm/v1/embeddings"),
    ]
    agents = discover_serving_endpoints(ws)
    assert len(agents) == 0


def test_discover_serving_endpoints_includes_tagged():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = [
        _make_endpoint("custom-agent", task="custom", tags={"agent": "true"}),
    ]
    agents = discover_serving_endpoints(ws)
    assert len(agents) == 1


def test_discover_serving_endpoints_not_ready():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = [
        _make_endpoint("my-agent", task="llm/v1/chat", state="NOT_READY"),
    ]
    agents = discover_serving_endpoints(ws)
    assert len(agents) == 1
    assert agents[0].status == "offline"


def _make_genie_response(spaces: list[dict]):
    return {"spaces": spaces}


def test_discover_genie_spaces():
    ws = MagicMock()
    ws.api_client.do.return_value = _make_genie_response([
        {"space_id": "abc123", "title": "Sales Analytics", "description": "Sales metrics"},
    ])
    agents = discover_genie_spaces(ws)
    assert len(agents) == 1
    assert agents[0].name == "Sales Analytics"
    assert agents[0].source == "genie_space"
    assert agents[0].metadata["space_id"] == "abc123"
    assert agents[0].status == "online"


def test_discover_genie_spaces_empty():
    ws = MagicMock()
    ws.api_client.do.return_value = {"spaces": []}
    agents = discover_genie_spaces(ws)
    assert len(agents) == 0


def test_discover_genie_spaces_api_error():
    ws = MagicMock()
    ws.api_client.do.side_effect = Exception("API error")
    agents = discover_genie_spaces(ws)
    assert len(agents) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hub && uv run pytest tests/test_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.discovery'`

- [ ] **Step 3: Implement discovery**

```python
# hub/src/hub/discovery.py
"""Discover agents from workspace serving endpoints and Genie spaces."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .models import HubAgent, HubSkill

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


def discover_serving_endpoints(ws: WorkspaceClient) -> list[HubAgent]:
    """List serving endpoints that look like agents (chat task or agent tag)."""
    agents: list[HubAgent] = []
    try:
        endpoints = ws.serving_endpoints.list()
    except Exception as e:
        logger.warning("Failed to list serving endpoints: %s", e)
        return agents

    for ep in endpoints:
        task = getattr(ep, "task", None) or ""
        tags = getattr(ep, "tags", None) or {}
        is_chat = task == "llm/v1/chat"
        is_tagged = tags.get("agent") is not None

        if not is_chat and not is_tagged:
            continue

        state = getattr(getattr(ep, "state", None), "ready", "NOT_READY")
        agents.append(HubAgent(
            name=ep.name,
            description=f"Serving endpoint: {ep.name}",
            source="serving_endpoint",
            url=ep.name,  # endpoint name, not a URL — SDK routes by name
            status="online" if state == "READY" else "offline",
            metadata={"task": task, "tags": tags},
        ))

    logger.info("Discovered %d serving endpoints", len(agents))
    return agents


def discover_genie_spaces(ws: WorkspaceClient) -> list[HubAgent]:
    """List Genie spaces from the workspace."""
    agents: list[HubAgent] = []
    try:
        response = ws.api_client.do("GET", "/api/2.0/genie/spaces")
    except Exception as e:
        logger.warning("Failed to list Genie spaces: %s", e)
        return agents

    spaces = response.get("spaces", []) if isinstance(response, dict) else []
    for space in spaces:
        space_id = space.get("space_id", "")
        title = space.get("title", space_id)
        description = space.get("description", f"Genie space: {title}")

        agents.append(HubAgent(
            name=title,
            description=description,
            source="genie_space",
            status="online",
            metadata={"space_id": space_id},
        ))

    logger.info("Discovered %d Genie spaces", len(agents))
    return agents
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hub && uv run pytest tests/test_discovery.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add hub/src/hub/discovery.py hub/tests/test_discovery.py
git commit -m "feat(hub): workspace serving endpoint and Genie space discovery"
```

---

## Task 4: Chat proxy

**Files:**
- Create: `hub/src/hub/chat.py`
- Create: `hub/tests/test_chat.py`

- [ ] **Step 1: Write failing tests for chat proxy**

```python
# hub/tests/test_chat.py
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from hub.chat import proxy_chat
from hub.models import HubAgent, ChatRequest, ChatResponse


@pytest.fixture
def apx_agent() -> HubAgent:
    return HubAgent(
        name="billing",
        description="Billing agent",
        source="apx",
        url="https://billing.example.com",
        status="online",
    )


@pytest.fixture
def serving_agent() -> HubAgent:
    return HubAgent(
        name="my-model",
        description="A model",
        source="serving_endpoint",
        url="my-model",
        status="online",
    )


@pytest.fixture
def genie_agent() -> HubAgent:
    return HubAgent(
        name="Sales Genie",
        description="Sales analytics",
        source="genie_space",
        status="online",
        metadata={"space_id": "abc123"},
    )


@pytest.mark.asyncio
async def test_proxy_chat_apx_agent(apx_agent: HubAgent):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "output_text": "Your bill is $42.",
    }

    with patch("hub.chat.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        request = ChatRequest(agent_id=apx_agent.id, message="What's my bill?")
        result = await proxy_chat(request, apx_agent, headers={})

        assert isinstance(result, ChatResponse)
        assert result.message == "Your bill is $42."
        assert result.conversation_id is not None

        mock_client.post.assert_called_once()
        call_url = mock_client.post.call_args[0][0]
        assert call_url == "https://billing.example.com/responses"


@pytest.mark.asyncio
async def test_proxy_chat_serving_endpoint(serving_agent: HubAgent):
    ws = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Model response"
    ws.serving_endpoints.query.return_value = mock_response

    request = ChatRequest(agent_id=serving_agent.id, message="Hello")
    result = await proxy_chat(request, serving_agent, headers={}, ws=ws)

    assert result.message == "Model response"
    ws.serving_endpoints.query.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_chat_genie_space(genie_agent: HubAgent):
    ws = MagicMock()
    # First call: create conversation
    ws.api_client.do.side_effect = [
        {"conversation_id": "conv-new", "message_id": "msg-1"},
        {"attachments": [{"text": {"content": "Sales are up 10%"}}]},
    ]

    request = ChatRequest(agent_id=genie_agent.id, message="How are sales?")
    result = await proxy_chat(request, genie_agent, headers={}, ws=ws)

    assert result.message == "Sales are up 10%"
    assert result.conversation_id is not None


@pytest.mark.asyncio
async def test_proxy_chat_agent_no_url():
    agent = HubAgent(name="broken", description="d", source="apx", url=None)
    request = ChatRequest(agent_id=agent.id, message="Hello")

    with pytest.raises(ValueError, match="no URL configured"):
        await proxy_chat(request, agent, headers={})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hub && uv run pytest tests/test_chat.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.chat'`

- [ ] **Step 3: Implement chat proxy**

```python
# hub/src/hub/chat.py
"""Proxy chat messages to agents based on their source type."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from .models import ChatRequest, ChatResponse, HubAgent

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


async def proxy_chat(
    request: ChatRequest,
    agent: HubAgent,
    headers: dict[str, str],
    ws: "WorkspaceClient | None" = None,
) -> ChatResponse:
    """Route a chat message to the appropriate backend."""
    conversation_id = request.conversation_id or str(uuid.uuid4())

    if agent.source == "apx":
        return await _chat_apx(request, agent, headers, conversation_id)
    elif agent.source == "serving_endpoint":
        if ws is None:
            raise ValueError("WorkspaceClient required for serving endpoint chat")
        return await _chat_serving_endpoint(request, agent, ws, conversation_id)
    elif agent.source == "genie_space":
        if ws is None:
            raise ValueError("WorkspaceClient required for Genie space chat")
        return await _chat_genie_space(request, agent, ws, conversation_id)
    else:
        raise ValueError(f"Unknown source: {agent.source}")


async def _chat_apx(
    request: ChatRequest,
    agent: HubAgent,
    headers: dict[str, str],
    conversation_id: str,
) -> ChatResponse:
    """Send message to an apx-agent app via /responses."""
    if not agent.url:
        raise ValueError(f"Agent '{agent.name}' has no URL configured")

    url = f"{agent.url.rstrip('/')}/responses"
    payload: dict[str, Any] = {
        "input": [{"role": "user", "content": request.message}],
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    # Extract text from Responses API format
    text = data.get("output_text", "")
    if not text:
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text", "")
                    break
            if text:
                break

    return ChatResponse(agent_id=request.agent_id, message=text, conversation_id=conversation_id)


async def _chat_serving_endpoint(
    request: ChatRequest,
    agent: HubAgent,
    ws: "WorkspaceClient",
    conversation_id: str,
) -> ChatResponse:
    """Send message to a serving endpoint via Databricks SDK."""
    response = ws.serving_endpoints.query(
        name=agent.url,
        messages=[{"role": "user", "content": request.message}],
    )
    text = response.choices[0].message.content if response.choices else ""
    return ChatResponse(agent_id=request.agent_id, message=text, conversation_id=conversation_id)


async def _chat_genie_space(
    request: ChatRequest,
    agent: HubAgent,
    ws: "WorkspaceClient",
    conversation_id: str,
) -> ChatResponse:
    """Send message to a Genie space via conversation API."""
    space_id = agent.metadata.get("space_id", "")

    # Start a new conversation with the message
    conv_resp = ws.api_client.do(
        "POST",
        f"/api/2.0/genie/spaces/{space_id}/conversations",
        body={"content": request.message},
    )
    genie_conv_id = conv_resp.get("conversation_id", "")
    message_id = conv_resp.get("message_id", "")

    # Fetch the response
    msg_resp = ws.api_client.do(
        "GET",
        f"/api/2.0/genie/spaces/{space_id}/conversations/{genie_conv_id}/messages/{message_id}",
    )
    attachments = msg_resp.get("attachments", [])
    text = ""
    for att in attachments:
        text_block = att.get("text", {})
        if text_block.get("content"):
            text = text_block["content"]
            break

    return ChatResponse(
        agent_id=request.agent_id,
        message=text,
        conversation_id=f"{space_id}:{genie_conv_id}",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hub && uv run pytest tests/test_chat.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add hub/src/hub/chat.py hub/tests/test_chat.py
git commit -m "feat(hub): chat proxy for apx agents, serving endpoints, and Genie spaces"
```

---

## Task 5: Hub app (API routes + background tasks)

**Files:**
- Create: `hub/src/hub/app.py`
- Create: `hub/tests/test_app.py`

- [ ] **Step 1: Write failing tests for app endpoints**

```python
# hub/tests/test_app.py
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_ws():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = []
    ws.api_client.do.return_value = {"spaces": []}
    return ws


@pytest.fixture
def app(mock_ws):
    with patch("hub.app._get_workspace_client", return_value=mock_ws):
        from hub.app import create_hub_app
        return create_hub_app()


@pytest.fixture
def client(app):
    return TestClient(app)


def test_list_agents_empty(client):
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == []


def test_register_and_list(client):
    # Mock the card fetch
    card_data = {
        "name": "test-agent",
        "description": "A test agent",
        "skills": [{"id": "s1", "name": "skill1", "description": "does stuff"}],
    }
    with patch("hub.app.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = card_data
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = client.post("/api/agents/register", json={"url": "https://test-agent.example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data

    agents = client.get("/api/agents").json()
    assert len(agents) == 1
    assert agents[0]["name"] == "test-agent"
    assert agents[0]["source"] == "apx"


def test_get_agent_not_found(client):
    resp = client.get("/api/agents/nonexistent")
    assert resp.status_code == 404


def test_list_agents_filter_source(client):
    # Pre-populate registry via the app's registry
    from hub.models import HubAgent
    app_instance = client.app
    registry = app_instance.state.hub_registry
    registry.add(HubAgent(name="a1", description="d", source="apx"))
    registry.add(HubAgent(name="a2", description="d", source="genie_space"))

    resp = client.get("/api/agents?source=apx")
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["source"] == "apx"


def test_list_agents_filter_query(client):
    from hub.models import HubAgent
    registry = client.app.state.hub_registry
    registry.add(HubAgent(name="billing-agent", description="Handles billing", source="apx"))
    registry.add(HubAgent(name="data-triage", description="Investigates data", source="apx"))

    resp = client.get("/api/agents?q=billing")
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["name"] == "billing-agent"


def test_frontend_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hub && uv run pytest tests/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.app'`

- [ ] **Step 3: Create minimal frontend placeholder**

Create `hub/frontend/index.html` so static file serving works:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Hub</title>
</head>
<body>
    <div id="app">Loading...</div>
</body>
</html>
```

- [ ] **Step 4: Implement hub app**

```python
# hub/src/hub/app.py
"""Agent Hub — FastAPI app with registry, discovery, and chat proxy."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from .chat import proxy_chat
from .discovery import discover_serving_endpoints, discover_genie_spaces
from .models import ChatRequest, ChatResponse, HubAgent, HubSkill, RegisterRequest, RegisterResponse
from .registry import AgentRegistry

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


def _get_workspace_client():
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient()


def create_hub_app() -> FastAPI:
    """Create the Agent Hub FastAPI application."""
    registry = AgentRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.hub_registry = registry
        app.state.workspace_client = _get_workspace_client()

        # Initial workspace discovery
        _refresh_registry(registry, app.state.workspace_client)

        # Start background refresh task
        task = asyncio.create_task(_background_refresh(registry, app.state.workspace_client))
        yield
        task.cancel()

    app = FastAPI(title="Agent Hub", lifespan=lifespan)

    # --- API routes ---

    @app.post("/api/agents/register")
    async def register_agent(body: RegisterRequest, request: Request) -> RegisterResponse:
        url = body.url.rstrip("/")
        card_url = f"{url}/.well-known/agent.json"

        # Forward auth headers so we can reach agents behind OBO
        fwd_headers = {}
        if auth := request.headers.get("Authorization"):
            fwd_headers["Authorization"] = auth
        if token := request.headers.get("X-Forwarded-Access-Token"):
            fwd_headers["X-Forwarded-Access-Token"] = token

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(card_url, headers=fwd_headers)
                resp.raise_for_status()
                card = resp.json()
        except Exception as e:
            logger.warning("Failed to fetch agent card from %s: %s", card_url, e)
            # Register with minimal info
            card = {"name": url.split("/")[-1], "description": url}

        skills = [
            HubSkill(name=s.get("name", ""), description=s.get("description", ""))
            for s in card.get("skills", [])
        ]
        agent = HubAgent(
            name=card.get("name", url),
            description=card.get("description", ""),
            source="apx",
            url=url,
            skills=skills,
            status="online",
        )
        registry.add(agent)
        logger.info("Registered agent: %s (%s)", agent.name, agent.id)
        return RegisterResponse(id=agent.id)

    @app.get("/api/agents")
    async def list_agents(
        source: str | None = Query(None),
        q: str | None = Query(None),
    ) -> list[dict[str, Any]]:
        agents = registry.list(source=source, query=q)
        return [a.model_dump() for a in agents]

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str) -> dict[str, Any]:
        agent = registry.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return agent.model_dump()

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request) -> ChatResponse:
        agent = registry.get(body.agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        # Forward OBO headers
        fwd_headers = {}
        if auth := request.headers.get("Authorization"):
            fwd_headers["Authorization"] = auth
        if token := request.headers.get("X-Forwarded-Access-Token"):
            fwd_headers["X-Forwarded-Access-Token"] = token

        ws = getattr(request.app.state, "workspace_client", None)
        return await proxy_chat(body, agent, headers=fwd_headers, ws=ws)

    # --- Static frontend ---

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app


def _refresh_registry(registry: AgentRegistry, ws: Any) -> None:
    """Synchronously refresh workspace-discovered agents."""
    for agent in discover_serving_endpoints(ws):
        registry.add(agent)
    for agent in discover_genie_spaces(ws):
        registry.add(agent)


async def _background_refresh(registry: AgentRegistry, ws: Any) -> None:
    """Periodically refresh workspace agents and health-check apx agents."""
    health_counter = 0
    while True:
        await asyncio.sleep(60)
        health_counter += 1

        # Health check apx agents every 60s
        apx_agents = registry.list(source="apx")
        for agent in apx_agents:
            if agent.url:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(f"{agent.url.rstrip('/')}/health")
                        if resp.status_code == 200:
                            registry.update_status(agent.id, "online")
                        else:
                            registry.update_status(agent.id, "offline")
                except Exception:
                    registry.update_status(agent.id, "offline")

        # Workspace refresh every 5 minutes (every 5th health check cycle)
        if health_counter % 5 == 0:
            _refresh_registry(registry, ws)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd hub && uv run pytest tests/test_app.py -v`
Expected: All 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add hub/src/hub/app.py hub/tests/test_app.py hub/frontend/index.html
git commit -m "feat(hub): API routes, background discovery, and static file serving"
```

---

## Task 6: Frontend — catalog view

**Files:**
- Create: `hub/frontend/style.css`
- Modify: `hub/frontend/index.html`
- Create: `hub/frontend/app.js`

- [ ] **Step 1: Build the HTML shell**

```html
<!-- hub/frontend/index.html -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Hub</title>
    <link rel="stylesheet" href="/style.css">
</head>
<body>
    <header>
        <h1>Agent Hub</h1>
        <div class="controls">
            <input type="text" id="search" placeholder="Search agents..." autocomplete="off">
            <div class="filters">
                <button class="filter-btn active" data-source="all">All</button>
                <button class="filter-btn" data-source="apx">Apps</button>
                <button class="filter-btn" data-source="serving_endpoint">Endpoints</button>
                <button class="filter-btn" data-source="genie_space">Genie</button>
            </div>
        </div>
    </header>

    <main>
        <!-- Catalog view -->
        <div id="catalog" class="view active">
            <div id="agent-grid" class="agent-grid"></div>
        </div>

        <!-- Chat view -->
        <div id="chat-view" class="view">
            <aside id="agent-sidebar" class="sidebar"></aside>
            <div class="chat-main">
                <div id="chat-header" class="chat-header"></div>
                <div id="chat-messages" class="chat-messages"></div>
                <form id="chat-form" class="chat-form">
                    <input type="text" id="chat-input" placeholder="Type a message..." autocomplete="off">
                    <button type="submit">Send</button>
                </form>
            </div>
        </div>
    </main>

    <script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Write the CSS**

```css
/* hub/frontend/style.css */
* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f5f5f5;
    color: #1a1a1a;
}

header {
    background: #fff;
    border-bottom: 1px solid #e0e0e0;
    padding: 16px 24px;
    position: sticky;
    top: 0;
    z-index: 10;
}

header h1 { font-size: 20px; margin-bottom: 12px; }

.controls { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }

#search {
    padding: 8px 12px;
    border: 1px solid #d0d0d0;
    border-radius: 6px;
    font-size: 14px;
    width: 260px;
}

.filters { display: flex; gap: 4px; }

.filter-btn {
    padding: 6px 12px;
    border: 1px solid #d0d0d0;
    border-radius: 6px;
    background: #fff;
    cursor: pointer;
    font-size: 13px;
}
.filter-btn.active { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }

main { padding: 24px; }

.view { display: none; }
.view.active { display: block; }
#chat-view.active { display: flex; height: calc(100vh - 120px); }

/* Catalog grid */
.agent-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 16px;
}

.agent-card {
    background: #fff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 16px;
    cursor: pointer;
    transition: box-shadow 0.15s;
}
.agent-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.1); }

.agent-card .name { font-weight: 600; font-size: 15px; margin-bottom: 4px; }
.agent-card .description { font-size: 13px; color: #666; margin-bottom: 8px; }

.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
}
.badge.apx { background: #e3f2fd; color: #1565c0; }
.badge.serving_endpoint { background: #f3e5f5; color: #7b1fa2; }
.badge.genie_space { background: #e8f5e9; color: #2e7d32; }

.status-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
}
.status-dot.online { background: #4caf50; }
.status-dot.offline { background: #f44336; }
.status-dot.unknown { background: #bdbdbd; }

/* Chat view */
.sidebar {
    width: 260px;
    border-right: 1px solid #e0e0e0;
    background: #fff;
    overflow-y: auto;
    flex-shrink: 0;
}

.sidebar-item {
    padding: 12px 16px;
    cursor: pointer;
    border-bottom: 1px solid #f0f0f0;
    font-size: 13px;
}
.sidebar-item:hover { background: #f5f5f5; }
.sidebar-item.active { background: #e3f2fd; font-weight: 600; }

.chat-main { flex: 1; display: flex; flex-direction: column; }

.chat-header {
    padding: 12px 16px;
    border-bottom: 1px solid #e0e0e0;
    background: #fff;
    font-weight: 600;
}

.chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    background: #fafafa;
}

.message {
    margin-bottom: 12px;
    max-width: 70%;
}
.message.user { margin-left: auto; text-align: right; }
.message .bubble {
    display: inline-block;
    padding: 8px 14px;
    border-radius: 12px;
    font-size: 14px;
    line-height: 1.4;
}
.message.user .bubble { background: #1a1a1a; color: #fff; }
.message.assistant .bubble { background: #fff; border: 1px solid #e0e0e0; }

.divider {
    text-align: center;
    color: #999;
    font-size: 12px;
    margin: 16px 0;
    position: relative;
}

.chat-form {
    display: flex;
    gap: 8px;
    padding: 12px 16px;
    border-top: 1px solid #e0e0e0;
    background: #fff;
}
.chat-form input {
    flex: 1;
    padding: 10px 14px;
    border: 1px solid #d0d0d0;
    border-radius: 8px;
    font-size: 14px;
}
.chat-form button {
    padding: 10px 20px;
    background: #1a1a1a;
    color: #fff;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    font-size: 14px;
}
.chat-form button:disabled { opacity: 0.5; cursor: not-allowed; }

.loading { opacity: 0.6; font-style: italic; }
```

- [ ] **Step 3: Write the JavaScript**

```javascript
// hub/frontend/app.js

const state = {
    agents: [],
    activeFilter: 'all',
    searchQuery: '',
    currentAgent: null,
    messages: [],          // { role, content, agentId, agentName }
    conversationIds: {},   // agentId -> conversationId
    sending: false,
};

// --- API ---

async function fetchAgents() {
    const params = new URLSearchParams();
    if (state.activeFilter !== 'all') params.set('source', state.activeFilter);
    if (state.searchQuery) params.set('q', state.searchQuery);
    const resp = await fetch(`/api/agents?${params}`);
    state.agents = await resp.json();
    renderCatalog();
    renderSidebar();
}

async function sendMessage(message) {
    if (!state.currentAgent || state.sending) return;
    state.sending = true;
    const agent = state.currentAgent;

    state.messages.push({ role: 'user', content: message, agentId: agent.id, agentName: agent.name });
    renderMessages();

    try {
        const resp = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                agent_id: agent.id,
                message: message,
                conversation_id: state.conversationIds[agent.id] || null,
            }),
        });
        const data = await resp.json();
        state.conversationIds[agent.id] = data.conversation_id;
        state.messages.push({ role: 'assistant', content: data.message, agentId: agent.id, agentName: agent.name });
    } catch (err) {
        state.messages.push({ role: 'assistant', content: `Error: ${err.message}`, agentId: agent.id, agentName: agent.name });
    }

    state.sending = false;
    renderMessages();
}

// --- Rendering ---

function renderCatalog() {
    const grid = document.getElementById('agent-grid');
    grid.innerHTML = state.agents.map(a => `
        <div class="agent-card" data-id="${a.id}">
            <div class="name">
                <span class="status-dot ${a.status}"></span>
                ${esc(a.name)}
            </div>
            <div class="description">${esc(a.description)}</div>
            <span class="badge ${a.source}">${badgeLabel(a.source)}</span>
        </div>
    `).join('');

    grid.querySelectorAll('.agent-card').forEach(card => {
        card.addEventListener('click', () => openChat(card.dataset.id));
    });
}

function renderSidebar() {
    const sidebar = document.getElementById('agent-sidebar');
    sidebar.innerHTML = state.agents.map(a => `
        <div class="sidebar-item ${state.currentAgent?.id === a.id ? 'active' : ''}" data-id="${a.id}">
            <span class="status-dot ${a.status}"></span>
            ${esc(a.name)}
        </div>
    `).join('');

    sidebar.querySelectorAll('.sidebar-item').forEach(item => {
        item.addEventListener('click', () => switchAgent(item.dataset.id));
    });
}

function renderMessages() {
    const container = document.getElementById('chat-messages');
    let html = '';
    let lastAgentId = null;

    for (const msg of state.messages) {
        if (msg.agentId !== lastAgentId && lastAgentId !== null) {
            html += `<div class="divider">Switched to ${esc(msg.agentName)}</div>`;
        }
        lastAgentId = msg.agentId;
        html += `
            <div class="message ${msg.role}">
                <div class="bubble">${esc(msg.content)}</div>
            </div>
        `;
    }

    if (state.sending) {
        html += `<div class="message assistant"><div class="bubble loading">Thinking...</div></div>`;
    }

    container.innerHTML = html;
    container.scrollTop = container.scrollHeight;
}

// --- Navigation ---

function openChat(agentId) {
    const agent = state.agents.find(a => a.id === agentId);
    if (!agent) return;

    state.currentAgent = agent;

    document.getElementById('catalog').classList.remove('active');
    document.getElementById('chat-view').classList.add('active');
    document.getElementById('chat-header').textContent = agent.name;
    document.getElementById('chat-input').focus();

    renderSidebar();
    renderMessages();
}

function switchAgent(agentId) {
    const agent = state.agents.find(a => a.id === agentId);
    if (!agent || agent.id === state.currentAgent?.id) return;

    state.currentAgent = agent;
    document.getElementById('chat-header').textContent = agent.name;

    renderSidebar();
    renderMessages();
}

function showCatalog() {
    document.getElementById('chat-view').classList.remove('active');
    document.getElementById('catalog').classList.add('active');
}

// --- Utilities ---

function badgeLabel(source) {
    return { apx: 'App', serving_endpoint: 'Endpoint', genie_space: 'Genie' }[source] || source;
}

function esc(str) {
    const d = document.createElement('div');
    d.textContent = str || '';
    return d.innerHTML;
}

// --- Event Listeners ---

document.getElementById('search').addEventListener('input', (e) => {
    state.searchQuery = e.target.value;
    fetchAgents();
});

document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.activeFilter = btn.dataset.source;
        fetchAgents();
    });
});

document.getElementById('chat-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    sendMessage(message);
});

document.querySelector('header h1').addEventListener('click', showCatalog);
document.querySelector('header h1').style.cursor = 'pointer';

// --- Init ---

fetchAgents();
// Refresh every 30 seconds
setInterval(fetchAgents, 30000);
```

- [ ] **Step 4: Test manually by running the app**

Run: `cd hub && uv run pytest tests/ -v`
Expected: All tests pass (models, registry, discovery, chat, app)

- [ ] **Step 5: Commit**

```bash
git add hub/frontend/
git commit -m "feat(hub): frontend catalog and chat UI"
```

---

## Task 7: Deployment config and README

**Files:**
- Create: `hub/app.yml`
- Create: `hub/databricks.yml`

- [ ] **Step 1: Create app.yml**

```yaml
# hub/app.yml
command:
  - uvicorn
  - hub.app:create_hub_app
  - --factory
  - --host=0.0.0.0
  - --port=8000
env:
  - name: PYTHONPATH
    value: src
```

- [ ] **Step 2: Create databricks.yml**

```yaml
# hub/databricks.yml
bundle:
  name: agent-hub

resources:
  apps:
    agent-hub:
      name: "agent-hub"
      description: "Unified catalog and chat for all workspace agents"
      source_code_path: .

targets:
  dev:
    default: true
```

- [ ] **Step 3: Update top-level README**

Add the hub to the project structure section in `README.md`:

Replace:
```
python/          Python package — pyproject.toml, src/, tests/, examples/
typescript/      TypeScript package — package.json, src/, tests/, examples/
docs/            Design specs and implementation plans
```

With:
```
python/          Python package — pyproject.toml, src/, tests/, examples/
typescript/      TypeScript package — package.json, src/, tests/, examples/
hub/             Agent Hub — catalog and chat dashboard (Databricks App)
docs/            Design specs and implementation plans
```

- [ ] **Step 4: Update CI to include hub tests**

Add a new job to `.github/workflows/ci.yml`:

```yaml
  test-hub:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: hub

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v4

      - name: Set up Python
        run: uv python install 3.11

      - name: Install dependencies
        run: uv sync --group dev

      - name: Run tests
        run: uv run pytest tests/ -v --tb=short
```

- [ ] **Step 5: Run all tests one final time**

Run: `cd hub && uv run pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add hub/app.yml hub/databricks.yml README.md .github/workflows/ci.yml
git commit -m "feat(hub): deployment config, CI, and README update"
```

---

## Self-Review Checklist

**Spec coverage:**
- Registry (POST /api/agents/register) → Task 5
- Catalog UI (searchable card grid) → Task 6
- Chat UI (proxy + agent switching) → Tasks 4, 5, 6
- Unified agent model → Task 1
- Discovery: serving endpoints → Task 3
- Discovery: Genie spaces → Task 3
- Background refresh + health checks → Task 5
- Frontend: catalog view → Task 6
- Frontend: chat view → Task 6
- Frontend: agent switching → Task 6
- Deployment config → Task 7
- No persistent storage, no auth, no orchestration → confirmed by design

**Placeholder scan:** No TBDs, TODOs, or "implement later" found.

**Type consistency:**
- `HubAgent`, `HubSkill` — used consistently in models, registry, discovery, chat, app
- `ChatRequest`, `ChatResponse` — used consistently in chat.py and app.py
- `RegisterRequest`, `RegisterResponse` — used consistently in models and app
- `AgentRegistry` methods: `add()`, `get()`, `remove()`, `update_status()`, `list()` — consistent across registry.py and app.py
- `proxy_chat()` signature matches usage in app.py
- `discover_serving_endpoints()`, `discover_genie_spaces()` — consistent between discovery.py and app.py
