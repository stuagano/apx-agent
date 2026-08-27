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

## Checks

```bash
npm run lint
npm run typecheck
npm test
npm run build
```
