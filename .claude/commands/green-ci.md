---
description: Drive the branch to a fully green local gate, fixing one failure class per pass (Green-CI-before-merge loop).
---

Run the **Green-CI-before-merge** loop from `docs/loops/README.md`.

Run `make check` and `pre-commit run --all-files`. For the first failure,
diagnose the root cause and apply the smallest fix (Ponytail — don't restructure
unrelated code). Re-run both gates and confirm that failure is gone without
introducing another. Repeat for the next failure. Stop when both gates are
green, or a failure needs a decision beyond a local fix — then report it rather
than weakening or skipping the test.
