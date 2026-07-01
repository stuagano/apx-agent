#!/usr/bin/env python3
"""Live smoke: the Lakebase STORE paths against a real instance (#329).

Exercises the SQLAlchemy ``build_lakebase_engine`` path — which the checkpointer
smoke does NOT touch (that uses a separate psycopg builder) — through all three
stores that use it:

  1. LakebaseConversationStore  (session; no embeddings)  — create/append/read-back
  2. LakebaseMemoryStore        (memory; pgvector)         — add/recall (semantic)
  3. LakebaseExampleStore       (example; pgvector)        — add/find_similar (semantic)

Proves the engine connects (OAuth token, principal, SSL), auto-creates tables,
and each store round-trips real writes/reads. Uses unique per-run tables and
drops them at the end (unless --keep).

Usage::

    cd python
    uv run --extra lakebase python scripts/lakebase_stores_smoke.py \\
        --profile <profile> \\
        --host ep-xxxx.database.<region>.cloud.databricks.com \\
        --database databricks_postgres

Exits 0 on PASS, non-zero on FAIL.
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
    p.add_argument("--profile", required=True)
    p.add_argument("--host", required=True, help="Lakebase endpoint host.")
    p.add_argument("--database", default="databricks_postgres")
    p.add_argument("--embedding-model", default="databricks-gte-large-en")
    p.add_argument("--embedding-dim", type=int, default=1024)
    p.add_argument("--keep", action="store_true", help="Skip dropping the smoke tables.")
    args = p.parse_args()

    os.environ["DATABRICKS_CONFIG_PROFILE"] = args.profile
    os.environ["APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK"] = "true"

    from databricks.sdk import WorkspaceClient
    from sqlalchemy import text

    from apx_agent._conversation import MessageData, NewConversationItem
    from apx_agent._conversation_lakebase import LakebaseConversationStore
    from apx_agent._embeddings import make_embedding_fn
    from apx_agent._example import FindSimilarOptions
    from apx_agent._example_lakebase import LakebaseExampleStore
    from apx_agent._lakebase_engine import build_lakebase_engine
    from apx_agent._memory import RecallOptions
    from apx_agent._memory_lakebase import LakebaseMemoryStore

    ws = WorkspaceClient(profile=args.profile)
    print(f"profile={args.profile}  principal={ws.current_user.me().user_name}")
    print(f"host={args.host}  db={args.database}\n")

    engine = build_lakebase_engine(ws=ws, database=args.database, host=args.host)
    uid = uuid.uuid4().hex[:8]
    tables: list[str] = []

    # 1) ConversationStore — no embeddings.
    print("→ ConversationStore: create → append → read back …")
    conv_t, item_t = f"apx_smoke_{uid}_conv", f"apx_smoke_{uid}_items"
    tables += [conv_t, item_t]
    conv_store = LakebaseConversationStore(
        engine=engine, conversations_table=conv_t, items_table=item_t, auto_create=True
    )
    conv = conv_store.create_conversation(title="smoke", agent_id="smoke")
    conv_store.append(conv.id, [
        NewConversationItem(
            type="message", response_id="r1",
            data=MessageData(role="user", content=[{"type": "input_text", "text": "remember me"}]),
        )
    ])
    items = conv_store.list_items(conv.id, order="asc", limit=10).data
    if not any("remember me" in str(it.data) for it in items):
        _fail(f"conversation round-trip lost the item: {[str(it.data) for it in items]}")
    print("  ✓ conversation item written to Lakebase and read back")

    # 2) MemoryStore — pgvector semantic recall.
    print("→ MemoryStore: add → recall (semantic) …")
    mem_t = f"apx_smoke_{uid}_mem"
    tables.append(mem_t)
    embed = make_embedding_fn(ws, args.embedding_model)
    mem_store = LakebaseMemoryStore(
        engine=engine, embedding_fn=embed, embedding_dim=args.embedding_dim,
        table_name=mem_t, auto_create=True, ensure_extension=True,
    )
    mem_store.add({"principal_id": "smoke", "content": "the sky is a deep shade of blue"})
    hits = mem_store.recall(RecallOptions(principal_id="smoke", query="what colour is the sky", k=1))
    if not hits or "sky" not in hits[0].memory.content:
        _fail(f"memory recall did not return the added memory: {hits}")
    print(f"  ✓ memory recalled semantically: {hits[0].memory.content!r}")

    # 3) ExampleStore — pgvector semantic find.
    print("→ ExampleStore: add → find_similar (semantic) …")
    ex_t = f"apx_smoke_{uid}_ex"
    tables.append(ex_t)
    ex_store = LakebaseExampleStore(
        engine=engine, embedding_fn=embed, embedding_dim=args.embedding_dim,
        table_name=ex_t, auto_create=True, ensure_extension=True,
    )
    ex_store.add({"agent_id": "smoke", "input": "reset my password", "output": "Open Settings → Security."})
    found = ex_store.find_similar(FindSimilarOptions(agent_id="smoke", query="I forgot my password"))
    if not found or "Settings" not in found[0].example.output:
        _fail(f"example find_similar did not return the added example: {found}")
    print(f"  ✓ example found semantically: {found[0].example.input!r}")

    # Cleanup.
    if not args.keep:
        with engine.begin() as conn:
            for t in tables:
                conn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))
        print(f"  ✓ dropped {len(tables)} smoke tables")

    print("\nPASS — Lakebase engine + all three stores round-trip against a real instance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
