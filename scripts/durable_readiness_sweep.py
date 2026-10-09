"""Sweep python/examples with `apx-agent doctor --durable` and print a readiness table.

Runs each example in its own environment (`uv run --project`), never re-locks
(UV_FROZEN is respected), writes nothing, and always exits 0 — it is a report.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "python" / "examples"
STAGES = ("Config", "Compile", "Startup", "Request", "Deploy")
SYMBOL = {"ok": "✓", "fail": "✗", "skip": "–", "warn": "!"}
WITH_SDK = "databricks-agentbricks>=0.4.0,<0.5"


@dataclass
class Row:
    example: str
    statuses: dict[str, str]
    blocker: str


@dataclass
class Discovery:
    found: list[Path]
    broken: list[Row]


def _env_reason(stderr: str) -> str:
    """The error line (uv `error:` / Python `XError:`), not uv's progress chatter."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return next((line for line in lines if re.match(r"(error|\w*Error)\b", line)), lines[-1] if lines else "no output")


def _env_row(example: str, reason: str) -> Row:
    return Row(example, {stage: "env" for stage in STAGES}, reason)


def _load_json(stdout: str) -> Any:
    """Parse stdout as JSON, tolerating log noise before the first line starting with `{`."""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        lines = stdout.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("{")), None)
        if start is None:
            raise
        return json.loads("\n".join(lines[start:]))


def parse_payload(example: str, stdout: str, stderr: str) -> Row:
    try:
        payload = _load_json(stdout)
    except json.JSONDecodeError:
        return _env_row(example, _env_reason(stderr))
    checks = payload.get("Durable readiness") if isinstance(payload, dict) else None
    if not isinstance(checks, list):
        return _env_row(example, "no Durable readiness group (apx-agent without --durable?)")
    valid = [c for c in checks if isinstance(c, dict) and "name" in c and "status" in c]
    statuses = {c["name"]: SYMBOL.get(c["status"], str(c["status"])) for c in valid if c["name"] in STAGES}
    blocker = next((f"{c['name']}: {c.get('detail')}" for c in valid if c["status"] == "fail"), "")
    return Row(example, statuses, blocker)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def build_table(rows: list[Row]) -> str:
    header = "| example | " + " | ".join(STAGES) + " | first blocker |"
    rule = "|" + "---|" * (len(STAGES) + 2)
    body = [f"| {_cell(r.example)} | " + " | ".join(_cell(r.statuses.get(s, "–")) for s in STAGES)
            + f" | {_cell(r.blocker)} |" for r in rows]
    return "\n".join([header, rule, *body])


def _apx_examples(root: Path = EXAMPLES) -> Discovery:
    """Apx examples under root, plus env rows for pyprojects that cannot be read."""
    found, broken = [], []
    for pyproject in sorted(root.glob("*/pyproject.toml")):
        try:
            data = tomllib.loads(pyproject.read_text())
        except (tomllib.TOMLDecodeError, OSError) as exc:
            broken.append(_env_row(pyproject.parent.name, f"invalid pyproject.toml: {exc}"))
            continue
        apx = data.get("tool", {}).get("apx")
        if isinstance(apx, dict) and "agent" in apx:
            found.append(pyproject.parent)
    return Discovery(found, broken)


def main() -> None:
    discovery = _apx_examples()
    rows = discovery.broken
    for example in discovery.found:
        try:
            proc = subprocess.run(
                ["uv", "run", "--project", str(example), "--with", WITH_SDK,
                 "apx-agent", "doctor", "--durable", "--offline", "--json"],
                cwd=example, capture_output=True, text=True, timeout=600,
            )
            rows.append(parse_payload(example.name, proc.stdout, proc.stderr))
        except subprocess.TimeoutExpired:
            rows.append(_env_row(example.name, "timed out after 600s"))
    print(build_table(sorted(rows, key=lambda r: r.example)))
    sys.exit(0)


if __name__ == "__main__":
    main()
