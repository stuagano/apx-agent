"""Framework self-introspection helpers (PEP 610 pin discovery).

Adapted from agent-foundry's ``_meta.py`` for ``apx-agent``.

Used to:

* Stamp a commit SHA / tag into scaffolded ``pyproject.toml`` pins.
* Warn at ``apx deploy --target apps`` when the *running* install does
  not match the repo's git pin (global CLI shadowing the project
  ``.venv`` — deploy bundles the running wheel into the App).
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from importlib import metadata as _metadata
from pathlib import Path

_DIST_NAME = "apx-agent"


@dataclass(frozen=True)
class DiscoveryResult:
    """Outcome of inspecting the installed ``apx-agent`` distribution."""

    sha: str | None
    requested_ref: str
    reason: str


def discover_framework_sha() -> DiscoveryResult:
    """Best-effort lookup of the installed ``apx-agent`` git SHA (PEP 610)."""
    try:
        dist = _metadata.distribution(_DIST_NAME)
    except _metadata.PackageNotFoundError:
        return DiscoveryResult(
            sha=None,
            requested_ref="",
            reason=(
                f"distribution {_DIST_NAME!r} not found in the current "
                "environment — install apx-agent (not just import from a "
                "source tree) for SHA discovery to work."
            ),
        )

    raw = dist.read_text("direct_url.json")
    if raw is None:
        return DiscoveryResult(
            sha=None,
            requested_ref="",
            reason=(
                "direct_url.json missing from the installed distribution — "
                "apx-agent was probably installed from a wheel / sdist / "
                "PyPI without VCS metadata. No SHA available."
            ),
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return DiscoveryResult(
            sha=None,
            requested_ref="",
            reason=f"direct_url.json is not valid JSON: {exc}",
        )

    vcs_info = payload.get("vcs_info") or {}
    sha = vcs_info.get("commit_id") or None
    requested_ref = vcs_info.get("requested_revision") or ""

    if sha is None:
        return DiscoveryResult(
            sha=None,
            requested_ref=requested_ref,
            reason=_explain_missing_vcs(payload),
        )
    return DiscoveryResult(sha=sha, requested_ref=requested_ref, reason="")


def _explain_missing_vcs(payload: dict[str, object]) -> str:
    dir_info = payload.get("dir_info")
    url = payload.get("url")
    url_str = url if isinstance(url, str) else ""

    if isinstance(dir_info, dict) and dir_info.get("editable"):
        location = f" from {url_str!r}" if url_str else ""
        return (
            f"apx-agent is installed in editable mode{location}; "
            "no commit SHA is available."
        )
    if isinstance(dir_info, dict):
        location = f" from {url_str!r}" if url_str else ""
        return (
            f"apx-agent was installed from a local path{location}; "
            "no commit SHA is available."
        )
    return (
        "direct_url.json has no vcs_info.commit_id — the install source "
        "is not a recognised VCS URL."
    )


@dataclass(frozen=True)
class PinComparison:
    """Outcome of comparing the running build against the repo pin."""

    matches: bool
    skipped: bool
    pinned_ref: str | None
    running_sha: str | None
    message: str


def read_pinned_framework_ref(pyproject_path: Path) -> str | None:
    """Return the git ref ``apx-agent`` is pinned to in ``pyproject.toml``."""
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    deps = (data.get("project") or {}).get("dependencies") or []
    for dep in deps:
        if isinstance(dep, str) and _requirement_name(dep) == _DIST_NAME:
            return _extract_vcs_ref(dep)
    return None


def _requirement_name(dep: str) -> str:
    head = dep.split("@", 1)[0]
    head = head.split("[", 1)[0]
    return head.strip().replace("_", "-").lower()


def _extract_vcs_ref(dep: str) -> str | None:
    _, _, url = dep.partition("@")
    url = url.strip().split("#", 1)[0].strip()
    if not url.startswith("git+") or "@" not in url:
        return None
    ref = url.rsplit("@", 1)[1].strip()
    if not ref or "://" in ref or ref.endswith(".git"):
        return None
    return ref


def _short(ref: str) -> str:
    return ref if len(ref) <= 12 else ref[:12]


def _refs_match(pinned: str, sha: str, requested_ref: str) -> bool:
    for candidate in (sha, requested_ref):
        if candidate and (
            pinned == candidate
            or candidate.startswith(pinned)
            or pinned.startswith(candidate)
        ):
            return True
    return False


def compare_pinned_sha(project_root: Path) -> PinComparison:
    """Compare the running ``apx-agent`` build against the repo's pin.

    Never raises: unreadable configs and unresolvable installs degrade to
    ``skipped=True`` so callers can treat "cannot compare" as benign.
    """
    pinned = read_pinned_framework_ref(project_root / "pyproject.toml")
    if pinned is None:
        return PinComparison(
            matches=True,
            skipped=True,
            pinned_ref=None,
            running_sha=None,
            message=(
                "apx-agent is not pinned to a git ref in pyproject.toml; "
                "SHA check skipped."
            ),
        )

    running = discover_framework_sha()
    if running.sha is None:
        return PinComparison(
            matches=True,
            skipped=True,
            pinned_ref=pinned,
            running_sha=None,
            message=(
                f"repo pins {_short(pinned)} but the running build has no "
                f"resolvable VCS SHA ({running.reason}); SHA check skipped."
            ),
        )

    if _refs_match(pinned, running.sha, running.requested_ref):
        return PinComparison(
            matches=True,
            skipped=False,
            pinned_ref=pinned,
            running_sha=running.sha,
            message=(
                f"running build {_short(running.sha)} matches the pinned "
                f"ref {_short(pinned)}."
            ),
        )

    return PinComparison(
        matches=False,
        skipped=False,
        pinned_ref=pinned,
        running_sha=running.sha,
        message=(
            f"pinned ref {_short(pinned)} != running build "
            f"{_short(running.sha)}. `apx deploy --target apps` bundles the "
            f"running build into the App wheel, so the deploy would ship a "
            f"framework that does not match pyproject.toml. This usually "
            f"means a globally-installed apx is shadowing the project venv. "
            f"Fix: run `uv sync` and invoke deploy via `uv run apx` from "
            f"the project root. See docs/upgrade.md."
        ),
    )


__all__ = [
    "DiscoveryResult",
    "PinComparison",
    "compare_pinned_sha",
    "discover_framework_sha",
    "read_pinned_framework_ref",
]
