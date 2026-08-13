# Generating the pre-call brief data + tools

How the demo's Unity Catalog objects were generated on a sandbox workspace
(`fevm-hvhhmh`, catalog `serverless_stable_hvhhmh_catalog`). All four sources are
**synthetic** — no real customer names or data are committed; `contract.COMPANIES`
ships as generic placeholders, so swap in your own seed set locally before running.

Run in order (each targets the catalog/schema/warehouse constants at the top of the
file — edit those for your workspace):

1. **`land_uc.py`** — creates the schema, one backing table per view loaded with
   synthetic rows from `synthetic.generate()`, and the 7 governed views. Every row
   is keyed by a company from `contract.COMPANIES` so all 7 sections join cleanly.

2. **`create_functions.py`** — creates the 7 scalar UC functions (one per brief
   section), each `<section>(company STRING) RETURNS STRING` returning that section's
   rows as JSON. The rich `COMMENT` on each function is what the agent reads as the
   tool description (`uc_function_toolkit` surfaces it), so it's the primary semantic
   lever for tool selection — keep it dense and domain-specific.

3. **`gen_okf.py`** — authors the OKF knowledge bundle at `mirion-precall/.apx/okf`:
   7 function cards, 7 enriched view cards (Overview + per-column descriptions +
   golden queries — harvested by the framework's `okf_grounding`), and a dataset-level
   glossary of domain terms + synonyms (harvested by `okf_glossary`). This is the
   grounding lever, distinct from the UC comments.

The contract itself (view names, columns, sections, company seed) lives in
`../contract.py` — the single source of truth all three generators import.

Swapping synthetic → real later: repoint each `../sql/vw_*.sql` (and the tables the
functions read) at real ingested tables. The column contract, functions, OKF, and
agent do not change.
