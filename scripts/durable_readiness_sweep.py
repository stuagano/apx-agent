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


def _env_reason(stderr: str) -> str:
    """The error line (uv `error:` / Python `XError:`), not uv's progress chatter."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return next((line for line in lines if re.match(r"(error|\w*Error)\b", line)), lines[-1] if lines else "no output")


def parse_payload(example: str, stdout: str, stderr: str) -> Row:
    try:
        checks = json.loads(stdout)["Durable readiness"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return Row(example, {stage: "env" for stage in STAGES}, _env_reason(stderr))
    statuses = {c["name"]: SYMBOL[c["status"]] for c in checks if c["name"] in STAGES}
    blocker = next((f"{c['name']}: {c['detail']}" for c in checks if c["status"] == "fail"), "")
    return Row(example, statuses, blocker)


def build_table(rows: list[Row]) -> str:
    header = "| example | " + " | ".join(STAGES) + " | first blocker |"
    rule = "|" + "---|" * (len(STAGES) + 2)
    body = [f"| {r.example} | " + " | ".join(r.statuses[s] if s in r.statuses else "–" for s in STAGES)
            + f" | {r.blocker} |" for r in rows]
    return "\n".join([header, rule, *body])


def _apx_examples() -> list[Path]:
    found = []
    for pyproject in sorted(EXAMPLES.glob("*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text())
        if "agent" in data.get("tool", {}).get("apx", {}):
            found.append(pyproject.parent)
    return found


def main() -> None:
    rows = []
    for example in _apx_examples():
        try:
            proc = subprocess.run(
                ["uv", "run", "--project", str(example), "--with", WITH_SDK,
                 "apx-agent", "doctor", "--durable", "--offline", "--json"],
                cwd=example, capture_output=True, text=True, timeout=600,
            )
            rows.append(parse_payload(example.name, proc.stdout, proc.stderr))
        except subprocess.TimeoutExpired:
            rows.append(parse_payload(example.name, "", "timed out after 600s"))
    print(build_table(rows))
    sys.exit(0)


if __name__ == "__main__":
    main()
