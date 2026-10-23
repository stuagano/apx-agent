# APX handoff receipt

I have read the complete source handoff at
`/Users/stuart.gano/orca/workspaces/Curinos/causal-native-workflow/.superpowers/sdd/2026-10-23-snapshot-rollout/APX-HANDOFF.md`,
`/Users/stuart.gano/AGENTS.md`, and this checkout's `AGENTS.md`.
No additional Python subtree AGENTS.md was found.

This session owns all implementation and validation of the local APX authoritative-output
completion fix in `authoritative-tool-completion`, branch
`stuartg-dev/authoritative-tool-completion`. The verified, intentional exact-deployed
base is **0541b1c3d57a5a4d345056aab2b85e9f78732bd7**. Do not rebase onto main.

Scope: eliminate the redundant model turn after the declared `output_tool` executes,
preserving exactly-one-successful-call enforcement, exact tool-result/schema validation,
guardrails, artifact metadata/digests, state writes, callbacks, honest model usage,
native completion events, and approval/interruption semantics. Ordinary agents remain unchanged.

Authorization is local implementation, offline regression tests, and independent review only.
No push, merge, publication, deployment, remote calls, permission changes, timeout increase,
installed-wheel edits, or destructive cleanup. Retain local work and this checkout.

Preserve the entire Curinos `causal-native-workflow` checkout, including uncommitted work,
evidence, wheels, stages, and the reviewed interpreter/dependencies. Preserve unrelated
APX worktrees `apx-effective-config` and `shrimp`; no implementation belongs there.

Current evidence: the new compiled-graph regression failed as expected in sync, async,
and streaming modes (two model calls versus the required one). A minimal local candidate
now marks the output tool for direct return and constructs a fresh deterministic terminal
message. Narrow verification is running; this is not yet a completion claim.

Next action: inspect the narrow test result, verify edge cases and contract safeguards,
run APX and isolated Curinos regression checks, and obtain independent defect-focused
review. Report the local diff and red/green evidence with any remaining gaps.

Context receipt and implementation ownership are confirmed. The sending session can end;
all further implementation remains in this checkout.

Completion evidence is recorded in [LOCAL-VALIDATION.md](LOCAL-VALIDATION.md).
