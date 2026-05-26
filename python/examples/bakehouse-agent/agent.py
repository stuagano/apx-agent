"""bakehouse-agent — a RouterAgent over a bakery's sales + customer reviews.

A showcase of DataAgent + multi-agent routing on Databricks' built-in
``samples.bakehouse`` dataset:

  * **sales_agent** — a ``DataAgent`` for sales metrics (transactions,
    customers, franchises, suppliers).
  * **reviews_agent** — a ``DataAgent`` for customer feedback, querying the
    review text.
  * **RouterAgent** — routes "how are sales?" to sales, "what do customers
    say?" to reviews.

Zero setup: both leaves use SQL, so on a workspace with serverless SQL the
agent runs immediately (``sql_tool`` auto-discovers a warehouse) — no Vector
Search endpoint, no index, no idle cost. Each leaf runs as the calling user,
so Unity Catalog grants apply per request.

Upgrade — semantic review search: for production-grade retrieval over reviews,
swap ``reviews_agent`` for a Vector Search version (one-time index, see
README.md → "Upgrade"). The structured/sales path is unchanged.
"""

from __future__ import annotations

import os

from apx_agent import DataAgent, RouterAgent

WAREHOUSE_ID = os.environ.get("WAREHOUSE_ID") or None

# --- Sales: metrics over the structured tables --------------------------------
sales_agent = DataAgent(
    "samples", "bakehouse",
    warehouse_id=WAREHOUSE_ID,
    name="sales_agent",
    instructions=(
        "You answer questions about the bakery's sales. Always use FULLY-QUALIFIED "
        "table names: samples.bakehouse.sales_transactions, "
        "samples.bakehouse.sales_customers, samples.bakehouse.sales_franchises, "
        "samples.bakehouse.sales_suppliers. Aggregate with GROUP BY, join on the id "
        "columns, and base every answer on the SQL tool's results."
    ),
)

# --- Reviews: customer feedback over the review text --------------------------
reviews_agent = DataAgent(
    "samples", "bakehouse",
    warehouse_id=WAREHOUSE_ID,
    name="reviews_agent",
    instructions=(
        "You answer questions about what customers say. Query the fully-qualified "
        "table samples.bakehouse.media_customer_reviews with the SQL tool — filter "
        "the review text with WHERE ... LIKE, read the rows, and summarize sentiment "
        "and themes, quoting briefly. Say so if nothing relevant is found."
    ),
)
# Upgrade — semantic search over reviews (needs a one-time Vector Search index):
#   from apx_agent import Agent, vector_search_tool
#   reviews_agent = Agent(
#       name="reviews_agent",
#       instructions="Answer questions about what customers say using the review "
#                    "search tool; summarize sentiment and themes, quoting briefly.",
#       tools=[vector_search_tool(os.environ["REVIEWS_INDEX"], num_results=5)],
#   )

# --- Route between them --------------------------------------------------------
agent = RouterAgent(
    agents=[
        ("sales", "Sales, transactions, revenue, customers, franchises, suppliers", sales_agent),
        ("reviews", "Customer feedback, reviews, sentiment, what people say", reviews_agent),
    ],
    instructions="Route data/metrics questions to sales; opinion/feedback questions to reviews.",
)
