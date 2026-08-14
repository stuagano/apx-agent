"""Pre-call brief agent (apx-agent root agent, Databricks Apps).

Given a company name, produces a 1-2 page pre-call brief with 7 fixed sections.
Each section is backed by a governed, predefined Unity Catalog function
(``<section>(company)``) that the agent calls as a tool — the function's UC
COMMENT is the tool description, and the data team owns the query. Functions run
as the calling user (OBO), so Unity Catalog enforces their grants.

Reusable: a second customer is a copy of this project with a different
catalog/schema and its own set of section functions. Overridable per environment
via APX_CATALOG / APX_SCHEMA / APX_WAREHOUSE_ID.
"""
from __future__ import annotations

import os

from apx_agent import DataAgent

_CATALOG = os.environ.get("APX_CATALOG", "main")
_SCHEMA = os.environ.get("APX_SCHEMA", "precall")
_WAREHOUSE_ID = os.environ.get("APX_WAREHOUSE_ID", "")

# Brief sections in render order (title -> UC function tool name). Each function
# takes one arg (company) and returns that section's rows as a JSON array.
_SECTIONS: list[tuple[str, str]] = [
    ("Open Orders & Shipping", "open_orders_and_shipping"),
    ("Open Opportunities", "open_opportunities"),
    ("Recent Win / Loss", "recent_win_loss"),
    ("Open RMAs", "open_rmas"),
    ("Open PPRs", "open_pprs"),
    ("Field Notes", "field_notes"),
    ("Overdue Actions", "overdue_actions"),
]


def _instructions() -> str:
    lines = "\n".join(
        f"{i + 1}. **{title}** — run `SELECT {_CATALOG}.{_SCHEMA}.{fn}('<company>')`"
        for i, (title, fn) in enumerate(_SECTIONS)
    )
    return f"""You are a field pre-call brief writer preparing one-page briefings for sales reps.

FIRST, before any query, emit exactly one short status line so the user sees
immediate progress (the SQL warehouse can take ~10-20s to warm up if idle):
`_Gathering <company>'s data — warming up the warehouse, this takes a few seconds…_`
Then produce a concise 1-2 page markdown brief with EXACTLY these sections in this
order. Populate each by calling its predefined governed function via SQL (one query
per section), substituting the company name:

{lines}

Each function returns a JSON array of rows for that company. Parse it and render each
section as a compact markdown table, or 'No records.' if the array is empty. Begin the
brief with '# Pre-Call Brief: <company>'. Do not invent data — report only what the
functions return. You may add one short reasoning note per section when it helps
(e.g. flag a Blocked shipment or a Critical severity issue), but never fabricate values.
"""


# include_functions=False on purpose: the agent invokes the governed functions via
# the built-in SQL tool (SELECT ...fn(company)), which needs only the `sql` OBO scope.
# uc_function_toolkit would introspect params via the UC metadata API, requiring a
# `unity-catalog` OBO scope this Apps runtime does not offer.
agent = DataAgent(
    _CATALOG,
    _SCHEMA,
    warehouse_id=_WAREHOUSE_ID if _WAREHOUSE_ID else None,
    include_functions=False,
    instructions=_instructions(),
    name="precall-brief",
    knowledge="./.apx/okf",
)
