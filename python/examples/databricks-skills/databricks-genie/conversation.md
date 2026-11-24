# Querying Genie Spaces

Give an apx-agent agent a tool that asks a Genie Space questions through the
Genie Conversation API. Genie writes the SQL using the space's curated
instructions and certified queries, so the agent does not have to.

## When to Use a Genie tool

### Use Genie When:

| Scenario | Why |
|----------|-----|
| The space has curated business logic | Genie knows rules like "active customer = ordered in 90 days" |
| The user asks to use their Genie Space | Their curated space is the source of truth |
| Business metrics with official definitions | Genie has certified queries for them |
| Conversational exploration with follow-ups | Genie keeps the context of earlier questions |

### Use Direct SQL (`sql_tool`) Instead When:

| Scenario | Why |
|----------|-----|
| Simple ad-hoc query, or the SQL is already known | No curation needed |
| No Genie Space covers the data | Genie needs a space |
| You need exact control over the query | Direct SQL runs what you write |

## The tools

| Factory | Returns | Use for |
|---------|---------|---------|
| `genie_tool(space_id)` | Genie's text answer (`str`) | Narrative answers ("which region sold most?") |
| `genie_query_tool(space_id, max_rows=20)` | `{question, sql_results, result_count, genie_response, generated_sql}`, or `{error, question}` | Steps that need the rows or the SQL |
| `genie_one_tool()` | The finished Genie One answer | Workspace-wide Genie, not one space |

```python
from apx_agent import Agent, genie_tool, genie_query_tool

agent = Agent(
    instructions="Answer sales questions with the Sales Genie Space.",
    tools=[
        genie_tool("<space-id>", description="Ask the Sales Genie Space a question."),
        genie_query_tool("<space-id>", name="query_sales_rows", max_rows=50),
    ],
)
```

The model sees one parameter, `question: str`. The workspace client comes from
the incoming request, so Genie runs as the calling user (OBO). Each tool
declares the `genie_space` resource (and the `genie` user scope) for deploy.
Give each tool a distinct `name` and `description` when an
agent uses more than one space.

## Follow-ups

Within one apx session (the `thread_id` / `session_id` the turn runs under),
each tool keeps one Genie conversation per space. Later questions in the
session continue it, so "and last year?" keeps its context. Different sessions
never share a conversation. A turn with no session starts a new conversation
for every question. If the stored conversation is gone, the tool starts a new
one.

Turn follow-ups off per tool with `genie_tool(space_id, follow_ups=False)`.

## Cleanup

apx deletes the conversations it started:

- when the app calls `end_genie_session(session_id)` at the end of a chat;
- when a conversation sits idle for an hour, or is the least recently used once
  more than 512 are tracked in the process.

Deletion is best-effort and never fails a user request. Sweep conversations
left behind by a stopped process:

```bash
apx-agent resources genie-sweep <space-id> --older-than-hours 24 --dry-run   # list only
apx-agent resources genie-sweep <space-id> --older-than-hours 24             # delete
```

```python
from apx_agent import sweep_genie_conversations

stale = sweep_genie_conversations(ws, "<space-id>", older_than_s=86400)                 # dry run
deleted = sweep_genie_conversations(ws, "<space-id>", older_than_s=86400, dry_run=False)
```

The sweep cannot tell apx-started conversations from ones a person started in
the Genie UI, so choose an age past anything still in use. `--all-users` /
`include_all=True` covers every user's conversations and needs CAN MANAGE on
the space.

## Handling results

- `genie_tool` raises a tool error when the Genie API fails, including a
  re-authorize hint when the user's token lacks the scope. A query that does not
  complete returns `"Genie query did not complete (status: ...)"`.
- `genie_query_tool` never raises: check for an `error` key.
- If Genie asks for clarification, the text answer (`genie_response`) holds the
  question. Have the agent rephrase with specifics.

## Testing a new space

After `apx-agent resources genie-space push`, run the agent locally and ask the
space's sample questions, or score it with the benchmarks in its config file.
Check the generated SQL (`genie_query_tool`) as well as the answers.

## Troubleshooting

### "Genie space not found" or permission errors

- Check the `space_id` (the ID recorded under `deployments` in the config file)
- Check the calling user has CAN RUN on the space and access to its tables

### Slow or timed-out answers

- Check the SQL warehouse is running
- Simplify the question, or give the space SQL examples for it

### Wrong answers

- Review `generated_sql` from `genie_query_tool`
- Add instructions, join specs, SQL examples or sample questions to the config
  file and `push` again (see [spaces.md](spaces.md))
