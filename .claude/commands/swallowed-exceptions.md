---
description: Sweep newly changed code for error-hiding except blocks, fixing one real finding per pass (Swallowed-exception sweep loop).
---

Run the **Swallowed-exception sweep** loop from `docs/loops/README.md`.

Run `find_swallowed_exceptions` over the files your change touched (the whole
tree still has justified log-only handlers, so scope to the diff). For the first
finding, decide whether the handler should re-raise, surface the error, or is a
justified no-op; apply the smallest fix. Verify with `make check` and re-run the
scan to confirm that finding is gone without adding others. Stop when the scoped
scan is clean or a finding needs an owner decision — then escalate.
