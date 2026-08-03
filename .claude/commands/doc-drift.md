---
description: Keep docs honest by verifying each command/flag a doc claims against the code, fixing one drift per pass (Doc-claims-vs-code drift loop).
---

Run the **Doc-claims-vs-code drift** loop from `docs/loops/README.md`.

Pick a doc page that claims a command, flag, or behaviour (e.g. an
`apx-agent …` invocation). For one claim, verify it against the code —
`apx-agent --help`, the CLI source, or a quick run. If it drifted, make the
smallest correction to the doc (or the code, if the doc is the intended
contract) and confirm the corrected claim now holds by running it. Record the
page and claim. Stop when the page's claims all hold, or a drift needs a product
decision — then flag it. Never edit a doc to match without checking the code
first.
