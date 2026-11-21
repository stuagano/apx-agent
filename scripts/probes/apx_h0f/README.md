# Disposable session/invocation worker-loss probe

**Danger: a user message containing `h0f-worker-loss` terminates the worker
with `os._exit(86)` on the first attempt.** Never install this on a customer
App. It is an opt-in harness for bead `apx-h0f`, not a production host.

It uses native `DurableAgentServer`, APX `LlmAgent` + `compile_durable_handlers`
(`recovery=True`), a managed `DatabricksSessionStoreSaver`, and a deterministic
model. Service identity only: `[auth.user] required = false`. Request-user
recovery is not enabled and must not be simulated by swapping credentials.

The dated run record lives under `docs/evidence/`. Local tests in
`python/tests/test_durable_session_probe.py` check the crash boundary and the
committed artifact. They are not a live deployment.
