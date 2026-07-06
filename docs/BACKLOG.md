# Backlog

Speculative, low-priority, or "someday" items that don't warrant a live
GitHub issue right now — usually because they're suspected-not-confirmed,
gated on profiling evidence, or blocked on an external dependency reaching
GA. When one of these becomes actionable (reproduced under load, a real
customer hits it, the blocking dependency ships), promote it to a GitHub
issue and remove the entry here.

## Performance / infrastructure

- **Per-request `WorkspaceClient` never explicitly closed** (was #494).
  Every `predict`/`predict_stream` call builds a fresh `WorkspaceClient`,
  which holds a `requests.Session` (`databricks-sdk`'s `_base_client.py`).
  Confirmed via direct SDK source inspection: there is no `close()` method
  anywhere in the `WorkspaceClient` → `ApiClient` → `_BaseClient` chain, so
  there's no clean way to release the session per-request today — it's
  garbage-collected eventually, not leaked forever. The only available
  fixes are worse than the problem: reaching into a private `._session`
  three layers deep (fragile, breaks on SDK upgrades), or a token-keyed
  client cache (caches credentials in memory — a real security tradeoff for
  a performance concern). Revisit only if profiling under real load shows
  this actually matters; not a correctness bug today.
