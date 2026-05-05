"""Eval dataset for the data-triage agent.

Run:
    DATABRICKS_CONFIG_PROFILE=<your-profile> python eval/eval_dataset.py

Uses MLflow GenAI evaluate against the deployed agent's /responses endpoint.
"""

import os

import mlflow

mlflow.set_tracking_uri("databricks")

from mlflow.genai.scorers import (
    Guidelines,
    RelevanceToQuery,
)

APP_URL = os.environ.get(
    "APP_URL",
    "http://localhost:8000",
)

# ---------------------------------------------------------------------------
# Predict function — calls /responses on the deployed agent
# ---------------------------------------------------------------------------


def make_predict(url: str, token: str):
    """Return an MLflow-compatible predict function.

    Uses streaming to avoid proxy timeouts on long pipeline runs.
    """
    import json
    import urllib.request

    def predict(question: str) -> str:
        body = json.dumps({"input": question, "stream": True}).encode()
        req = urllib.request.Request(
            f"{url.rstrip('/')}/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        full_text = ""
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data: "):
                    try:
                        d = json.loads(line[6:])
                        if isinstance(d, dict):
                            text = d.get("text", "")
                            if text:
                                full_text += text
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass
        return full_text

    return predict


# ---------------------------------------------------------------------------
# Eval dataset — 12 cases across all agent paths
# ---------------------------------------------------------------------------

CATALOG = os.environ.get("EVAL_CATALOG", "<your-catalog>")
SCHEMA = os.environ.get("EVAL_SCHEMA", "<your-schema>")
TABLE = f"{CATALOG}.{SCHEMA}.customers"

eval_data = [
    # --- General: sub-agent calls ---
    {
        "inputs": {"question": f"Show me the schema for {TABLE}"},
        "expectations": {
            "expected_facts": ["customer_id", "name", "rate_plan_id"],
            "guidelines": "Should call tools and return real column names from the table.",
        },
    },
    {
        "inputs": {"question": f"How many customers are in {TABLE}?"},
        "expectations": {
            "expected_facts": ["10"],
            "guidelines": "Should return the exact row count of 10.",
        },
    },
    {
        "inputs": {"question": f"Look up customer CUST-0003 in {TABLE}"},
        "expectations": {
            "expected_facts": ["Priya Patel", "Green"],
            "guidelines": "Should return Priya Patel on the Green Energy Plan.",
        },
    },
    {
        "inputs": {"question": f"Show the version history for {TABLE}"},
        "expectations": {
            "expected_facts": ["CREATE", "WRITE"],
            "guidelines": "Should show Delta version history with CREATE and WRITE operations.",
        },
    },
    # --- General: local tool calls ---
    {
        "inputs": {"question": f"What upstream sources feed into {TABLE}?"},
        "expectations": {
            "expected_facts": ["lineage"],
            "guidelines": "Should call get_table_lineage and identify upstream sources.",
        },
    },
    {
        "inputs": {"question": f"What jobs write to {CATALOG}.{SCHEMA}.billing_history?"},
        "expectations": {
            "expected_facts": ["job"],
            "guidelines": "Should call find_jobs_for_table and return writer information.",
        },
    },
    # --- Investigation pipeline ---
    {
        "inputs": {"question": f"CUST-0011 is missing from {TABLE}. Investigate why."},
        "expectations": {
            "expected_facts": ["MISSING", "10"],
            "guidelines": "Should run the investigation pipeline. Verdict: DATA MISSING. Table has 10 customers, CUST-0011 was never ingested.",
        },
    },
    {
        "inputs": {"question": f"The data in {CATALOG}.{SCHEMA}.meter_readings seems stale. When was it last updated?"},
        "expectations": {
            "expected_facts": ["version", "update"],
            "guidelines": "Should check table freshness via table info or version history.",
        },
    },
    # --- Edge cases ---
    {
        "inputs": {"question": "Hello, what can you help me with?"},
        "expectations": {
            "expected_facts": ["table", "pipeline"],
            "guidelines": "Should describe capabilities without calling any tools.",
        },
    },
    {
        "inputs": {"question": f"Does CUST-0005 exist in {TABLE}?"},
        "expectations": {
            "expected_facts": ["CUST-0005", "Sarah Johnson"],
            "guidelines": "Should confirm CUST-0005 exists and return details.",
        },
    },
]


# ---------------------------------------------------------------------------
# Run evaluation
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from databricks.sdk import WorkspaceClient

    ws = WorkspaceClient()
    token = ws.config.authenticate().get("Authorization", "").replace("Bearer ", "")

    experiment_name = "/Shared/data-triage-agent-eval"
    mlflow.set_experiment(experiment_name)

    predict_fn = make_predict(APP_URL, token)

    print(f"Evaluating against: {APP_URL}")
    print(f"Experiment: {experiment_name}")
    print(f"Cases: {len(eval_data)}")
    print()

    # Build scorers
    scorers = [
        RelevanceToQuery(),
        Guidelines(
            name="fact_check",
            guidelines="The response should contain all expected facts from the expectations.",
        ),
    ]

    results = mlflow.genai.evaluate(
        data=eval_data,
        predict_fn=predict_fn,
        scorers=scorers,
    )

    print("\n=== Results ===")
    print(f"Metrics: {results.metrics}")

    # Also do a simple fact check locally
    print("\n=== Fact Check ===")
    client = mlflow.tracking.MlflowClient()
    traces = client.search_traces(
        experiment_ids=[mlflow.get_experiment_by_name(experiment_name).experiment_id],
        max_results=len(eval_data),
        order_by=["timestamp_ms DESC"],
    )

    import json as _json

    expectations = {d["inputs"]["question"][:40]: d["expectations"]["expected_facts"] for d in eval_data}

    passed = failed = errors = 0
    for t in reversed(traces):
        req = _json.loads(t.data.request) if isinstance(t.data.request, str) else t.data.request
        question = req.get("question", "")
        status = t.info.status.name

        if status == "ERROR":
            print(f"  ❌ ERROR  {question[:60]}")
            errors += 1
            continue

        resp = str(t.data.response).lower()
        matched = next((k for k in expectations if k.lower() in question.lower()), None)
        if matched:
            facts = expectations[matched]
            missing = [f for f in facts if f.lower() not in resp]
            if missing:
                print(f"  ⚠️  PARTIAL {question[:60]}  missing={missing}")
                failed += 1
            else:
                print(f"  ✅ PASS    {question[:60]}")
                passed += 1
        else:
            print(f"  ✅ PASS    {question[:60]}  (no facts to check)")
            passed += 1

    print(f"\n{passed} passed, {failed} partial, {errors} errors / {len(traces)} total")
