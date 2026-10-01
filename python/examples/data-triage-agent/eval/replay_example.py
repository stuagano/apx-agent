"""Small typed triage example: real models, synthetic recorded tool results.

This is a fixture-only demonstration, not the deployed six-step triage agent.
Run from the repository's python directory with an explicitly chosen profile:
  DATABRICKS_CONFIG_PROFILE=<profile> uv run --frozen python \
    examples/data-triage-agent/eval/replay_example.py --model <endpoint>
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from apx_agent import Agent, SequentialAgent, evaluate_chain


class Customer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str


class Presence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str
    exists: bool


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str
    status: Literal["found", "missing"]


def lookup_customer(customer_id: str) -> dict:
    """Check whether this customer exists in the source table."""
    raise RuntimeError("This example requires recorded tool fixtures")


def build_agent() -> SequentialAgent:
    return SequentialAgent([
        Agent(name="identify", output_schema=Customer, output_key="customer",
              instructions="Extract the customer_id from the request. Return only JSON."),
        Agent(name="lookup", output_schema=Presence, output_key="presence",
              tools=[lookup_customer],
              instructions="Customer: {customer}. Call lookup_customer exactly once with the customer_id. Return its result as JSON."),
        Agent(name="verdict", output_schema=Verdict,
              instructions="Presence: {presence}. Return customer_id and status: found when exists is true, otherwise missing. Only JSON."),
    ], name="triage", on_failure="escalate")


def load_fixtures() -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "chain-eval.json"
    return json.loads(path.read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Model endpoint; calls are real and may incur cost")
    args = parser.parse_args()
    report = evaluate_chain(build_agent(), model=args.model, fixtures=load_fixtures())
    print(json.dumps(asdict(report), indent=2))
    return int(report.outcome_counts.get("wrong", 0) > 0)


if __name__ == "__main__":
    raise SystemExit(main())
