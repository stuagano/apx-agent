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


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "regen-okf-cache.py"


def test_regen_invalid_bundle_returns_zero_and_writes_no_cache(tmp_path):
    # A bundle whose dataset concept lacks catalog/schema is unparseable
    # (okf_manifest -> None). The hook must stay green and create NO cache file.
    apx = tmp_path / ".apx"
    tdir = apx / "okf" / "tables"
    tdir.mkdir(parents=True)
    md = tdir / "t.md"
    md.write_text(
        "---\ntype: Unity Catalog Table\ntitle: t\ndescription: d\ntimestamp: z\n---\n\n# Schema\n"
    )  # no datasets/ concept -> okf_manifest returns None

    r = subprocess.run([sys.executable, str(_SCRIPT), str(md)], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (apx / "schema.json").exists()


def test_regen_empty_bundle_returns_zero_and_writes_no_cache(tmp_path):
    # An empty .apx/okf with a stray (reserved) file and no table concepts.
    apx = tmp_path / ".apx"
    (apx / "okf").mkdir(parents=True)
    idx = apx / "okf" / "index.md"
    idx.write_text('---\nokf_version: "0.1"\n---\n\n# Subdirectories\n')

    r = subprocess.run([sys.executable, str(_SCRIPT), str(idx)], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (apx / "schema.json").exists()


def test_regen_output_matches_cli_writer_byte_for_byte(tmp_path):
    # No flip-flop: the bytes the regen hook writes are identical to what the CLI
    # cache writer emits for the same bundle, so the two never rewrite each other.
    from apx_agent._okf import write_okf_bundle, okf_manifest, dump_schema_cache
    m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"], "u": ["b(string)"]}}
    apx = tmp_path / ".apx"
    write_okf_bundle(m, apx / "okf", timestamp="z")
    md = str(apx / "okf" / "tables" / "t.md")

    r = subprocess.run([sys.executable, str(_SCRIPT), md], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 1, r.stderr  # wrote the cache
    hook_bytes = (apx / "schema.json").read_bytes()
    cli_bytes = dump_schema_cache(okf_manifest(apx / "okf")).encode()
    assert hook_bytes == cli_bytes

    # And re-running the hook now finds it in sync (idempotent, exit 0, no rewrite).
    r2 = subprocess.run([sys.executable, str(_SCRIPT), md], capture_output=True, text=True, cwd=tmp_path)
    assert r2.returncode == 0, r2.stderr
    assert (apx / "schema.json").read_bytes() == hook_bytes
