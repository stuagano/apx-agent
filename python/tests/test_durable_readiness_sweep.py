"""Sweep table builder: one row per example, per-stage status, first blocker."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "durable_readiness_sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("durable_readiness_sweep", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves annotations via sys.modules
    spec.loader.exec_module(module)
    return module


def _payload(*pairs: tuple[str, str, str]) -> str:
    return json.dumps({"Durable readiness": [{"name": n, "status": s, "detail": d, "fix": None} for n, s, d in pairs]})


def test_rows_ok_fail_and_env() -> None:
    sweep = _load()
    ok = sweep.parse_payload("good", _payload(*[(n, "ok", "fine") for n in sweep.STAGES]), "")
    fail = sweep.parse_payload("bad", _payload(("Config", "ok", ""), ("Compile", "fail", "user_identity: nope"),
                                              ("Startup", "skip", "blocked by Compile"),
                                              ("Request", "skip", "blocked by Compile"),
                                              ("Deploy", "skip", "blocked by Compile")), "")
    env = sweep.parse_payload("noenv", "", "error: Unable to find lockfile at `uv.lock`\nmore")
    chatter = sweep.parse_payload("trace", "", "Using CPython 3.13\nTraceback (most recent call last):\nModuleNotFoundError: No module named 'sqlglot'\n")
    table = sweep.build_table([ok, fail, env, chatter])
    assert "| trace | env | env | env | env | env | ModuleNotFoundError: No module named 'sqlglot' |" in table
    lines = table.splitlines()
    assert lines[0].startswith("| example |")
    assert "| good | ✓ | ✓ | ✓ | ✓ | ✓ |" in table
    assert "| bad | ✓ | ✗ | – | – | – | Compile: user_identity: nope |" in table
    assert "| noenv | env | env | env | env | env | error: Unable to find lockfile at `uv.lock` |" in table
