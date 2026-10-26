# Cost tracking

apx-agent estimates dollar cost from token usage. It does not query billing
tables; the estimate is for the session budget guard and the dev UI's trace
panel.

`apx_agent._cost.usd_for_token_usage(input_tokens, output_tokens, model=...)`
prices an input/output token count with published per-million-token rates for known
foundation models (for example `databricks-claude-sonnet-4-6`). It returns
`None` for a model with no published rate, so an unknown model shows no price
instead of a wrong one.

Two surfaces use it:

- the session budget guard, which caps running dollar cost per session from the
  same usage;
- the dev UI trace panel, which shows the estimated cost of each trace.
