"""Real-MLflow regression guard for trace search (claim-vs-reality, via ctk).

`mlflow.search_traces` has no `experiment_names` parameter in mlflow 3.x — it
raises `TypeError` — and `experiment_ids` is deprecated in favour of
`locations`. The canary soak (`_canary`, `_canary_apps`), eval-chain coverage
(`_eval_chain`), and trace export (`_trace_export`) all read traces through
`search_traces_for_experiment`, and every existing test for those paths *mocks*
`mlflow.search_traces` — so a wrong kwarg sails straight through the suite while
failing in production (the bug this test was written to catch).

This test calls the helper against a **real** mlflow (local file backend, no
network) so the actual `search_traces` signature is exercised: it logs one real
trace into an experiment, then asserts the helper reads it back. A regression to
`experiment_names=` (or any dropped/renamed param) raises `TypeError` here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctk import claim_vs_reality, expect


# Unmarked (not @pytest.mark.unit): this exercises a REAL mlflow against a local
# file backend — the opposite of the "mocked boundaries" the `unit` marker means.
# Matches the unmarked real-mlflow tests in test_trace_store.py.
def test_search_traces_for_experiment_reads_a_real_trace(tmp_path: Path) -> None:
    import mlflow

    from apx_agent._mlflow_tracing import search_traces_for_experiment

    # Real MLflow tracing against a LOCAL file backend (never network/blob).
    mlflow.set_tracking_uri(f"file://{tmp_path}")
    exp_name = f"apx-search-{tmp_path.name}"
    exp_id = mlflow.create_experiment(exp_name)
    mlflow.set_experiment(experiment_id=exp_id)

    # Log exactly one real trace into this experiment, then flush the async
    # trace-logging queue so the search below sees it (logging is async by
    # default — without the flush the trace may not be persisted yet).
    with mlflow.start_span(name="probe") as span:
        span.set_inputs({"q": "hello"})
        span.set_outputs({"a": "world"})
    mlflow.flush_trace_async_logging()

    # claim-vs-reality: the helper claims it reads an experiment's traces. The
    # only honest verification is the real API returning the trace we just
    # logged — a TypeError from a bad kwarg trips this, a mock never would.
    def _reads_the_trace() -> None:
        df = search_traces_for_experiment(exp_name, max_results=10)
        expect(len(df)).satisfies(lambda n: n >= 1, "trace just logged is returned").verify()

    claim_vs_reality(claimed_success=True, verifier=_reads_the_trace, claim_label="search_traces_for_experiment")

    # Exercise the exact canary-path kwargs (filter_string + include_spans=False)
    # so a signature drift on those also fails here, not just in a canary soak.
    df2 = search_traces_for_experiment(
        exp_name, filter_string="timestamp_ms > 0", max_results=10, include_spans=False
    )
    expect(len(df2)).satisfies(lambda n: n >= 1, "metadata-only read returns the trace").verify()


def test_search_traces_unknown_experiment_surfaces_error(tmp_path: Path) -> None:
    """A missing experiment must raise — not return an empty result that the
    canary/eval paths would mis-read as "no traces / no errors observed"."""
    import mlflow

    from apx_agent._mlflow_tracing import search_traces_for_experiment

    mlflow.set_tracking_uri(f"file://{tmp_path}")
    with pytest.raises(ValueError, match="not found"):
        search_traces_for_experiment("definitely-not-a-real-experiment", max_results=1)
