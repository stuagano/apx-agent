"""mirion-precall — Pre-Call Brief Agent (apx-agent root agent, Databricks Apps).

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

_CATALOG = os.environ.get("APX_CATALOG", "serverless_stable_hvhhmh_catalog")
_SCHEMA = os.environ.get("APX_SCHEMA", "mirion_precall")
_WAREHOUSE_ID = os.environ.get("APX_WAREHOUSE_ID", "0e8908a6bd79447c")

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
        f"{i + 1}. **{title}** — call the `{fn}` tool with the company name"
        for i, (title, fn) in enumerate(_SECTIONS)
    )
    return f"""You are a field pre-call brief writer for Mirion sales reps.

FIRST, before any tool call, emit exactly one short status line so the user sees
immediate progress (the SQL warehouse can take ~10-20s to warm up if idle):
`_Gathering <company>'s data — warming up the warehouse, this takes a few seconds…_`
Then produce a concise 1-2 page markdown brief with EXACTLY these sections in this
order. Populate each by calling its predefined tool with the company name as the
`company` argument:

{lines}

Each tool returns a JSON array of rows for that company. Render each section as a
compact markdown table of those rows, or 'No records.' if the array is empty. Begin
the brief with '# Pre-Call Brief: <company>'. Do not invent data — report only what
the tools return. You may add one short reasoning note per section when it helps
(e.g. flag a Blocked shipment or a Critical PPR), but never fabricate values.
"""


agent = DataAgent(
    _CATALOG,
    _SCHEMA,
    warehouse_id=_WAREHOUSE_ID,
    include_functions=True,  # uc_function_toolkit surfaces the 7 section functions as tools
    instructions=_instructions(),
    name="mirion-precall",
    knowledge="./.apx/okf",
)
