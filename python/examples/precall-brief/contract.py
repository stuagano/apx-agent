"""Frozen pre-call brief contract — the single source of truth.

The 7 governed UC views, their exact ordered columns, the brief sections (title
-> view), and the shared company seed set that keys all four synthetic sources.

Code imports this (synthetic-data generator, agent-schema baking); the
deployable ``sql/vw_*.sql`` DDLs restate the same column contract and the AC-1
gate parses them back to prove they still match ``VIEWS`` here. Two
representations, one frozen contract — drift in either is a gate failure.
"""

# View name -> exact ordered column contract. Frozen (AC-1).
VIEWS: dict[str, list[str]] = {
    "vw_opportunities": ["company", "opportunity", "stage", "value", "close_date"],
    "vw_actions": ["company", "action", "due_date", "status"],
    "vw_winloss": ["company", "outcome", "product", "date"],
    "vw_field_notes": ["company", "note", "author", "date"],
    "vw_rmas": ["company", "rma_id", "description", "status", "date"],
    "vw_pprs": ["company", "ppr_id", "description", "severity", "status"],
    "vw_orders": ["company", "order_id", "description", "qty", "expected_ship", "status"],
}

# Which real source each view points at once ingestion lands (docs only; the
# agent binds to view names, never to a source).
SOURCE: dict[str, str] = {
    "vw_opportunities": "your CRM",
    "vw_actions": "your CRM",
    "vw_winloss": "your CRM",
    "vw_field_notes": "your CRM",
    "vw_rmas": "your field-service system",
    "vw_pprs": "your document store",
    "vw_orders": "your ERP",
}

# Brief sections in render order (title -> view). Mirrors precall.toml
# [[precall.section]] blocks; kept here so the offline gates need no TOML parse.
SECTIONS: list[tuple[str, str]] = [
    ("Open Orders & Shipping", "vw_orders"),
    ("Open Opportunities", "vw_opportunities"),
    ("Recent Win / Loss", "vw_winloss"),
    ("Open RMAs", "vw_rmas"),
    ("Open PPRs", "vw_pprs"),
    ("Field Notes", "vw_field_notes"),
    ("Overdue Actions", "vw_actions"),
]

# Shared company seed set — every `company` value in all four synthetic sources
# is drawn from this list so all 7 views join cleanly (AC-2). Generic placeholder
# names only; swap in your own seed set locally. Zero dependency on any real system.
COMPANIES: list[str] = [f"Example Customer {i:02d}" for i in range(1, 9)]
