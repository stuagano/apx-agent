---
description: Raise claim-vs-reality coverage one feature at a time, replacing an existence-only check with a real read-back (Reality-test backfill loop).
---

Run the **Reality-test backfill** loop from `docs/loops/README.md`.

Find one CLI command or generated artifact covered only by an `.exists()` /
exit-code check. Add a `*_reality_ctk.py` test that reads the output back and
asserts it is real with `ctk.verify(Artifact(..., min_bytes=..., must_contain=...))`.
Confirm the test passes, then confirm it *fails* when the artifact is emptied —
a test that can't fail proves nothing. Record the feature covered. Stop when no
existence-only feature remains, or the next needs a fixture beyond the kit —
then note it. Don't weaken the assertion to make it pass.
