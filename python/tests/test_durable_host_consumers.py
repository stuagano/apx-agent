"""Freeze production ChatAgent / AppKit consumers for #864.

Retirement of ``_chat_agent.py`` or legacy ``/invocations`` requires this set
to shrink with a migrated caller, not a silent import deletion.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "apx_agent"

CHAT_NAMES = frozenset({"chat_agent_for", "compile_to_chat_agent", "log_agent"})
INVOCATIONS_NAME = "mount_invocations_route"

# Production modules that still compile or serve the MLflow ChatAgent.
CHAT_AGENT_MODULES = frozenset({
    "_chat_agent.py",  # definitions
    "__init__.py",     # public re-export
    "cli.py",          # log_agent + eval test
    "_run_once.py",
    "_eval.py",
    "_dev.py",
    "_invocations.py",
    "_a2a.py",
})

# Compatibility FastAPI host still mounts ChatAgent /invocations.
INVOCATIONS_MODULES = frozenset({
    "_invocations.py",
    "_wiring.py",
    "__init__.py",
})

RETIRED_PATHS = (
    SRC / "_appkit_host_generator.py",
    SRC / "_appkit_tool_bridge.py",
    SRC / "_apps_host_manifest.py",
)


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1] for alias in node.names)
    return names


def _modules_importing(wanted: frozenset[str] | str) -> set[str]:
    needed = {wanted} if isinstance(wanted, str) else set(wanted)
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        if _imported_names(path) & needed:
            found.add(path.name)
    return found


def test_chat_agent_production_callers_are_inventoried() -> None:
    found = _modules_importing(CHAT_NAMES)
    extra = found - CHAT_AGENT_MODULES
    required = CHAT_AGENT_MODULES - {"_chat_agent.py"}
    missing = required - found
    assert extra == set(), f"new ChatAgent callers; update docs/design/durable-host-consumers.md: {sorted(extra)}"
    assert missing == set(), f"inventoried ChatAgent importer disappeared: {sorted(missing)}"


def test_legacy_invocations_mount_is_inventoried() -> None:
    found = _modules_importing(INVOCATIONS_NAME)
    extra = found - INVOCATIONS_MODULES
    assert extra == set(), f"new /invocations mount sites: {sorted(extra)}"
    assert "_wiring.py" in found
    assert "__init__.py" in found


def test_appkit_host_files_stay_retired() -> None:
    existing = [str(path) for path in RETIRED_PATHS if path.exists()]
    assert existing == []


def test_native_launcher_does_not_import_chat_agent() -> None:
    text = (SRC / "_serve.py").read_text(encoding="utf-8")
    assert "chat_agent_for" not in text
    assert "compile_to_chat_agent" not in text
    assert "durable_agent_server" in text
