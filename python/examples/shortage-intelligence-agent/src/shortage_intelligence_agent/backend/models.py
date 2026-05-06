from typing import Any
from pydantic import BaseModel
from .. import __version__


class VersionOut(BaseModel):
    version: str

    @classmethod
    def from_metadata(cls) -> "VersionOut":
        return cls(version=__version__)


class ShortageSignal(BaseModel):
    component_id: str
    component_name: str
    customer_count: int
    earliest_request: str    # ISO datetime
    latest_request: str
    total_units_requested: int
    confidence: str          # HIGH / MEDIUM / LOW


class HistoricalPattern(BaseModel):
    component_id: str
    similar_events_found: int
    avg_price_delta_pct: float    # e.g. 34.2 = 34.2% increase
    max_price_delta_pct: float
    avg_shortage_duration_days: int
    recent_events: list[dict[str, Any]]


class MarketSignal(BaseModel):
    component_id: str
    confirmed: bool
    confidence: str           # HIGH / MEDIUM / LOW
    supporting_sources: list[str]
    summary: str


class VendorListing(BaseModel):
    part_number: str
    manufacturer: str
    vendor: str               # DigiKey, Mouser, Arrow, etc.
    unit_price_usd: float
    quantity_available: int
    lead_time_weeks: int | None
    product_url: str | None


class AlternativePart(BaseModel):
    original_part_number: str
    alt_part_number: str
    alt_manufacturer: str
    spec_match_score: float   # 0.0 - 1.0
    key_differences: str
    in_stock: bool


class SourcingReport(BaseModel):
    generated_at: str
    priority: str             # URGENT / WATCH / MONITOR
    signal_count: int
    signals: list[ShortageSignal]
    vendor_recommendations: list[VendorListing]
    alternative_parts: list[AlternativePart]
    action_items: list[str]


class SalesReport(BaseModel):
    generated_at: str
    escalation_tier: str      # IMMEDIATE / 48HR / MONITOR
    signal_count: int
    at_risk_customers: list[dict[str, Any]]
    price_spike_forecast: str
    proactive_alert_template: str
