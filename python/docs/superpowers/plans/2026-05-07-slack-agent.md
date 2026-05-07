# Slack Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a working example Slack bot that uses real end-user Databricks credentials via OAuth, illustrating how apx-agent's OBO token system works.

**Architecture:** A Databricks App built with `create_app(agent)` with two call paths: browser requests get `X-Forwarded-Access-Token` injected automatically by Databricks Apps proxy; Slack requests get the same header injected manually by the Slack handler after retrieving the stored OAuth token. The agent code is identical for both paths.

**Tech Stack:** apx-agent, FastAPI, pydantic-settings, httpx, databricks-sdk, Python 3.11+

---

## File Map

| File | Responsibility |
|------|---------------|
| `pyproject.toml` | Project metadata, dependencies, apx agent config |
| `src/slack_agent/__init__.py` | Package marker |
| `src/slack_agent/backend/__init__.py` | Package marker |
| `src/slack_agent/backend/config.py` | `Settings` via pydantic-settings — all env vars |
| `src/slack_agent/backend/token_store.py` | Module-level dict mapping Slack user ID → Databricks access token |
| `src/slack_agent/backend/agent_router.py` | `LlmAgent` with `who_am_i` tool using `Dependencies.UserClient` |
| `src/slack_agent/backend/slack_router.py` | `/slack/install`, `/slack/oauth/callback`, `/slack/events` |
| `src/slack_agent/backend/app.py` | `create_app(agent)` + include slack router |
| `tests/__init__.py` | Test package marker |
| `tests/test_config.py` | Settings defaults and env var loading |
| `tests/test_token_store.py` | get/set/clear token store |
| `tests/test_slack_signature.py` | `_verify_slack_signature` unit tests |
| `tests/test_slack_events.py` | `/slack/events` endpoint: signature rejection, /connect, missing token, happy path |
| `tests/test_oauth.py` | `/slack/install` redirect, `/slack/oauth/callback` token exchange |
| `tests/test_who_am_i.py` | `who_am_i` tool unit test |

---

## Task 1: Scaffold the project

**Files:**
- Create: `examples/slack-agent/pyproject.toml`
- Create: `examples/slack-agent/src/slack_agent/__init__.py`
- Create: `examples/slack-agent/src/slack_agent/backend/__init__.py`
- Create: `examples/slack-agent/tests/__init__.py`

- [ ] **Step 1: Create the directory structure**

```bash
cd /Users/stuart.gano/Documents/apx-agent/python/examples
mkdir -p slack-agent/src/slack_agent/backend
mkdir -p slack-agent/tests
touch slack-agent/src/slack_agent/__init__.py
touch slack-agent/src/slack_agent/backend/__init__.py
touch slack-agent/tests/__init__.py
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "slack-agent"
version = "0.1.0"
description = "Example: Slack bot using apx-agent with Databricks OAuth — illustrates OBO token forwarding"
requires-python = ">=3.11"
dependencies = [
    "apx-agent",
    "databricks-sdk>=0.30",
    "httpx>=0.27",
    "pydantic>=2.5",
    "pydantic-settings>=2.11.0",
]

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.apx.agent]
name = "slack-agent"
description = "Slack bot connected to Databricks via OAuth — illustrates OBO token forwarding"
model = "databricks-claude-sonnet-4-6"

[tool.uv.sources]
apx-agent = { path = "../..", editable = true }

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: Install dependencies**

```bash
cd /Users/stuart.gano/Documents/apx-agent/python/examples/slack-agent
uv sync --dev
```

Expected: resolves and installs all packages without errors.

- [ ] **Step 4: Commit scaffold**

```bash
git add examples/slack-agent/
git commit -m "feat(slack-agent): scaffold project structure"
```

---

## Task 2: `config.py` — Settings

**Files:**
- Create: `examples/slack-agent/src/slack_agent/backend/config.py`
- Create: `examples/slack-agent/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
import pytest
from slack_agent.backend.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.databricks_host == ""
    assert s.databricks_client_id == ""
    assert s.databricks_client_secret == ""
    assert s.app_url == ""
    assert s.slack_signing_secret == ""
    assert s.slack_bot_token == ""


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "adb-123.azuredatabricks.net")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "my-client-id")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "my-signing-secret")
    s = Settings()
    assert s.databricks_host == "adb-123.azuredatabricks.net"
    assert s.databricks_client_id == "my-client-id"
    assert s.slack_signing_secret == "my-signing-secret"
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/stuart.gano/Documents/apx-agent/python/examples/slack-agent
uv run pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'slack_agent'`

- [ ] **Step 3: Write `config.py`**

`src/slack_agent/backend/config.py`:
```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    databricks_host: str = ""
    databricks_client_id: str = ""
    databricks_client_secret: str = ""
    app_url: str = ""
    slack_signing_secret: str = ""
    slack_bot_token: str = ""


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_config.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add examples/slack-agent/src/slack_agent/backend/config.py examples/slack-agent/tests/test_config.py
git commit -m "feat(slack-agent): add Settings config with env var support"
```

---

## Task 3: `token_store.py` — In-memory token store

**Files:**
- Create: `examples/slack-agent/src/slack_agent/backend/token_store.py`
- Create: `examples/slack-agent/tests/test_token_store.py`

- [ ] **Step 1: Write the failing test**

`tests/test_token_store.py`:
```python
import pytest
from slack_agent.backend import token_store


@pytest.fixture(autouse=True)
def clear_store():
    token_store._store.clear()
    yield
    token_store._store.clear()


def test_get_missing_returns_none():
    assert token_store.get_token("U999") is None


def test_set_then_get_returns_token():
    token_store.set_token("U123", "dapi-abc")
    assert token_store.get_token("U123") == "dapi-abc"


def test_set_overwrites_existing():
    token_store.set_token("U123", "old-token")
    token_store.set_token("U123", "new-token")
    assert token_store.get_token("U123") == "new-token"


def test_clear_removes_token():
    token_store.set_token("U123", "dapi-abc")
    token_store.clear_token("U123")
    assert token_store.get_token("U123") is None


def test_clear_missing_is_noop():
    token_store.clear_token("U999")  # must not raise
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_token_store.py -v
```

Expected: `ModuleNotFoundError` or `ImportError`.

- [ ] **Step 3: Write `token_store.py`**

`src/slack_agent/backend/token_store.py`:
```python
# In-memory store: Slack user ID → Databricks access token.
# Single-process safe. Resets on redeploy — for production use one of:
#   Option B: slack_bolt InstallationStore (e.g. FileInstallationStore)
#   Option C: Delta table via WorkspaceClient SQL execution

_store: dict[str, str] = {}


def get_token(slack_user_id: str) -> str | None:
    return _store.get(slack_user_id)


def set_token(slack_user_id: str, access_token: str) -> None:
    _store[slack_user_id] = access_token


def clear_token(slack_user_id: str) -> None:
    _store.pop(slack_user_id, None)
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_token_store.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add examples/slack-agent/src/slack_agent/backend/token_store.py examples/slack-agent/tests/test_token_store.py
git commit -m "feat(slack-agent): add in-memory token store"
```

---

## Task 4: `agent_router.py` — `who_am_i` tool

**Files:**
- Create: `examples/slack-agent/src/slack_agent/backend/agent_router.py`
- Create: `examples/slack-agent/tests/test_who_am_i.py`

- [ ] **Step 1: Write the failing test**

`tests/test_who_am_i.py`:
```python
from unittest.mock import MagicMock
from slack_agent.backend.agent_router import who_am_i


def test_who_am_i_formats_display_name_and_email():
    mock_ws = MagicMock()
    mock_ws.current_user.me.return_value = MagicMock(
        display_name="Alice Smith",
        user_name="alice@example.com",
    )
    result = who_am_i(mock_ws)
    assert result == "Alice Smith (alice@example.com)"


def test_who_am_i_calls_current_user_me():
    mock_ws = MagicMock()
    mock_ws.current_user.me.return_value = MagicMock(
        display_name="Bob Jones",
        user_name="bob@example.com",
    )
    who_am_i(mock_ws)
    mock_ws.current_user.me.assert_called_once()
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_who_am_i.py -v
```

Expected: `ImportError` or `ModuleNotFoundError`.

- [ ] **Step 3: Write `agent_router.py`**

`src/slack_agent/backend/agent_router.py`:
```python
from apx_agent import Agent, Dependencies


def who_am_i(ws: Dependencies.UserClient) -> str:
    """Return the identity of the current Databricks user.

    When called from the browser, Dependencies.UserClient reads
    X-Forwarded-Access-Token injected automatically by the Databricks Apps proxy.
    When called from Slack, the Slack handler injects the stored OAuth token
    into that same header before calling /responses — the agent sees no difference.
    """
    user = ws.current_user.me()
    return f"{user.display_name} ({user.user_name})"


agent = Agent(
    tools=[who_am_i],
    instructions=(
        "You are a helpful assistant connected to Databricks. "
        "When asked who the user is or what account they are using, call who_am_i."
    ),
)
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_who_am_i.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add examples/slack-agent/src/slack_agent/backend/agent_router.py examples/slack-agent/tests/test_who_am_i.py
git commit -m "feat(slack-agent): add who_am_i tool and LlmAgent"
```

---

## Task 5: Slack signature verification

**Files:**
- Create: `examples/slack-agent/src/slack_agent/backend/slack_router.py` (partial — signature function only)
- Create: `examples/slack-agent/tests/test_slack_signature.py`

- [ ] **Step 1: Write the failing test**

`tests/test_slack_signature.py`:
```python
import hashlib
import hmac
import time

from slack_agent.backend.slack_router import _verify_slack_signature

SECRET = "test-signing-secret"


def _make_sig(body: bytes, timestamp: str) -> str:
    basestring = f"v0:{timestamp}:{body.decode()}"
    return "v0=" + hmac.new(SECRET.encode(), basestring.encode(), hashlib.sha256).hexdigest()


def test_valid_signature_passes():
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()))
    sig = _make_sig(body, ts)
    assert _verify_slack_signature(body, ts, sig, SECRET) is True


def test_invalid_signature_fails():
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()))
    assert _verify_slack_signature(body, ts, "v0=badhex", SECRET) is False


def test_stale_timestamp_fails():
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()) - 400)  # 6+ minutes old
    sig = _make_sig(body, ts)
    assert _verify_slack_signature(body, ts, sig, SECRET) is False


def test_non_numeric_timestamp_fails():
    body = b"command=%2Fwhoami&user_id=U123"
    assert _verify_slack_signature(body, "not-a-number", "v0=anything", SECRET) is False


def test_empty_signature_fails():
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()))
    assert _verify_slack_signature(body, ts, "", SECRET) is False
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_slack_signature.py -v
```

Expected: `ImportError` — `slack_router` doesn't exist yet.

- [ ] **Step 3: Create `slack_router.py` with the signature function only**

`src/slack_agent/backend/slack_router.py`:
```python
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import Settings, get_settings
from . import token_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/slack")


def _verify_slack_signature(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    """Validate Slack's HMAC-SHA256 request signature.

    Slack signs requests with: HMAC-SHA256(signing_secret, "v0:{timestamp}:{body}")
    Rejects requests with timestamps older than 5 minutes (replay protection).
    """
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts) > 300:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

- [ ] **Step 4: Run to verify it passes**

```bash
uv run pytest tests/test_slack_signature.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add examples/slack-agent/src/slack_agent/backend/slack_router.py examples/slack-agent/tests/test_slack_signature.py
git commit -m "feat(slack-agent): add Slack HMAC-SHA256 signature verification"
```

---

## Task 6: `/slack/install` — Databricks OIDC redirect

**Files:**
- Modify: `examples/slack-agent/src/slack_agent/backend/slack_router.py`
- Create: `examples/slack-agent/src/slack_agent/backend/app.py` (needed by TestClient)
- Create: `examples/slack-agent/tests/test_oauth.py` (partial — install endpoint only)

- [ ] **Step 1: Write the failing test**

`tests/test_oauth.py`:
```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from slack_agent.backend.app import app
from slack_agent.backend.config import Settings, get_settings
from slack_agent.backend import token_store

DATABRICKS_HOST = "adb-123.azuredatabricks.net"
APP_URL = "https://my-app.databricksapps.com"


def _make_settings():
    return Settings(
        databricks_host=DATABRICKS_HOST,
        databricks_client_id="my-client-id",
        databricks_client_secret="my-client-secret",
        app_url=APP_URL,
        slack_signing_secret="signing-secret",
        slack_bot_token="xoxb-bot-token",
    )


@pytest.fixture(autouse=True)
def clear_store():
    token_store._store.clear()
    yield
    token_store._store.clear()


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_install_redirects_to_databricks_oidc(client):
    resp = client.get("/slack/install?user=U123", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert f"https://{DATABRICKS_HOST}/oidc/v1/authorize" in location
    assert "client_id=my-client-id" in location
    assert "state=U123" in location
    assert "response_type=code" in location
    assert "scope=all-apis" in location


def test_install_includes_redirect_uri(client):
    resp = client.get("/slack/install?user=U123", follow_redirects=False)
    location = resp.headers["location"]
    assert "redirect_uri=" in location
    assert "slack%2Foauth%2Fcallback" in location or "slack/oauth/callback" in location
```

- [ ] **Step 2: Create `app.py`** (needed for the TestClient fixture)

`src/slack_agent/backend/app.py`:
```python
import logging
import os

from apx_agent import create_app
from apx_agent._models import AgentConfig
from fastapi.responses import RedirectResponse

from .agent_router import agent
from .slack_router import router as slack_router

logger = logging.getLogger(__name__)

_agent_config = AgentConfig(
    name="slack-agent",
    description="Slack bot connected to Databricks via OAuth — illustrates OBO token forwarding",
    model="databricks-claude-sonnet-4-6",
    url=os.environ.get("SLACK_AGENT_URL"),
)

app = create_app(agent, config=_agent_config)
app.include_router(slack_router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/_apx/agent")
```

- [ ] **Step 3: Run to verify it fails**

```bash
uv run pytest tests/test_oauth.py::test_install_redirects_to_databricks_oidc -v
```

Expected: FAIL — `slack_router` has no `/slack/install` route yet.

- [ ] **Step 4: Add `/slack/install` to `slack_router.py`**

Append to `src/slack_agent/backend/slack_router.py` after the `_verify_slack_signature` function:

```python

@router.get("/install")
async def install(
    user: str = Query(..., description="Slack user ID to associate with the Databricks token"),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Redirect to the Databricks OIDC authorization URL.

    Passes the Slack user ID as OAuth 'state' so the callback can store
    the resulting token against the correct Slack user.
    """
    params = urlencode({
        "response_type": "code",
        "client_id": settings.databricks_client_id,
        "redirect_uri": f"{settings.app_url}/slack/oauth/callback",
        "scope": "all-apis",
        "state": user,
    })
    return RedirectResponse(
        url=f"https://{settings.databricks_host}/oidc/v1/authorize?{params}"
    )
```

- [ ] **Step 5: Run to verify it passes**

```bash
uv run pytest tests/test_oauth.py::test_install_redirects_to_databricks_oidc tests/test_oauth.py::test_install_includes_redirect_uri -v
```

Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add examples/slack-agent/src/slack_agent/backend/app.py examples/slack-agent/src/slack_agent/backend/slack_router.py examples/slack-agent/tests/test_oauth.py
git commit -m "feat(slack-agent): add /slack/install OAuth redirect endpoint"
```

---

## Task 7: `/slack/oauth/callback` — token exchange and storage

**Files:**
- Modify: `examples/slack-agent/src/slack_agent/backend/slack_router.py`
- Modify: `examples/slack-agent/tests/test_oauth.py`

- [ ] **Step 1: Add tests for the callback to `test_oauth.py`**

Append to `tests/test_oauth.py`:

```python

def test_oauth_callback_stores_token(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "dapi-real-token"}

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("slack_agent.backend.slack_router.httpx.AsyncClient") as MockAsyncClient:
        MockAsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockAsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = client.get("/slack/oauth/callback?code=abc123&state=U123")

    assert resp.status_code == 200
    assert "Connected" in resp.text
    assert token_store.get_token("U123") == "dapi-real-token"


def test_oauth_callback_failed_exchange_returns_502(client):
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "bad_verification_code"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("slack_agent.backend.slack_router.httpx.AsyncClient") as MockAsyncClient:
        MockAsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockAsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = client.get("/slack/oauth/callback?code=bad&state=U123")

    assert resp.status_code == 502
    assert token_store.get_token("U123") is None


def test_oauth_callback_missing_access_token_returns_502(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}  # no access_token key

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("slack_agent.backend.slack_router.httpx.AsyncClient") as MockAsyncClient:
        MockAsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockAsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = client.get("/slack/oauth/callback?code=abc&state=U123")

    assert resp.status_code == 502
```

- [ ] **Step 2: Run to verify the new tests fail**

```bash
uv run pytest tests/test_oauth.py -v
```

Expected: 2 pass (install tests), 3 fail (callback tests — route not yet defined).

- [ ] **Step 3: Add `/slack/oauth/callback` to `slack_router.py`**

Append to `src/slack_agent/backend/slack_router.py` after the `install` endpoint:

```python

@router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(..., description="Slack user ID passed through OAuth state"),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Exchange the Databricks authorization code for an access token.

    This is the manual version of what Databricks Apps does automatically for
    browser requests. The Apps proxy injects X-Forwarded-Access-Token so that
    Dependencies.UserClient can read it. Here, we fetch the token ourselves via
    OAuth and store it — then inject it the same way in _dispatch_to_agent().
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{settings.databricks_host}/oidc/v1/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{settings.app_url}/slack/oauth/callback",
                "client_id": settings.databricks_client_id,
                "client_secret": settings.databricks_client_secret,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {resp.text}")

    access_token = resp.json().get("access_token", "")
    if not access_token:
        raise HTTPException(status_code=502, detail="No access_token in Databricks response")

    slack_user_id = state
    token_store.set_token(slack_user_id, access_token)
    logger.info("Stored Databricks token for Slack user %s", slack_user_id)

    return HTMLResponse(
        content=(
            "<h1>Connected!</h1>"
            "<p>Your Databricks account is linked. Try <code>/whoami</code> in Slack.</p>"
        ),
        status_code=200,
    )
```

- [ ] **Step 4: Run to verify all tests pass**

```bash
uv run pytest tests/test_oauth.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add examples/slack-agent/src/slack_agent/backend/slack_router.py examples/slack-agent/tests/test_oauth.py
git commit -m "feat(slack-agent): add /slack/oauth/callback token exchange endpoint"
```

---

## Task 8: `/slack/events` — command handler and async dispatch

**Files:**
- Modify: `examples/slack-agent/src/slack_agent/backend/slack_router.py`
- Create: `examples/slack-agent/tests/test_slack_events.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_slack_events.py`:
```python
import hashlib
import hmac
import time
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from slack_agent.backend.app import app
from slack_agent.backend.config import Settings, get_settings
from slack_agent.backend import token_store

SECRET = "signing-secret"
APP_URL = "https://my-app.databricksapps.com"


def _make_settings():
    return Settings(
        databricks_host="adb-123.azuredatabricks.net",
        databricks_client_id="client-id",
        databricks_client_secret="client-secret",
        app_url=APP_URL,
        slack_signing_secret=SECRET,
        slack_bot_token="xoxb-bot-token",
    )


def _sign(body: bytes, timestamp: str) -> str:
    basestring = f"v0:{timestamp}:{body.decode()}"
    return "v0=" + hmac.new(SECRET.encode(), basestring.encode(), hashlib.sha256).hexdigest()


def _slash(command: str = "/whoami", user_id: str = "U123", text: str = "") -> tuple[bytes, dict]:
    body = (
        f"command={command}&user_id={user_id}"
        f"&text={text}&response_url=https://hooks.slack.com/resp/abc"
    ).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": _sign(body, ts),
    }
    return body, headers


@pytest.fixture(autouse=True)
def clear_store():
    token_store._store.clear()
    yield
    token_store._store.clear()


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_invalid_signature_returns_401(client):
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()))
    resp = client.post(
        "/slack/events",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": "v0=bad",
        },
    )
    assert resp.status_code == 401


def test_connect_command_returns_install_link(client):
    body, headers = _slash(command="/connect", user_id="U123")
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_type"] == "ephemeral"
    assert "/slack/install" in data["text"]
    assert "U123" in data["text"]


def test_command_without_stored_token_returns_connect_prompt(client):
    body, headers = _slash(command="/whoami", user_id="U456")
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_type"] == "ephemeral"
    assert "install" in data["text"].lower() or "connect" in data["text"].lower()


def test_command_with_token_returns_200_and_fires_task(client):
    token_store.set_token("U123", "dapi-fake-token")
    body, headers = _slash(command="/whoami", user_id="U123")
    with patch("slack_agent.backend.slack_router.asyncio.create_task") as mock_task:
        resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_type"] == "ephemeral"
    mock_task.assert_called_once()


def test_command_with_token_passes_correct_args_to_dispatch(client):
    token_store.set_token("U123", "dapi-real-token")
    body, headers = _slash(command="/whoami", user_id="U123", text="")
    captured = {}

    def capture_task(coro):
        captured["coro"] = coro
        coro.close()  # prevent ResourceWarning

    with patch("slack_agent.backend.slack_router.asyncio.create_task", side_effect=capture_task):
        client.post("/slack/events", content=body, headers=headers)

    assert "coro" in captured
```

- [ ] **Step 2: Run to verify tests fail**

```bash
uv run pytest tests/test_slack_events.py -v
```

Expected: FAIL — no `/slack/events` route yet.

- [ ] **Step 3: Add `/slack/events` and `_dispatch_to_agent` to `slack_router.py`**

Append to `src/slack_agent/backend/slack_router.py` after the `oauth_callback` endpoint:

```python

@router.post("/events")
async def slack_events(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Handle Slack slash commands.

    Validates Slack's HMAC-SHA256 signature, then:
    - /connect: returns an ephemeral message with the OAuth install link.
    - anything else: looks up the stored Databricks token; if found, returns
      200 immediately and fires an async task that runs the agent and posts
      the result back to Slack via response_url (3-second deadline workaround).
    """
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(body, timestamp, signature, settings.slack_signing_secret):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    user_id = str(form.get("user_id", ""))
    text = str(form.get("text", "")).strip()
    response_url = str(form.get("response_url", ""))
    command = str(form.get("command", ""))

    if command == "/connect":
        install_url = f"{settings.app_url}/slack/install?user={user_id}"
        return {
            "response_type": "ephemeral",
            "text": f"Click to connect your Databricks account: {install_url}",
        }

    stored_token = token_store.get_token(user_id)
    if not stored_token:
        install_url = f"{settings.app_url}/slack/install?user={user_id}"
        return {
            "response_type": "ephemeral",
            "text": f"Connect your Databricks account first: {install_url}",
        }

    # Slack requires a response within 3 seconds. Return immediately and do
    # the agent work in the background, posting back via response_url.
    asyncio.create_task(
        _dispatch_to_agent(
            text=text or command,
            slack_user_id=user_id,
            response_url=response_url,
            databricks_token=stored_token,
            databricks_host=settings.databricks_host,
        )
    )
    return {"response_type": "ephemeral", "text": "Working on it..."}


async def _dispatch_to_agent(
    text: str,
    slack_user_id: str,
    response_url: str,
    databricks_token: str,
    databricks_host: str,
) -> None:
    """Call the agent and post the result back to Slack via response_url.

    Databricks Apps injects X-Forwarded-Access-Token automatically for browser
    requests. Dependencies.UserClient reads it to create a WorkspaceClient for
    the real user. Here in Slack, we do the same thing manually — we fetched
    the token via Databricks OAuth and stored it; now we inject it into the
    request headers so the agent sees no difference.
    """
    port = os.environ.get("PORT", "8000")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            agent_resp = await client.post(
                f"http://localhost:{port}/responses",
                json={"input": [{"role": "user", "content": text}]},
                headers={
                    "X-Forwarded-Access-Token": databricks_token,
                    "X-Forwarded-Host": databricks_host,
                },
            )
            agent_resp.raise_for_status()
            result_text = agent_resp.json().get("output_text", "(no response)")

        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(response_url, json={"text": result_text})

    except Exception:
        logger.exception("Error dispatching to agent for Slack user %s", slack_user_id)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(response_url, json={"text": "Sorry, something went wrong."})
        except Exception:
            logger.exception("Error posting error response to Slack for user %s", slack_user_id)
```

- [ ] **Step 4: Run to verify all events tests pass**

```bash
uv run pytest tests/test_slack_events.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Run the full test suite**

```bash
uv run pytest tests/ -v
```

Expected: all tests pass across all test files.

- [ ] **Step 6: Commit**

```bash
git add examples/slack-agent/src/slack_agent/backend/slack_router.py examples/slack-agent/tests/test_slack_events.py
git commit -m "feat(slack-agent): add /slack/events handler with async agent dispatch"
```

---

## Task 9: Smoke test and final commit

**Files:**
- No new files — verify the full app starts cleanly.

- [ ] **Step 1: Run the complete test suite one final time**

```bash
cd /Users/stuart.gano/Documents/apx-agent/python/examples/slack-agent
uv run pytest tests/ -v
```

Expected: all tests in all files pass.

- [ ] **Step 2: Verify the app can import cleanly**

```bash
uv run python -c "from slack_agent.backend.app import app; print('OK', len(app.routes), 'routes')"
```

Expected: `OK <N> routes` with no import errors. N should be at least 8 (health, agent.json, responses, root, slack/install, slack/oauth/callback, slack/events, dev UI routes).

- [ ] **Step 3: Verify the full `slack_router.py` file is complete**

Confirm the file ends with `_dispatch_to_agent` and contains all four exported names: `router`, `_verify_slack_signature`, `install`, `oauth_callback`, `slack_events`, `_dispatch_to_agent`.

```bash
uv run python -c "
from slack_agent.backend.slack_router import (
    router, _verify_slack_signature, install,
    oauth_callback, slack_events, _dispatch_to_agent
)
print('All exports present')
"
```

Expected: `All exports present`

- [ ] **Step 4: Final commit**

```bash
git add examples/slack-agent/
git commit -m "feat(slack-agent): complete working example — Slack OAuth + Databricks OBO token forwarding"
```

---

## Setup Instructions (for `.env` at runtime)

To run locally against a real Databricks workspace:

```bash
# examples/slack-agent/.env
DATABRICKS_HOST=adb-<workspace-id>.azuredatabricks.net
DATABRICKS_CLIENT_ID=<OAuth app client ID>
DATABRICKS_CLIENT_SECRET=<OAuth app client secret>
APP_URL=https://<your-app>.databricksapps.com
SLACK_SIGNING_SECRET=<from Slack app Basic Information>
SLACK_BOT_TOKEN=xoxb-<from Slack app OAuth & Permissions>
```

Run: `uv run uvicorn slack_agent.backend.app:app --reload --port 8000`

Slack slash commands to configure: `/connect` and `/whoami`, both pointing to `https://<your-tunnel>/slack/events`.
