"""monthly_summary — scheduled batch job: summarize last month's HubSpot complaints.

Run manually (``uv run monthly-summary [--month YYYY-MM]``) or via the
``hubspot-complaint-summary`` Databricks Job (see ``databricks.yml``), which
schedules this monthly. Writes one row per month to
``<catalog>.<schema>.complaint_summaries``: an exact ticket count from SQL
(never LLM-derived) plus a qualitative theme summary from the agent (via
``run_once``).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import NamedTuple

# Guarantees `from agent import ...` resolves regardless of the runtime's
# cwd/sys.path (Databricks Job compute doesn't guarantee the script's own
# directory is on sys.path the way local `uv run` invocations do).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk import WorkspaceClient

from apx_agent import run_once, run_sql

from agent import CATALOG, SCHEMA, TABLE, agent


class MonthlySummary(NamedTuple):
    """Monthly summary result: count and theme analysis."""
    ticket_count: int
    summary: str


def previous_month(today: dt.date | None = None) -> str:
    """Return the previous full calendar month as 'YYYY-MM-01'."""
    today = today or dt.date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - dt.timedelta(days=1)
    return last_of_prev_month.replace(day=1).isoformat()


def parse_month(value: str) -> str:
    """Validate/normalize a 'YYYY-MM' CLI arg to 'YYYY-MM-01'."""
    parsed = dt.datetime.strptime(value, "%Y-%m")
    return parsed.date().replace(day=1).isoformat()


def summarize_month(ws: WorkspaceClient, month: str) -> MonthlySummary:
    """Return (exact_ticket_count, llm_theme_summary) for `month` ('YYYY-MM-01')."""
    count_rows = run_sql(
        ws,
        f"SELECT COUNT(*) AS n FROM {TABLE} "
        "WHERE CAST(date_trunc('month', hs_createdate) AS DATE) = :month",
        parameters=[{"name": "month", "value": month, "type": "DATE"}],
    )
    ticket_count = int(count_rows[0]["n"]) if count_rows else 0

    summary = run_once(
        agent,
        f"Summarize customer complaints for {month[:7]}. Report the total "
        "count and the recurring themes with counts and example subjects.",
    )
    return MonthlySummary(ticket_count, summary)


def write_summary(ws: WorkspaceClient, month: str, ticket_count: int, summary: str) -> None:
    run_sql(
        ws,
        f"CREATE TABLE IF NOT EXISTS {CATALOG}.{SCHEMA}.complaint_summaries ("
        "month DATE, ticket_count INT, summary STRING, generated_at TIMESTAMP)",
    )
    run_sql(
        ws,
        f"INSERT INTO {CATALOG}.{SCHEMA}.complaint_summaries "
        "VALUES (:month, :ticket_count, :summary, current_timestamp())",
        parameters=[
            {"name": "month", "value": month, "type": "DATE"},
            {"name": "ticket_count", "value": str(ticket_count), "type": "INT"},
            {"name": "summary", "value": summary, "type": "STRING"},
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--month", type=parse_month, default=None,
        help="Month to summarize as YYYY-MM (default: previous full calendar month)",
    )
    args = parser.parse_args()
    month = args.month or previous_month()

    ws = WorkspaceClient()
    ticket_count, summary = summarize_month(ws, month)
    write_summary(ws, month, ticket_count, summary)
    print(f"complaint_summaries: {month[:7]} — {ticket_count} tickets")


if __name__ == "__main__":
    main()
