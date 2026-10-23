"""Claim-vs-reality: the shipped Discover bundle is built and wired to the graph API."""

from __future__ import annotations

from pathlib import Path

from ctk import Artifact, verify

import apx_agent

STATIC = Path(apx_agent.__file__).parent / "_static" / "topology"


def _bundle_js() -> str:
    return "\n".join(p.read_text() for p in (STATIC / "assets").glob("*.js"))


def test_discover_page_is_built_into_the_wheel_tree():
    verify(
        Artifact(str(STATIC / "discover.html"), min_bytes=200, must_contain="/_apx/topology/assets/"),
        Artifact(str(STATIC / "index.html"), min_bytes=200, must_contain="/_apx/topology/assets/"),
    )


def test_bundle_calls_graph_api_and_no_wire_api():
    js = _bundle_js()
    assert "/_apx/discover/graph.json" in js
    assert "/_apx/discover/impact/" in js
    assert "/_apx/discover/wire" not in js
    assert "/_apx/discover/unwire" not in js
