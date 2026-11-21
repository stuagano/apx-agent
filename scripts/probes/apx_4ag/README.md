# Disposable disconnect / worker-loss probe

**Danger: a user message containing `4ag-worker-loss` terminates the worker
with `os._exit(86)`.** Never install this on a customer App.

`4ag-hold` sleeps and returns. Closing the SSE client is not cancellation.
This harness does not register `CancellationRegistry` and does not add a
cancel route. The legacy in-process kill switch stays in
`apx_agent._cancellation`.

Local tests: `python/tests/test_durable_disconnect_probe.py`. They are not a
live deployment.
