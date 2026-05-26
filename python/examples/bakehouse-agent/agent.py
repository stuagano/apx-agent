"""bakehouse-agent — a RouterAgent over structured sales + unstructured reviews.

A showcase of three apx-agent ideas working together on Databricks' built-in
``samples.bakehouse`` dataset (a bakery's sales + customer reviews):

  * **DataAgent** — a governed agent over the structured sales tables
    (transactions / customers / franchises / suppliers). With ``ws`` it
    introspects the schema, wires the schema's tools, and grounds its
    instructions in the real columns.
  * **Vector Search** — semantic retrieval over the pre-chunked customer
    reviews (``samples.bakehouse.media_gold_reviews_chunked``).
  * **RouterAgent** — routes "how are sales doing?" to the data agent and
    "what do customers say?" to the reviews agent.

Each leaf runs as the calling user, so Unity Catalog grants apply per request.

Setup (one-time): the reviews path needs a Vector Search index over the chunked
reviews — see README.md. Until it exists, the sales path works on its own; or
swap the reviews agent for the SQL fallback shown below.
"""

from __future__ import annotations

import os

from apx_agent import Agent, DataAgent, RouterAgent, vector_search_tool

WAREHOUSE_ID = os.environ.get("WAREHOUSE_ID") or None
REVIEWS_INDEX = os.environ.get("REVIEWS_INDEX", "main.default.bakehouse_reviews_idx")

# --- Sales: a governed DataAgent over the structured bakehouse tables ---------
# Pass ws=WorkspaceClient() to introspect the schema at startup (auto-wires the
# tables as resources + grounds the instructions). Left lazy here so the example
# starts without a warehouse round-trip; sql_tool discovers a warehouse at call time.
sales_agent = DataAgent(
    "samples", "bakehouse",
    warehouse_id=WAREHOUSE_ID,
    name="sales_agent",
)

# --- Reviews: semantic search over the pre-chunked customer reviews -----------
reviews_agent = Agent(
    name="reviews_agent",
    instructions=(
        "You answer questions about what bakery customers are saying. "
        "Search the customer reviews for relevant passages and summarize the "
        "sentiment and themes, quoting briefly. If nothing relevant is found, "
        "say so rather than guessing."
    ),
    tools=[vector_search_tool(REVIEWS_INDEX, num_results=5)],
)
# SQL fallback (no index needed) — uncomment to query review text directly instead:
# reviews_agent = DataAgent(
#     "samples", "bakehouse", warehouse_id=WAREHOUSE_ID, name="reviews_agent",
#     instructions="Answer questions about customer feedback by querying "
#                  "media_customer_reviews with targeted SELECTs.",
# )

# --- Route between them --------------------------------------------------------
agent = RouterAgent(
    agents=[
        ("sales", "Sales, transactions, revenue, customers, franchises, suppliers", sales_agent),
        ("reviews", "Customer feedback, reviews, sentiment, what people say", reviews_agent),
    ],
    instructions="Route data/metrics questions to sales; opinion/feedback questions to reviews.",
)
