"""Pre-demo smoke test for the deployed chatbot-contracts agent.

Hits the deployed Databricks app, runs each demo question, and asserts:
  1. The response is substantive (>20 chars, no error markers).
  2. The HTTP response is successful.

Usage:
    APP_URL=https://<your-app-url> \
    DATABRICKS_TOKEN=<oauth-token> \
    uv run python scripts/smoke_demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import yaml

BAD_MARKERS = (
    "extraction_unavailable",
    '"error":',
    "Traceback",
    "Internal Server Error",
)


def main() -> int:
    app_url = os.environ.get("APP_URL")
    token = os.environ.get("DATABRICKS_TOKEN")
    if not app_url or not token:
        print("set APP_URL and DATABRICKS_TOKEN", file=sys.stderr)
        return 2
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "agent.config.yaml").read_text())
    questions: list[str] = cfg.get("demo_questions", [])
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    failures: list[str] = []
    with httpx.Client(timeout=120) as cx:
        for q in questions:
            print(f"\n>>> {q}")
            resp = cx.post(
                f"{app_url}/responses",
                headers=headers,
                json={"input": [{"role": "user", "content": q}]},
            )
            ok = resp.status_code == 200
            text = resp.text
            ok = ok and len(text) > 20 and not any(m in text for m in BAD_MARKERS)
            print(text[:400])
            if not ok:
                failures.append(q)

    if failures:
        print(f"\nSMOKE FAILED for {len(failures)}/{len(questions)} questions:", file=sys.stderr)
        for q in failures:
            print(f"  - {q}", file=sys.stderr)
        return 1
    print(f"\nSMOKE OK — {len(questions)}/{len(questions)} questions returned substantive answers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
