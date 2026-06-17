"""The cache-regen helper rewrites .apx/schema.json from a changed .apx/okf bundle."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_regen_writes_cache_from_bundle(tmp_path):
    from apx_agent._okf import write_okf_bundle
    m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
    apx = tmp_path / ".apx"
    write_okf_bundle(m, apx / "okf", timestamp="z")
    (apx / "schema.json").write_text("{}")  # stale cache

    script = Path(__file__).resolve().parents[2] / "scripts" / "regen-okf-cache.py"
    r = subprocess.run([sys.executable, str(script), str(apx / "okf" / "tables" / "t.md")],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode in (0, 1), r.stderr
    assert json.loads((apx / "schema.json").read_text())["tables"] == {"t": ["a(int)"]}
