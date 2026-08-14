"""Author a semantically-strong OKF bundle for pre-call brief.

Writes: (1) functions/ cards for the 7 section UC functions (Overview, Parameters,
Returns, Examples, Synonyms), (2) enriched tables/ view cards (Overview + per-column
Description + Examples golden queries that call the functions), (3) a dataset-level
Glossary of domain terms + synonyms, and refreshes the indexes. Idempotent — overwrites the target cards.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Fix sys.path: parent is examples/precall-brief
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

OKF = Path(_ROOT) / ".apx" / "okf"
TS = datetime.now(timezone.utc).isoformat()

# Per-section spec. Domain-agnostic so grounding is portable.
SPECS = {
    "open_orders_and_shipping": {
        "title": "Open Orders & Shipping",
        "view": "vw_orders",
        "source": "ERP",
        "overview": (
            "In-flight sales orders and shipment status for one customer from your ERP. "
            "The pre-call signal here is fulfillment risk: what products the customer is "
            "waiting on and whether any shipment is Blocked or slipping its expected ship "
            "date."
        ),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "order_id": "Order number (e.g. ORDER-7988).",
            "description": "Ordered product line.",
            "qty": "Units ordered.",
            "expected_ship": "Committed ship date (ISO). Compare to today to spot slips.",
            "status": "Fulfillment status: Open, In Progress, Blocked, or Closed. 'Blocked' is the escalation signal.",
        },
        "synonyms": [
            "orders",
            "open orders",
            "shipments",
            "shipping status",
            "backlog",
            "fulfillment",
        ],
        "questions": [
            "What does this customer have on order?",
            "Are any shipments blocked or delayed?",
        ],
    },
    "open_opportunities": {
        "title": "Open Opportunities",
        "view": "vw_opportunities",
        "source": "CRM",
        "overview": (
            "Active sales pipeline for one customer from your CRM — the open deals, their "
            "stage, dollar value, and close date. Use it to walk into the call knowing deal "
            "size and where each opportunity sits, especially anything in Proposal or "
            "Negotiation with a near-term close."
        ),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "opportunity": "Deal name, usually a product-line upgrade.",
            "stage": "Sales stage: Discovery, Qualification, Proposal, Negotiation, Closed Won.",
            "value": "Deal value in USD.",
            "close_date": "Expected close date (ISO).",
        },
        "synonyms": [
            "opportunities",
            "pipeline",
            "open deals",
            "deals",
            "sales pipeline",
            "opps",
        ],
        "questions": [
            "What's in the pipeline for this customer?",
            "Which deals are close to closing?",
        ],
    },
    "recent_win_loss": {
        "title": "Recent Win / Loss",
        "view": "vw_winloss",
        "source": "CRM",
        "overview": (
            "Recently won and lost deals for one customer from your CRM. Recent wins show "
            "relationship momentum and which product lines are landing; recent losses are a "
            "competitive flag the rep should understand before the call."
        ),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "outcome": "Won or Lost.",
            "product": "Product line involved in the deal.",
            "date": "Date the deal closed (ISO).",
        },
        "synonyms": [
            "wins",
            "losses",
            "win loss",
            "closed deals",
            "recent deals",
            "competitive losses",
        ],
        "questions": [
            "What has this customer bought recently?",
            "Have we lost any deals here?",
        ],
    },
    "open_rmas": {
        "title": "Open RMAs",
        "view": "vw_rmas",
        "source": "Field Service System",
        "overview": (
            "Open RMAs (Return Merchandise Authorizations) for one customer from your field-service system "
            "— returned or failed products and their repair/replacement status. Open or "
            "Blocked RMAs are active service issues the rep must be ready to speak to."
        ),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "rma_id": "RMA number.",
            "description": "The affected unit / product being returned or repaired.",
            "status": "Open, In Progress, Blocked, or Closed.",
            "date": "RMA open date (ISO).",
        },
        "synonyms": [
            "RMA",
            "RMAs",
            "returns",
            "return authorization",
            "repairs",
            "service returns",
            "failed units",
        ],
        "questions": [
            "Any open returns or repairs for this customer?",
            "What service issues are outstanding?",
        ],
    },
    "open_pprs": {
        "title": "Open PPRs",
        "view": "vw_pprs",
        "source": "Document Store",
        "overview": (
            "Open PPRs (Product Problem Reports) for one customer from your document store — quality "
            "issues on your products, analogous to a CAPA (Corrective and Preventive "
            "Action). Severity and status matter most: a Critical, Blocked PPR is the single "
            "most important thing to surface before a call."
        ),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "ppr_id": "Product Problem Report id.",
            "description": "The quality issue and affected unit.",
            "severity": "Low, Medium, High, or Critical. Critical is the escalation signal.",
            "status": "Open, In Progress, Blocked, or Closed.",
        },
        "synonyms": [
            "PPR",
            "PPRs",
            "product problem report",
            "quality issue",
            "CAPA",
            "corrective action",
            "defect",
        ],
        "questions": [
            "Any open quality issues for this customer?",
            "Are there critical product problems to flag?",
        ],
    },
    "field_notes": {
        "title": "Field Notes",
        "view": "vw_field_notes",
        "source": "CRM",
        "overview": (
            "Free-text field and supply-chain handoff notes for one customer from "
            "your CRM — the human context: site visits, expressed product interest, "
            "relationship and logistics notes from the account team. Read these for signal "
            "the structured tables miss, including upsell interest not yet in the pipeline."
        ),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "note": "Free-text note from a site visit or supply-chain handoff.",
            "author": "Who wrote the note.",
            "date": "Note date (ISO).",
        },
        "synonyms": [
            "field notes",
            "notes",
            "site visit notes",
            "handoff notes",
            "account notes",
            "supply chain notes",
        ],
        "questions": [
            "What are the latest field notes on this customer?",
            "What interest has the customer expressed?",
        ],
    },
    "overdue_actions": {
        "title": "Overdue Actions",
        "view": "vw_actions",
        "source": "CRM",
        "overview": (
            "Committed follow-up actions for one customer from your CRM — quotes to send, "
            "calls to make — with due date and status. Anything past its due date is a "
            "dropped ball the rep should close out on the call so nothing the account team "
            "promised slips."
        ),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "action": "The committed follow-up.",
            "due_date": "When it was due (ISO). Past-due = overdue.",
            "status": "Open, In Progress, Blocked, or Closed.",
        },
        "synonyms": [
            "actions",
            "overdue actions",
            "follow-ups",
            "tasks",
            "next steps",
            "commitments",
            "to-do",
        ],
        "questions": [
            "What follow-ups are overdue for this customer?",
            "What did we commit to this customer?",
        ],
    },
}


def fm(d: dict) -> str:
    import yaml

    return "---\n" + yaml.safe_dump(d, sort_keys=False).strip() + "\n---\n\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Author OKF knowledge bundle for pre-call brief"
    )
    parser.add_argument(
        "--catalog",
        default="main",
        help="Catalog name (default: main)",
    )
    parser.add_argument(
        "--schema",
        default="precall",
        help="Schema name (default: precall)",
    )
    args = parser.parse_args()

    # 1) functions/ cards
    fdir = OKF / "functions"
    for fn, s in SPECS.items():
        params = "# Parameters\n- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.\n\n"
        returns = "# Returns\nA JSON array (STRING) of matching rows; `[]` when the customer has none.\n\n"
        ex = "# Examples\n"
        for q in s["questions"]:
            ex += f"### {q}\n```sql\nSELECT {args.catalog}.{args.schema}.{fn}('<company>')\n```\n- @company (STRING): the customer name\n\n"
        syn = "# Synonyms\n" + ", ".join(s["synonyms"]) + "\n"
        body = f"# Overview\n{s['overview']}\n\n{params}{returns}{ex}{syn}"
        front = fm(
            {
                "type": "Unity Catalog Function",
                "title": fn,
                "description": f"{s['title']} for a customer (source: {s['source']}). Scalar function; returns rows as a JSON array.",
                "resource": f"{args.catalog}.{args.schema}.{fn}",
                "timestamp": TS,
            }
        )
        write(fdir / f"{fn}.md", front + body)
    write(fdir / "index.md", "# Functions\n" + "".join(f"* [{fn}]({fn}.md)\n" for fn in SPECS))

    # 2) enriched tables/ view cards
    tdir = OKF / "tables"
    for fn, s in SPECS.items():
        view = s["view"]
        schema_rows = "".join(
            f"| `{c}` | string | {desc} |\n"
            if c not in ("qty", "value")
            else f"| `{c}` | bigint | {desc} |\n"
            for c, desc in s["cols"].items()
        )
        body = (
            f"# Overview\n{s['overview']}\n\n"
            f"# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n{schema_rows}\n"
            f"# Joins\nEvery view in this schema joins to the others on `company`; one company's "
            f"7 sections together form its pre-call brief.\n\n"
            f"# Examples\n### {s['questions'][0]}\n```sql\nSELECT * FROM {args.catalog}.{args.schema}.{view} WHERE company = '<company>'\n```\n"
            f"- @company (STRING): the customer name\n\n"
            f"Prefer the governed function `{args.catalog}.{args.schema}.{fn}('<company>')`, which wraps this view.\n"
        )
        front = fm(
            {
                "type": "Unity Catalog View",
                "title": view,
                "description": f"{s['title']} for a customer (source: {s['source']}). Backs the `{fn}` function.",
                "resource": f"{args.catalog}.{args.schema}.{view}",
                "timestamp": TS,
            }
        )
        write(tdir / f"{view}.md", front + body)

    # 3) dataset card
    views = [s["view"] for s in SPECS.values()]
    glossary_terms = {
        "Pre-Call Brief": (
            "A one-page summary a rep reads before a customer visit, assembled from 7 sections (orders, opportunities, win/loss, RMAs, PPRs, field notes, overdue actions).",
            ["call brief", "pre-visit brief", "briefing"],
        ),
        "RMA": (
            "Return Merchandise Authorization — an approved return or repair, tracked in your field-service system.",
            ["return", "return authorization"],
        ),
        "PPR": (
            "Product Problem Report — a logged product-quality issue, similar to a CAPA. Severity ranges Low to Critical.",
            ["product problem report", "CAPA", "quality issue"],
        ),
    }
    gl = "# Glossary\n"
    for term, (defn, syns) in glossary_terms.items():
        gl += f"### {term}\n{defn} Synonyms: {', '.join(syns)}.\n\n"

    ds_body = (
        "# Tables\n"
        + "".join(f"* [{v}](../tables/{v}.md)\n" for v in views)
        + "\n"
        + "# Functions\n"
        + "".join(f"* [{fn}](../functions/{fn}.md)\n" for fn in SPECS)
        + "\n"
        + gl
    )
    ds_front = fm(
        {
            "type": "Databricks Schema",
            "title": args.schema,
            "description": "Pre-call brief data: 7 governed views + 7 section functions, keyed by company, for building a rep's pre-call brief.",
            "resource": f"{args.catalog}.{args.schema}",
            "catalog": args.catalog,
            "schema": args.schema,
            "timestamp": TS,
        }
    )
    write(OKF / "datasets" / f"{args.schema}.md", ds_front + ds_body)

    # 4) top index
    write(
        OKF / "index.md",
        '---\nokf_version: "0.1"\n---\n\n# Subdirectories\n* [datasets](datasets/)\n* [tables](tables/)\n* [functions](functions/)\n',
    )

    print("OKF authored: 7 function cards, 7 enriched view cards, glossary, indexes.")


if __name__ == "__main__":
    main()
