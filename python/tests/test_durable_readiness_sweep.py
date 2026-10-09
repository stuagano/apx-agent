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


def test_apx_examples_survive_malformed_and_non_apx(tmp_path: Path) -> None:
    sweep = _load()
    for name, text in {
        "good": '[tool.apx.agent]\nname = "x"\n',
        "broken": "[tool.apx\n",
        "plain": '[project]\nname = "p"\n',
        "scalar": "[tool]\napx = 3\n",
    }.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").write_text(text)
    discovery = sweep._apx_examples(tmp_path)
    found, broken = discovery.found, discovery.broken
    assert [p.name for p in found] == ["good"]
    assert [r.example for r in broken] == ["broken"]
    assert broken[0].blocker.startswith("invalid pyproject.toml: ")
    assert set(broken[0].statuses.values()) == {"env"}


def test_table_escapes_pipes_and_newlines() -> None:
    sweep = _load()
    row = sweep.parse_payload("p", _payload(("Config", "fail", "a | b\nc")), "")
    line = sweep.build_table([row]).splitlines()[-1]
    assert line.endswith("| Config: a \\| b c |")
    assert len(line.replace("\\|", "").split("|")) == len(sweep.STAGES) + 4


def test_parse_payload_tolerates_malformed_output() -> None:
    sweep = _load()
    odd = json.dumps({"Durable readiness": [{"name": "Config", "status": "weird", "detail": ""}, {"status": "ok"}, "junk"]})
    assert sweep.parse_payload("u", odd, "").statuses == {"Config": "weird"}
    missing = sweep.parse_payload("m", json.dumps({"Other": []}), "stderr guess")
    assert missing.blocker == "no Durable readiness group (apx-agent without --durable?)"
    assert set(missing.statuses.values()) == {"env"}
    noisy = sweep.parse_payload("n", "INFO starting\nwarn\n" + _payload(*[(n, "ok", "") for n in sweep.STAGES]), "")
    assert set(noisy.statuses.values()) == {"✓"}


def test_doctor_command_never_relocks(tmp_path: Path) -> None:
    sweep = _load()
    command = sweep.doctor_command(tmp_path)
    assert command[:2] == ["uv", "run"]
    assert "--frozen" in command
    assert "--isolated" not in command
    assert command[command.index("--project") + 1] == str(tmp_path)
    assert command[-4:] == ["doctor", "--durable", "--offline", "--json"]
