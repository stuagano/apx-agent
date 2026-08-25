"""Packaging-config invariants for the wheel build.

These guard against build-config regressions that pass locally (older
hatchling silently dedupes) but break a fresh-container build (newer hatchling
raises a hard error) — the failure mode behind the issue #116 follow-up.
"""
from __future__ import annotations

from pathlib import Path

try:
    import tomllib  # py>=3.11
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
TOPOLOGY_UI = PYPROJECT.parent / "dev-ui" / "topology" / "src"


def _wheel_target() -> dict:
    doc = tomllib.loads(PYPROJECT.read_text())
    return doc.get("tool", {}).get("hatch", {}).get("build", {}).get(
        "targets", {}
    ).get("wheel", {})


def test_no_force_include_overlaps_packages() -> None:
    """A force-include source that lives inside a ``packages`` directory maps
    the same files into the wheel twice (once via ``packages``, once via
    ``force-include``). Older hatchling dedupes with a "Duplicate name"
    warning; newer hatchling (fresh Databricks Apps container) raises
    ``ValueError: A second file is being added to the wheel archive at the same
    path`` and the git+https install fails. Keep data files in ``packages``
    only.
    """
    wheel = _wheel_target()
    packages = wheel.get("packages", [])
    force_include = wheel.get("force-include", {})

    overlaps = []
    for src in force_include:
        src_path = Path(src)
        for pkg in packages:
            pkg_path = Path(pkg)
            if src_path == pkg_path or pkg_path in src_path.parents:
                overlaps.append((src, pkg))

    assert not overlaps, (
        "force-include sources overlap a packages directory (double-include): "
        f"{overlaps}. Files already under a packages root must not be "
        "force-included — see tests/test_packaging.py docstring."
    )


def test_topology_assets_are_packaged() -> None:
    """The built _static/topology React bundle (served by _dev.py) must ship in
    the wheel. It lives under src/apx_agent/, so the ``packages`` entry covers
    it — assert the files exist on disk and the package root is declared."""
    pkg_root = PYPROJECT.parent / "src" / "apx_agent"
    topo = pkg_root / "_static" / "topology"
    assert (topo / "index.html").exists(), "topology bundle missing from source"

    packages = _wheel_target().get("packages", [])
    assert "src/apx_agent" in packages, (
        "src/apx_agent must be a wheel package so _static/topology ships"
    )


def test_workflow_rail_keeps_execution_and_sender_state_truthful() -> None:
    """Declared routes must not masquerade as trace evidence or queue invisibly."""
    app = (TOPOLOGY_UI / "App.tsx").read_text()
    chat = (TOPOLOGY_UI / "ChatDock.tsx").read_text()
    panel = (TOPOLOGY_UI / "WorkflowPanel.tsx").read_text()

    assert "const routeNodeIds = observedNodeIds;" in app
    assert "const routeEdgeIds = observedEdgeIds;" in app
    assert "new Set([...observedNodeIds, ...declaredRouteNodeIds])" not in app
    assert "canRun={!EMBED}" in app
    assert "senderBusy={chatSending}" in app
    assert "onSendingChange={setChatSending}" in app
    assert "if (EMBED) return;" in app
    assert "if (chatSending || activeWorkflowRun)" in app
    assert "onSendingChange?: (sending: boolean) => void;" in chat
    assert "if (!text || sending) return false;" in chat
    assert "if (!dispatched) onRunQuestion?.(runRequestId, false);" in chat
    assert "disabled={runDisabled}" in panel
    assert "Run examples are unavailable in embedded topology" in panel
