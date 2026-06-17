"""The cache-regen helper rewrites .apx/schema.json from a changed .apx/okf bundle."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_regen_writes_cache_and_exit_codes(tmp_path):
    from apx_agent._okf import write_okf_bundle
    m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
    apx = tmp_path / ".apx"
    write_okf_bundle(m, apx / "okf", timestamp="z")
    (apx / "schema.json").write_text("{}")  # stale cache

    script = Path(__file__).resolve().parents[2] / "scripts" / "regen-okf-cache.py"
    md = str(apx / "okf" / "tables" / "t.md")

    # First run: stale cache -> rewritten, exit 1 (pre-commit "fixed it" convention)
    r1 = subprocess.run([sys.executable, str(script), md], capture_output=True, text=True, cwd=tmp_path)
    assert r1.returncode == 1, r1.stderr
    assert json.loads((apx / "schema.json").read_text())["tables"] == {"t": ["a(int)"]}

    # Second run: cache already in sync -> no change, exit 0
    r2 = subprocess.run([sys.executable, str(script), md], capture_output=True, text=True, cwd=tmp_path)
    assert r2.returncode == 0, r2.stderr
