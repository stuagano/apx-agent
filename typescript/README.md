# apx-internal-runtime

Internal TypeScript runtime machinery for APX-generated Databricks hosts.

This package is private. It is not a standalone SDK and should not be
published, documented, or installed as a user-facing authoring surface. APX
users author agents through the Python declaration layer; generated Apps or
serving hosts may consume this runtime as implementation detail.

## Maintainer Notes

- `src/index.ts` is consumed by generated host code, not by end users.
- `src/internal/appkit-host.ts` contains the AppKit-backed Apps host adapter.
- `apx scaffold --target apps` wires generated TypeScript projects to this
  package with `"apx-internal-runtime": "file:.."`.
- Keep public APX guidance in the root README and Python docs.
- The retained runner in `src/agent/runner.ts` sends normal and streaming
  chat to `/ai-gateway/mlflow/v1/chat/completions`, preserving the authored
  model-service name. Chat uses service credentials (PAT or M2M OAuth);
  caller OBO credentials remain scoped to tool execution.
- This compatibility runner is not another native compiler target. Its Gateway
  transport does not establish parity with the AppKit adapter or Python's
  durable execution, managed state, deployment, or recovery contracts. Explicit
  Model Serving connector tools retain their existing transport.

## Checks

```bash
npm run lint
npm run typecheck
npm test
npm run build
```
