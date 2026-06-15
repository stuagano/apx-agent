# Rebuild dev-chat approval banner + history-desync fix — design

**Date:** 2026-06-14 · **Status:** approved · **Scope:** `python/src/apx_agent/_ui_chat.py` (client JS only)

## Problem

The `dc99975b` dev-chat rewrite ("SQL-first tool event display") rebuilt
`_ui_chat.py` (288 lines) and, as collateral, dropped two things its commit
message never mentioned:

1. The **ASK-policy approval UI** — `checkPendingApprovals()` / `resolveApproval()`
   and the `/_apx/approvals` polling. The backend is fully intact (`_dev.py`
   serves `GET /_apx/approvals`, `POST .../{id}/approve|deny`; `PolicyGate` in
   `_policy.py` manages the approvals), so the dev chat now has a working
   approval *backend* with no UI to drive it.
2. The **request-history reset** (`history.length = 0`) on conversation switch —
   so switching/creating a conversation leaks stale turns into the next request
   payload.

`tests/test_dev_approvals.py::test_chat_template_includes_approval_and_history_js`
caught both (it asserts `checkPendingApprovals`, `resolveApproval`,
`/_apx/approvals`, and `history.length = 0` ship in the rendered page) and
currently fails on `main`.

## Decision

Restore both, client-side only, into the new `_ui_chat.py`. Approval prompts
render **inline in the chat stream** (where the user is already looking and
acting), not in the read-only SQL-first Events panel. ASK policy is
non-blocking/retry-based, so turn-end is the correct polling trigger.

## Components (all in `_ui_chat.py`)

1. **`checkPendingApprovals()`** — `GET /_apx/approvals`. For each pending
   approval not already on screen (dedupe by `[data-approval-id]`), append an
   `.approval-card` into `chat`: `⏸ Approval required: <tool_name>`, optional
   reason, args JSON truncated to 300 chars (all `esc()`-escaped), and
   **Approve / Deny** buttons. Fails silent on network error / non-OK response
   (a dev UI without a policy gate must not error).

2. **`resolveApproval(id, approved, btn)`** — `POST /_apx/approvals/{id}/approve`
   or `.../deny`. On success: remove the card and auto-submit a follow-up turn
   ("I approved request {id}. Please retry the action." / "I denied request
   {id}. Do not retry that action.") so the agent closes the loop without the
   user retyping. On failure: re-enable the card's buttons and surface the error
   via `addMsg`.

3. **Wiring** — call `checkPendingApprovals()`:
   - at turn completion (after the assistant message is pushed, ~L2484), and
   - once on initial page load (so a pending approval is visible if the UI is
     reopened — a small improvement over the prior turn-end-only behavior).

4. **History-desync fix** — add `history.length = 0;` to `newConversation()`
   (~L1324) and `switchConversation()` (~L1349). The request `history` is a
   `const` array, so reset by truncation, not reassignment.

## Styling

Reuse the prior self-contained inline card styles (amber "pending" border, dark
theme). No new CSS classes or dependencies.

## Testing

`test_dev_approvals.py::test_chat_template_includes_approval_and_history_js`
already encodes the contract and currently fails — it goes green on restore. The
other 10 tests in that file cover the backend approve/deny/list API. No new
tests required; the regression test is the gate.

## Out of scope

The approval backend, `PolicyGate`, and the new SQL-first events UI. No behavior
change to any of them.
