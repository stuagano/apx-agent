"""samples-hub — routes questions to per-schema sample DataAgent Apps.

Thin orchestrator: no local catalog.schema. Each peer is a deployed App with
its own OKF pack. Wire URLs via ``APX_PEER_*`` env (set at deploy with ``--env``).
"""
from __future__ import annotations

from apx_agent import LlmAgent

# Peer Apps on FEVM BLR (one DataAgent + OKF pack each). Env keys match
# ``_peer_env_key(<app-name>)`` so Topology / Discover wire-back stays coherent.
_PEERS = [
    "$APX_PEER_HELLO_WORLD_URL",  # samples.nyctaxi
    "$APX_PEER_SAMPLES_ACCUWEATHER_URL",
    "$APX_PEER_SAMPLES_BAKEHOUSE_URL",
    "$APX_PEER_SAMPLES_HEALTHVERITY_URL",
    "$APX_PEER_SAMPLES_TPCH_URL",
    "$APX_PEER_SAMPLES_TPCDS_SF1_URL",
    "$APX_PEER_SAMPLES_TPCDS_SF1000_URL",
    "$APX_PEER_SAMPLES_WANDERBRICKS_URL",
]

_INSTRUCTIONS = """\
You are the samples hub for Databricks built-in sample datasets. You have no
local SQL tools — you MUST answer by calling the matching specialist sub-agent
tool (do not invent table names or rows yourself).

Route by domain:
- NYC taxi / trips / fares / yellow cab → hello-world (samples.nyctaxi)
- Weather / AccuWeather / forecasts → samples-accuweather
- Bakery / franchises / sales / customer reviews → samples-bakehouse
- Health / claims / HealthVerity → samples-healthverity
- TPC-H / orders / lineitem / supplier (classic OLAP) → samples-tpch
- TPC-DS scale factor 1 (retail decision support) → samples-tpcds-sf1
- TPC-DS scale factor 1000 (same shape, larger data) → samples-tpcds-sf1000
- Travel / Wanderbricks / amenities / bookings → samples-wanderbricks

If the question spans domains, call one specialist at a time and synthesize.
If no peer matches, say which sample schemas you can reach and ask the user
to pick one. Always cite which specialist you used.
"""

agent = LlmAgent(
    tools=[],
    sub_agents=_PEERS,
    instructions=_INSTRUCTIONS,
    name="samples-hub",
)
