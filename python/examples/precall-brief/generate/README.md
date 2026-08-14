# Generating the pre-call brief data + tools

How the example's Unity Catalog objects are generated on a sandbox workspace.
All sources are **synthetic** — no real customer names or data are committed;
`contract.COMPANIES` ships as generic placeholders, so swap in your own seed set
locally before running.

Run in order (each accepts `--catalog`, `--schema`, `--warehouse-id`, `--profile`
command-line args to configure the target workspace):

1. **`land_uc.py`** — creates the schema, one backing table per view loaded with
   synthetic rows from `synthetic.generate()`, and the 7 governed views. Every row
   is keyed by a company from `contract.COMPANIES` so all 7 sections join cleanly.

   ```bash
   python land_uc.py --profile=<profile> --catalog=main --schema=precall --warehouse-id=<id>
   ```

2. **`create_functions.py`** — creates the 7 scalar UC functions (one per brief
   section), each `<section>(company STRING) RETURNS STRING` returning that section's
   rows as JSON. The rich `COMMENT` on each function is what the agent reads as the
   tool description, so it's the primary semantic lever for tool selection — keep
   it dense and domain-specific.

   ```bash
   python create_functions.py --profile=<profile> --catalog=main --schema=precall --warehouse-id=<id>
   ```

3. **`gen_okf.py`** — authors the OKF knowledge bundle at `../.apx/okf`:
   7 function cards, 7 enriched view cards (Overview + per-column descriptions +
   golden queries), and a dataset-level glossary of domain terms + synonyms. This
   is the grounding lever, distinct from the UC comments.

   ```bash
   python gen_okf.py --catalog=main --schema=precall
   ```

The contract itself (view names, columns, sections, company seed) lives in
`../contract.py` — the single source of truth all three generators import.

Swapping synthetic → real later: repoint each `../sql/vw_*.sql` (and the tables the
functions read) at real ingested tables. The column contract, functions, OKF, and
agent do not change.
