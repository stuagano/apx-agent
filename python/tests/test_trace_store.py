"""In-process trace ring buffer (``apx_agent._trace_store``).

The dev-UI Trace detail (``/_apx/traces/{id}``) hangs on FEVM/private-link
because ``mlflow.get_trace`` falls through to downloading span artifacts from
blocked blob storage. The ring buffer snapshots each trace right after the run
(while it is still in MLflow's in-memory buffer — no blob round-trip) so the
route can serve recent traces from memory instead.
"""

from __future__ import annotations


def test_trace_store_put_get_and_bound():
    from apx_agent import _trace_store as ts
    ts.reset()
    ts.put("tr-1", [{"name": "a"}])
    assert ts.get("tr-1") == [{"name": "a"}]
    assert ts.get("nope") is None
    for i in range(ts.MAX_TRACES + 10):
        ts.put(f"x-{i}", [{"name": str(i)}])
    assert len(ts._STORE) <= ts.MAX_TRACES          # bounded (oldest evicted)
    assert ts.get("tr-1") is None                    # tr-1 evicted
