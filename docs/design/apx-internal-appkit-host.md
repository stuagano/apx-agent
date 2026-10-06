# Retired: APX AppKit host

The APX TypeScript agent runtime, generated AppKit host, and Python tool bridge
have been removed. APX compiles Python declarations to native DurableAgentServer
for managed Apps, or ResponsesAgent for MLflow model packaging.

See the [compiler guide](../running/runtime-targets.md) and the migrated
[discovery](../../python/examples/plg-discovery) and
[contract](../../python/examples/contract-parsing-agent) examples.

Browser TypeScript remains UI code. Process-local AppKit developer overrides
and thread-deletion routes are gone; changes belong in the agent declaration,
and a new conversation starts a new managed session without deleting history.
