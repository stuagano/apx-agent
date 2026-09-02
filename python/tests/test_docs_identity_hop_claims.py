"""#633 claim-vs-reality: identity-per-hop claims must name the FMAPI-SP limit.

``docs/multi-agent/a2a.md`` already states the reality: when app A calls app B,
B's internal LLM (FMAPI) calls use B's own service principal, not A's OBO token.
The positioning and composition pages sold "identity passed through per hop"
without that scope, so a reader could believe the callee's model calls also run
as the asking user. These assertions keep the marketing pages honest: the
per-hop claim must be scoped to *tool* calls and cross-link the A2A caveat.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _read(*parts: str) -> str:
    path = DOCS.joinpath(*parts)
    assert path.exists(), f"missing docs page: {path}"
    return path.read_text()


def test_a2a_page_still_states_fmapi_uses_callee_identity() -> None:
    """The ground truth the other pages must not contradict."""
    text = _read("multi-agent", "a2a.md").lower()
    assert "fmapi uses the callee app's own identity" in text
    assert "own sp token, not a's" in text


@pytest.mark.parametrize(
    "parts",
    [("positioning.md",), ("agents", "composition.md")],
)
def test_identity_per_hop_claims_are_scoped_to_tools(parts: tuple[str, ...]) -> None:
    """#633: per-hop identity claims must name the callee-SP limit for LLM calls."""
    text = _read(*parts)
    lowered = text.lower()
    assert "per hop" in lowered or "every form" in lowered, (
        f"{parts} no longer makes a per-hop identity claim — update this test"
    )
    assert "service principal" in lowered, (
        f"{parts} claims per-hop identity without naming the callee service "
        "principal that runs the callee's LLM calls (#633)"
    )
    assert "multi-agent/a2a.md" in text or "a2a.md" in text, (
        f"{parts} must cross-link the A2A auth caveat (#633)"
    )
    assert "#633" in text


def test_identity_passthrough_page_scopes_every_hop_claim() -> None:
    """The safety page's "every hop" wording must carry the same caveat."""
    text = _read("safety", "identity-passthrough.md")
    assert "every hop" in text.lower()
    assert "service principal" in text.lower()
    assert "a2a.md" in text


def test_apps_family_policy_is_operator_declared() -> None:
    """The compiler consumes policy; it does not invent project TOML."""
    text = _read(
        "superpowers", "specs", "2026-09-01-apps-authorization-compiler-design.md",
    )
    section = text.split("## App-Family Permissions", 1)[1].split("## Bundle Reconciliation", 1)[0]
    normalized = " ".join(section.split())
    assert "Operators declare one group-only Apps permission block" in normalized
    assert "APX compiles that configured policy" in normalized
    assert "APX does not create or generate this TOML block" in normalized
