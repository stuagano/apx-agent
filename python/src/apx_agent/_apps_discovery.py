"""Discover apx agents deployed as Databricks Apps by probing their A2A card.

``agents list`` reads UC-registered models (the deploy-time tag flow in
``_fleet``). Agents deployed to the Apps target that skipped UC registration are
invisible to that query — so this finds them the other way around: list the
workspace's Apps, probe each running one's ``/.well-known/agent.json``, and keep
the ones that answer with an apx/A2A agent card.

The probe uses the SDK's own credentials (Bearer token via
``ws.config.authenticate()``), which the Databricks Apps proxy accepts. Any app
the caller can't list, can't reach, or that isn't an apx agent is simply
omitted — discovery is best-effort and never raises.
"""

from __future__ import annotations

import concurrent.futures
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class AppAgentInfo:
    """An apx agent discovered by probing a running Databricks App."""

    name: str
    app_name: str
    url: str
    description: str | None
    tool_count: int
    state: str
    tools: tuple[str, ...] = ()
    """Skill / tool names from the A2A card (best-effort)."""


def _bearer_headers(ws: Any) -> dict[str, str]:
    """Auth headers from the SDK config (the configured Bearer credentials)."""
    try:
        return dict(ws.config.authenticate() or {})
    except Exception:
        return {}


def _probe_card(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any] | None:
    """GET ``<url>/.well-known/agent.json``; return the parsed card or ``None``.

    ``None`` means "not an apx agent / unreachable" — every error is non-fatal.
    """
    card_url = url.rstrip("/") + "/.well-known/agent.json"
    req = urllib.request.Request(card_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (workspace URL)
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    # An A2A card carries a name and a skills list — that's the apx signal.
    if isinstance(data, dict) and data.get("name") and "skills" in data:
        return data
    return None


def _norm(s: str | None) -> str:
    """Normalize a name for matching: drop ``-``/``_``, lowercase.

    App names use hyphens (``data-triage-agent``) while card names often use
    underscores (``data_triage_agent``); this lets either resolve the app.
    """
    return (s or "").replace("-", "").replace("_", "").lower()


def fetch_app_card(ws: Any, name: str, *, timeout: float = 4.0) -> dict[str, Any] | None:
    """Fetch the full A2A card for one Databricks App by name.

    Matches ``name`` against the app name (exact first, then normalized so
    hyphen/underscore variants resolve), probes its ``/.well-known/agent.json``,
    and returns ``{"app_name", "url", "card"}`` — or ``None`` if no running app
    by that name answers with a card.
    """
    try:
        apps = list(ws.apps.list())
    except Exception:
        return None
    headers = _bearer_headers(ws)
    target = _norm(name)
    matches = [
        app for app in apps
        if getattr(app, "url", None)
        and (getattr(app, "name", "") == name or _norm(getattr(app, "name", "")) == target)
    ]
    # Exact app-name match before a normalized one.
    matches.sort(key=lambda a: getattr(a, "name", "") != name)
    for app in matches:
        card = _probe_card(getattr(app, "url"), headers, timeout)
        if card is not None:
            return {"app_name": getattr(app, "name", name), "url": getattr(app, "url"), "card": card}
    return None


def _env_app_candidates() -> list[tuple[str, str]]:
    """Optional ``APX_DISCOVER_APP_URLS`` seed when ``apps.list`` is empty.

    Databricks Apps OBO tokens are scoped to ``user_api_scopes`` (often just
    ``sql`` + ``serving``), so ``apps.list`` fails inside the App even for the
    signed-in user. Deploy can seed peer App URLs here:

      APX_DISCOVER_APP_URLS=mcp-data-inspector=https://…,https://other…

    Entries are ``name=url`` or bare ``url`` (name inferred from hostname).
    """
    import os
    import re

    raw = (os.environ.get("APX_DISCOVER_APP_URLS") or "").strip()
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part and not part.startswith("http"):
            name, _, url = part.partition("=")
            name, url = name.strip(), url.strip()
        else:
            url = part
            host = re.sub(r"^https?://", "", url).split("/")[0]
            name = host.split(".")[0].rsplit("-", 1)[0] if host else "app"
            # hostname is often ``<app>-<workspaceId>`` — strip trailing digits block
            m = re.match(r"^(.+)-(\d{10,})$", host.split(".")[0] if host else "")
            if m:
                name = m.group(1)
        if url.startswith("http"):
            out.append((name or "app", url.rstrip("/")))
    return out


def discover_app_agents(
    ws: Any, *, timeout: float = 2.5, max_workers: int = 24
) -> list[AppAgentInfo]:
    """Find apx agents running as Databricks Apps.

    Lists the workspace's Apps and probes each one's A2A card concurrently,
    keeping the ones that answer with an apx card. The probe is both the
    liveness and the identity check: a stopped or non-apx app simply doesn't
    return a card. (``ws.apps.list()`` does not populate per-app state — that
    needs a ``get()`` per app — so we can't pre-filter to RUNNING cheaply; the
    bounded-timeout probe is the filter instead.)

    When ``apps.list`` is empty or denied (common for App SP / scoped OBO),
    also probes URLs from ``APX_DISCOVER_APP_URLS`` (see ``_env_app_candidates``).

    :param ws: A Databricks ``WorkspaceClient``.
    :param timeout: Per-probe HTTP timeout in seconds.
    :param max_workers: Max concurrent probes.
    :returns: Discovered app agents (possibly empty); never raises.
    """
    candidates: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    try:
        apps = list(ws.apps.list())
    except Exception:
        apps = []

    for app in apps:
        url = getattr(app, "url", None)
        if not url:
            continue
        key = url.rstrip("/")
        if key in seen_urls:
            continue
        seen_urls.add(key)
        candidates.append((getattr(app, "name", "?"), key))

    for name, url in _env_app_candidates():
        key = url.rstrip("/")
        if key in seen_urls:
            continue
        seen_urls.add(key)
        candidates.append((name, key))

    if not candidates:
        return []

    headers = _bearer_headers(ws)
    found: list[AppAgentInfo] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {
            ex.submit(_probe_card, url, headers, timeout): (app_name, url)
            for (app_name, url) in candidates
        }
        for fut in concurrent.futures.as_completed(futs):
            app_name, url = futs[fut]
            card = fut.result()
            if card is None:
                continue
            skills = card.get("skills") or []
            tool_names = tuple(
                str(s.get("name"))
                for s in skills
                if isinstance(s, dict) and s.get("name")
            )
            found.append(
                AppAgentInfo(
                    name=str(card.get("name") or app_name),
                    app_name=app_name,
                    url=url,
                    description=card.get("description"),
                    tool_count=len(tool_names) if tool_names else len(skills),
                    state="RUNNING",  # it answered the card, so it's up
                    tools=tool_names,
                )
            )
    found.sort(key=lambda a: a.name)
    return found
