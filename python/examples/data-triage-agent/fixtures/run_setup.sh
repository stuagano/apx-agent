#!/usr/bin/env bash
# Splits setup.sql on ; boundaries and runs each statement individually.
set -euo pipefail
PROFILE="${PROFILE:-fe-stable}"
WAREHOUSE="${WAREHOUSE:-76cf70399b8d0ef0}"
SQL_FILE="${1:-$(dirname "$0")/setup.sql}"

# Strip comments and split on ;
python3 <<PY
import json, re, subprocess, sys

sql_text = open("$SQL_FILE").read()
# strip line comments
clean = "\n".join(line for line in sql_text.splitlines() if not line.strip().startswith("--"))
stmts = [s.strip() for s in clean.split(";") if s.strip()]

print(f"Running {len(stmts)} statements on $PROFILE / warehouse $WAREHOUSE", file=sys.stderr)
for i, stmt in enumerate(stmts, 1):
    body = json.dumps({"warehouse_id": "$WAREHOUSE", "statement": stmt, "wait_timeout": "50s"})
    open("/tmp/_s.json", "w").write(body)
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements", "--profile", "$PROFILE", "--json", "@/tmp/_s.json"],
        capture_output=True, text=True
    )
    resp = json.loads(r.stdout)
    state = resp.get("status", {}).get("state", "?")
    print(f"  [{i}/{len(stmts)}] {state}: {stmt[:80].replace(chr(10),' ')}...", file=sys.stderr)
    if state == "FAILED":
        print(f"    error: {resp.get('status',{}).get('error',{}).get('message','')[:300]}", file=sys.stderr)
        sys.exit(1)
    if i == len(stmts) and resp.get("result", {}).get("data_array"):
        print("Verify:", file=sys.stderr)
        for row in resp["result"]["data_array"]:
            print(f"  {row}", file=sys.stderr)
PY
