#!/usr/bin/env python3
"""Live smoke: durable Lakebase checkpointer for served approvals (#329 Slice C).

Proves the one path ``make check`` cannot (no Postgres in CI):
``build_lakebase_checkpointer`` against a REAL Databricks Lakebase instance —
real token minting, the ``connection_class`` token injection, ``.setup()``, and
a pending mid-turn approval surviving a **fresh** ``PostgresSaver`` instance
(genuine cross-process durability, not the InMemorySaver simulation the unit
test uses).

Deterministic by design: a scripted fake model emits the gated tool call so the
thing under test is the checkpointer + Lakebase, not the LLM.

Usage (needs the ``lakebase`` extra and a Databricks profile that owns the
instance)::

    cd python
    uv run --extra lakebase python scripts/lakebase_checkpointer_smoke.py \\
        --profile <profile> \\
        --instance <lakebase-instance-name> \\
        --host ep-xxxx.database.<region>.cloud.databricks.com \\
        --database databricks_postgres

Exits 0 on PASS, non-zero on FAIL with a diagnostic.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid


def _fail(msg: str) -> None:
    print(f"\nFAIL: {msg}")
    sys.exit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", required=True, help="Databricks CLI profile that owns the instance.")
    p.add_argument("--instance", required=True, help="Lakebase database instance NAME (for the credential API).")
    p.add_argument("--host", required=True, help="Lakebase endpoint host (…database.<region>.cloud.databricks.com).")
    p.add_argument("--database", default="databricks_postgres", help="Postgres database name.")
    p.add_argument("--keep", action="store_true", help="Skip cleanup of the smoke thread's checkpoints.")
    args = p.parse_args()

    # The script runs as the profile's own identity (no OBO token to thread).
    os.environ["DATABRICKS_CONFIG_PROFILE"] = args.profile
    os.environ["APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK"] = "true"

    from databricks.sdk import WorkspaceClient
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from mlflow.types.agent import ChatAgentMessage

    from apx_agent import LlmAgent, _compile, chat_agent_for
    from apx_agent._checkpoint_lakebase import build_lakebase_checkpointer
    from apx_agent._policy import FunctionPolicy, PolicyAction, PolicyGate, PolicyResult

    sent: list[str] = []

    def send_email(to: str) -> str:
        """Send an email to the given address."""
        sent.append(to)
        return f"sent to {to}"

    class _ToolFake(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):  # type: ignore[no-untyped-def]
            return self

    gate = PolicyGate([
        FunctionPolicy(
            lambda ev: PolicyResult(action=PolicyAction.ASK, reason="needs human ok"),
            name="ask",
        )
    ])
    agent = LlmAgent(name="smoke", tools=[send_email], before_tool=gate)

    # Scripted: a tool call (which the gate suspends), then a final answer on resume.
    model = _ToolFake(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "send_email", "args": {"to": "x@y.com"}, "id": "t1"}]),
        AIMessage(content="done"),
    ]))
    _compile._build_chat_databricks = (  # type: ignore[attr-defined]
        lambda endpoint, *, temperature=None, max_tokens=None: model
    )

    ws = WorkspaceClient(profile=args.profile)
    principal = ws.current_user.me().user_name
    thread_id = f"apx-smoke-{uuid.uuid4().hex[:8]}"
    print(f"profile={args.profile}  principal={principal}")
    print(f"instance={args.instance}  host={args.host}  db={args.database}")
    print(f"thread_id={thread_id}\n")

    def _build():  # type: ignore[no-untyped-def]
        return build_lakebase_checkpointer(
            ws=ws, instance_name=args.instance, database=args.database, host=args.host
        )

    # 1) Suspend at the gated tool under a durable checkpointer (state → Lakebase).
    print("→ checkpointer #1: run the gated turn …")
    cp1 = _build()
    chat1 = chat_agent_for(agent, model="smoke", checkpointer=cp1)
    r1 = chat1.predict(
        [ChatAgentMessage(role="user", content="email x@y.com", id="u1")],
        custom_inputs={"session_id": thread_id},
    )
    if not (r1.custom_outputs or {}).get("approval_required"):
        _fail("no approval_required on the gated turn")
    if sent:
        _fail(f"tool ran before approval: {sent}")
    print("  ✓ approval surfaced, tool NOT run — paused state written to Lakebase")

    # 2) A FRESH PostgresSaver on the SAME instance resumes the pending approval.
    print("→ checkpointer #2 (fresh instance): resume approve …")
    cp2 = _build()
    chat2 = chat_agent_for(agent, model="smoke", checkpointer=cp2)
    r2 = chat2.predict([], custom_inputs={"session_id": thread_id, "resume": "approve"})
    out = " ".join(m.content for m in r2.messages if m.content)
    if sent != ["x@y.com"]:
        _fail(f"gated tool did not run after restart: sent={sent}")
    if "sent to x@y.com" not in out:
        _fail(f"tool result missing from resumed output: {out!r}")
    print("  ✓ fresh saver read the pending approval FROM Lakebase and ran the tool")

    if not args.keep:
        try:
            cp2.delete_thread(thread_id)
            print("  ✓ cleaned up the smoke thread's checkpoints")
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            print(f"  (cleanup skipped: {exc})")

    print("\nPASS — durable Lakebase checkpointer survives a restart end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
