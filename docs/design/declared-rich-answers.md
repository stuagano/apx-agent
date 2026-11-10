# Design: declared rich answers (`output_table_path`, `output_slots_path`)

**Status:** implemented (#1120) · **Date:** 2026-11-10 · **Builds on:**
[composite-session-keys.md](composite-session-keys.md) (`output_message_path`,
`output_replies_path`, `halt_statuses`)

## Intent

A typed step's answer is a single line of text, with optional quick-reply chips.
Everything structured stays out of the conversation:

- a list of records (accounts, matches, line items) only shows as a table in the
  side "Run result" panel and in the collapsed per-step "Output" sections. You
  have to click twice to see it;
- a halted intake turn shows its question but not what the intake already knows.
  The person can't see which slots are filled and which are missing. Dialogflow CX
  shows this for form parameters.

Dialogflow handles this with a small fixed set of rich response types that the
agent author declares (chips, lists, info cards) and the client renders. apx
already has chips (`output_replies_path`). This adds two more types the same way:
a **table** and a **slot summary**.

Success means:

- a step declares which output field is a table, or which is its slot map. The
  answer bubble then shows a table or a slot summary under the message text, with
  no UI code in the agent;
- every client sees the same answer. The rich part is written into the answer
  text as markdown, so Playground, Slack, A2A and Responses callers that read
  only `content` still get a readable table;
- model-produced cell values render as literal text, never as links, images or HTML;
- typed callers that read `apx_data_parts` see no change.

## API

```python
intake = LlmAgent(
    name="intake",
    output_schema=IntakeOut,
    output_key="intake",
    output_status_path=("status",),
    output_message_path=("question",),
    output_replies_path=("suggestions",),
    output_slots_path=("slots",),          # new: {name: value | null}
)
report = LlmAgent(
    name="report",
    output_schema=ReportOut,
    output_key="report",
    output_message_path=("summary",),
    output_table_path=("rows",),           # new: list of flat records
)
SequentialAgent([intake, lookup, report], session_keys=("intake",), halt_statuses=("needs_input",))
```

Both new paths use the same validation as the existing paths (an `output_schema`,
1-8 safe field names). Like `output_replies_path`, each one **requires
`output_message_path`**, because it decorates that answer. Otherwise it is
refused at construction.

## Behaviour

Rendering happens in `_halt_message` (`_compile.py`), the one place that already
builds the declared answer for both halts and the last step. After reading the
message text and the replies, it appends to `content`:

**Table** (`output_table_path`): the value must be a list of 1+ objects whose
values are scalars or null. Columns are the union of keys in first-seen order,
capped at 8. Rows are capped at 50, followed by a blank line and a `… N more rows` note (the
blank line ends the GFM table, so the note doesn't render as a row). Records with
no keys at all add nothing.
It renders as a GitHub markdown table under a blank line. A value of any other
shape adds nothing, matching how a malformed `replies` is ignored today.

**Slot summary** (`output_slots_path`): the value must be an object of 1-16
entries whose values are scalars or null. It renders as a two-column `Slot | Value`
markdown table. A null or empty-string value shows as `—` (missing). The table
goes **above** the question, so the person reads "here's what I have" before "here's
what I need". Any other shape adds nothing.

**Cell escaping** applies to keys and values alike. Each cell is converted with
`str()`, all whitespace runs (newlines and tabs included) collapse to one space,
the text is cut to 80 characters, and every markdown-active character
(``\ ` * _ [ ] ( ) < > | ! # ~ @``) is backslash-escaped.
GFM autolinks get broken too: `://` becomes `\://` and `www.` becomes `www\.`.
Without that, marked turns a bare URL inside an escaped cell into a link; the
real-browser test caught this. Other `:` and `.` stay as they are, so decimals and
times read cleanly in plain-text clients.
That keeps a model-written `![x](https://…)` or `<img>` literal text. It matters
because rendered images or links in an answer are an exfiltration channel.
DOMPurify still runs on the page as a second layer.

**No new wire fields and no new page JS.** Both native chat hosts already render
the answer with `marked`+`DOMPurify` into `.msg.assistant`. `/_apx/chat`
(`_ui_chat.py`) styles `.msg.assistant table`. The root page (`_ui_root_chat.py`)
had no table rules, so it now has equivalent ones under its own `.assistant`
class. On both pages, bubble tables are `display: block; max-width: 100%;
overflow-x: auto`. The old `width: 100%` on `/_apx/chat` is gone, because with
`display: block` it would stretch narrow tables, so every markdown table there now
sizes to its content. `apx_answer`, `apx_replies` and
`apx_data_parts` are unchanged.

## Not in this slice

- Inputs a sub-agent hop received, shown on its timeline card. Only its output is shown today.
- A schema-generated form for typed input instead of the raw JSON textarea.
- Info cards, images, or a structured `apx_rich` payload. Add these if markdown is
  too limiting for a real agent.

## Risks

- **Markdown tables are wide.** Eight columns of 80-character cells overflow a chat
  bubble. The bubble's table CSS needs `overflow-x: auto` on both hosts (checked in
  the browser test).
- **Plain-text clients see pipes.** A client that doesn't render markdown shows the
  raw table. That's still readable, and better than nothing, which is what they get today.

## Testing

- Unit (`test_composite_session_keys.py` style, fake model):
  - a table path renders a header, rows, the 50-row cap and the 8-column cap;
  - a non-list or nested value adds nothing;
  - the slot summary marks null and empty values as `—` and goes above the question;
  - escaping: a cell holding `![a](http://x)`, `<img src=x>`, `a|b` and a newline
    renders literally;
  - a new path without `output_message_path` raises; a new path without `output_schema` raises.
- Real browser (`tests/native_chat_browser.py` + headless Chromium, whose fixture
  already runs each test on both `/_apx/chat` and the root page). The answer content
  comes from the real table and slot renderers, with hostile cells and a 51-row
  overflow. The test asserts two tables, 50 body rows, the note outside the table,
  no `<img>`/`<a>` from a cell, and `overflow-x: auto`. That test (and the unit
  seam) replaced the `examples/` halt-then-table fixture the issue mentioned: the
  halt flow is covered at the compiled-sequence seam, and the browser test covers
  rendering. Assert there's no `pageerror`, a
  `<table>` is visible inside the assistant bubble, and no `<img>` or `<a>` comes
  from a cell.
- `make check` + `pre-commit run --all-files`.

## Delivery

1. One apx PR: both paths, `_halt_message` rendering, bubble table CSS on both hosts,
   docs (`_agents.py` docstring + this doc).
2. Later PRs, only on demand: sub-agent hop inputs, the schema form, richer types.
