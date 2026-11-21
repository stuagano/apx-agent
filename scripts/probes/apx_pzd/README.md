# Disposable trace / worker-loss probe

**Danger: a user message containing `pzd-worker-loss` terminates the worker
with `os._exit(86)`.** Never install this on a customer App.

Before that exit the probe ends the open `safe_span("graph.invoke")` and calls
`mlflow.flush_trace_async_logging`, because a hard exit would skip the span's
normal close. It does not call `continue_trace_from_headers`.

Local tests: `python/tests/test_durable_trace_probe.py`. They are not a live
deployment.
