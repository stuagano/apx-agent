"""Author a semantically-strong OKF bundle for mirion-precall.

Writes: (1) functions/ cards for the 7 section UC functions (Overview, Parameters,
Returns, Examples, Synonyms), (2) enriched tables/ view cards (Overview + per-column
Description + Examples golden queries that call the functions), (3) a dataset-level
Glossary of domain terms + synonyms (harvested by okf_glossary), and refreshes the
indexes. Idempotent — overwrites the target cards.
"""
from __future__ import annotations
import os
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from pathlib import Path

OKF = Path(os.path.join(_ROOT, "mirion-precall/.apx/okf")
CATALOG, SCHEMA = "serverless_stable_hvhhmh_catalog", "mirion_precall"
TS = "2026-08-13T21:00:00+00:00"

# Per-section spec. Everything here is domain-specific (Mirion = radiation detection
# & measurement for nuclear power, medical, and national labs) so grounding is dense.
SPECS = {
    "open_orders_and_shipping": {
        "title": "Open Orders & Shipping", "view": "vw_orders", "source": "SAP",
        "overview": (
            "In-flight sales orders and shipment status for one customer, sourced from SAP. "
            "The pre-call signal here is fulfillment risk: what hardware the customer is "
            "waiting on and whether any shipment is Blocked or slipping its expected ship "
            "date. A blocked detector or dosimeter order is a conversation the rep must "
            "lead with."),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "order_id": "SAP sales-order number (e.g. ORDER-7988).",
            "description": "Ordered product — detector, dosimeter, survey meter, spectrometer, probe, or camera.",
            "qty": "Units ordered.",
            "expected_ship": "Committed ship date (ISO). Compare to today to spot slips.",
            "status": "Fulfillment status: Open, In Progress, Blocked, or Closed. 'Blocked' is the escalation signal.",
        },
        "synonyms": ["orders", "open orders", "shipments", "shipping status", "backlog", "SAP orders", "fulfillment"],
        "questions": ["What does this customer have on order?", "Are any shipments blocked or delayed?"],
    },
    "open_opportunities": {
        "title": "Open Opportunities", "view": "vw_opportunities", "source": "Salesforce",
        "overview": (
            "Active sales pipeline for one customer from Salesforce — the open deals, their "
            "stage, dollar value, and close date. Use it to walk into the call knowing deal "
            "size and where each opportunity sits, especially anything in Proposal or "
            "Negotiation with a near-term close."),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "opportunity": "Deal name, usually a product-line fleet upgrade.",
            "stage": "Sales stage: Discovery, Qualification, Proposal, Negotiation, Closed Won.",
            "value": "Deal value in USD.",
            "close_date": "Expected close date (ISO).",
        },
        "synonyms": ["opportunities", "pipeline", "open deals", "deals", "sales pipeline", "opps"],
        "questions": ["What's in the pipeline for this customer?", "Which deals are close to closing?"],
    },
    "recent_win_loss": {
        "title": "Recent Win / Loss", "view": "vw_winloss", "source": "Salesforce",
        "overview": (
            "Recently won and lost deals for one customer from Salesforce. Recent wins show "
            "relationship momentum and which product lines are landing; recent losses are a "
            "competitive flag the rep should understand before the call."),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "outcome": "Won or Lost.",
            "product": "Product line involved in the deal.",
            "date": "Date the deal closed (ISO).",
        },
        "synonyms": ["wins", "losses", "win loss", "closed deals", "recent deals", "competitive losses"],
        "questions": ["What has this customer bought recently?", "Have we lost any deals here?"],
    },
    "open_rmas": {
        "title": "Open RMAs", "view": "vw_rmas", "source": "ServiceMax",
        "overview": (
            "Open RMAs (Return Merchandise Authorizations) for one customer from ServiceMax "
            "— returned or failed instruments and their repair/replacement status. Open or "
            "Blocked RMAs are active service pain the rep must be ready to speak to; a "
            "returned radiation instrument is high-stakes for a regulated customer."),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "rma_id": "ServiceMax RMA number.",
            "description": "The affected unit / instrument being returned or repaired.",
            "status": "Open, In Progress, Blocked, or Closed.",
            "date": "RMA open date (ISO).",
        },
        "synonyms": ["RMA", "RMAs", "returns", "return authorization", "repairs", "service returns", "failed units"],
        "questions": ["Any open returns or repairs for this customer?", "What service issues are outstanding?"],
    },
    "open_pprs": {
        "title": "Open PPRs", "view": "vw_pprs", "source": "SharePoint",
        "overview": (
            "Open PPRs (Product Problem Reports) for one customer from SharePoint — quality "
            "issues on Mirion product, analogous to a CAPA (Corrective and Preventive "
            "Action). Severity and status matter most: a Critical, Blocked PPR on a "
            "radiation-detection instrument is the single most important thing to surface "
            "before a call with a regulated customer."),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "ppr_id": "Product Problem Report id.",
            "description": "The quality issue and affected unit.",
            "severity": "Low, Medium, High, or Critical. Critical is the escalation signal.",
            "status": "Open, In Progress, Blocked, or Closed.",
        },
        "synonyms": ["PPR", "PPRs", "product problem report", "quality issue", "CAPA", "corrective action", "defect"],
        "questions": ["Any open quality issues for this customer?", "Are there critical product problems to flag?"],
    },
    "field_notes": {
        "title": "Field Notes", "view": "vw_field_notes", "source": "Salesforce",
        "overview": (
            "Free-text field and supply-chain handoff notes for one customer from "
            "Salesforce — the human context: site visits, expressed product interest, "
            "relationship and logistics notes from the account team. Read these for signal "
            "the structured tables miss, including upsell interest not yet in the pipeline."),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "note": "Free-text note from a site visit or supply-chain handoff.",
            "author": "Who wrote the note.",
            "date": "Note date (ISO).",
        },
        "synonyms": ["field notes", "notes", "site visit notes", "handoff notes", "account notes", "supply chain notes"],
        "questions": ["What are the latest field notes on this customer?", "What interest has the customer expressed?"],
    },
    "overdue_actions": {
        "title": "Overdue Actions", "view": "vw_actions", "source": "Salesforce",
        "overview": (
            "Committed follow-up actions for one customer from Salesforce — quotes to send, "
            "calls to make — with due date and status. Anything past its due date is a "
            "dropped ball the rep should close out on the call so nothing the account team "
            "promised slips."),
        "cols": {
            "company": "Customer / account name. Join key across every section.",
            "action": "The committed follow-up.",
            "due_date": "When it was due (ISO). Past-due = overdue.",
            "status": "Open, In Progress, Blocked, or Closed.",
        },
        "synonyms": ["actions", "overdue actions", "follow-ups", "tasks", "next steps", "commitments", "to-do"],
        "questions": ["What follow-ups are overdue for this customer?", "What did we commit to this customer?"],
    },
}


def fm(d: dict) -> str:
    import yaml
    return "---\n" + yaml.safe_dump(d, sort_keys=False).strip() + "\n---\n\n"


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# 1) functions/ cards
fdir = OKF / "functions"
for fn, s in SPECS.items():
    params = "# Parameters\n- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.\n\n"
    returns = "# Returns\nA JSON array (STRING) of matching rows; `[]` when the customer has none.\n\n"
    ex = "# Examples\n"
    for q in s["questions"]:
        ex += f"### {q}\n```sql\nSELECT {CATALOG}.{SCHEMA}.{fn}('<company>')\n```\n- @company (STRING): the customer name\n\n"
    syn = "# Synonyms\n" + ", ".join(s["synonyms"]) + "\n"
    body = f"# Overview\n{s['overview']}\n\n{params}{returns}{ex}{syn}"
    front = fm({
        "type": "Unity Catalog Function", "title": fn,
        "description": f"{s['title']} for a Mirion customer (source: {s['source']}). Scalar function; returns rows as a JSON array.",
        "resource": f"{CATALOG}.{SCHEMA}.{fn}", "timestamp": TS,
    })
    write(fdir / f"{fn}.md", front + body)
write(fdir / "index.md", "# Functions\n" + "".join(f"* [{fn}]({fn}.md)\n" for fn in SPECS))

# 2) enriched tables/ view cards (harvested by okf_grounding)
tdir = OKF / "tables"
for fn, s in SPECS.items():
    view = s["view"]
    schema_rows = "".join(f"| `{c}` | string | {desc} |\n" if c not in ("qty", "value")
                          else f"| `{c}` | bigint | {desc} |\n" for c, desc in s["cols"].items())
    body = (
        f"# Overview\n{s['overview']}\n\n"
        f"# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n{schema_rows}\n"
        f"# Joins\nEvery view in this schema joins to the others on `company`; one company's "
        f"7 sections together form its pre-call brief.\n\n"
        f"# Examples\n### {s['questions'][0]}\n```sql\nSELECT * FROM {CATALOG}.{SCHEMA}.{view} WHERE company = '<company>'\n```\n"
        f"- @company (STRING): the customer name\n\n"
        f"Prefer the governed function `{CATALOG}.{SCHEMA}.{fn}('<company>')`, which wraps this view.\n"
    )
    front = fm({
        "type": "Unity Catalog View", "title": view,
        "description": f"{s['title']} for a Mirion customer (source: {s['source']}). Backs the `{fn}` function.",
        "resource": f"{CATALOG}.{SCHEMA}.{view}", "timestamp": TS,
    })
    write(tdir / f"{view}.md", front + body)

# 3) dataset card: keep Tables list, add Functions list + Glossary (harvested by okf_glossary)
views = [s["view"] for s in SPECS.values()]
glossary_terms = {
    "Pre-Call Brief": ("A one-page summary a Mirion rep reads before a customer visit, assembled from 7 sections (orders, opportunities, win/loss, RMAs, PPRs, field notes, overdue actions).", ["call brief", "pre-visit brief", "briefing"]),
    "RMA": ("Return Merchandise Authorization — an approved return or repair of a Mirion instrument, tracked in ServiceMax.", ["return", "return authorization"]),
    "PPR": ("Product Problem Report — a logged product-quality issue, similar to a CAPA. Severity ranges Low to Critical.", ["product problem report", "CAPA", "quality issue"]),
    "Dosimeter": ("A wearable radiation-dose measurement instrument. One of Mirion's core product lines.", ["dose meter", "dosimetry"]),
    "Survey meter": ("A handheld instrument that measures radiation levels in the field.", ["survey instrument"]),
}
gl = "# Glossary\n"
for term, (defn, syns) in glossary_terms.items():
    gl += f"### {term}\n{defn} Synonyms: {', '.join(syns)}.\n\n"
ds_body = (
    "# Tables\n" + "".join(f"* [{v}](../tables/{v}.md)\n" for v in views) + "\n"
    "# Functions\n" + "".join(f"* [{fn}](../functions/{fn}.md)\n" for fn in SPECS) + "\n"
    + gl
)
ds_front = fm({
    "type": "Databricks Schema", "title": SCHEMA,
    "description": "Mirion pre-call brief data: 7 governed views + 7 section functions, keyed by company, for building a rep's pre-call brief.",
    "resource": f"{CATALOG}.{SCHEMA}", "catalog": CATALOG, "schema": SCHEMA, "timestamp": TS,
})
write(OKF / "datasets" / f"{SCHEMA}.md", ds_front + ds_body)

# 4) top index includes functions/
write(OKF / "index.md",
      '---\nokf_version: "0.1"\n---\n\n# Subdirectories\n* [datasets](datasets/)\n* [tables](tables/)\n* [functions](functions/)\n')

print("OKF authored: 7 function cards, 7 enriched view cards, glossary, indexes.")
