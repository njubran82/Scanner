"""
market_data/models.py — Data structures for all market data sources.

These models are specific to the market-data layer. They are independent
of the scanner-level models (Opportunity, Book, etc.). The profit_analyzer
acts as the adapter, translating MarketResult → Opportunity fields.

Keeping market data models separate means you can add Amazon, AbeBooks,
or any other source without touching the scanner pipeline.

DATA TYPES:
    MarketListing   — one individual listing from any source
    PriceSummary    — statistical summary across a set of listings
    MarketResult    — complete response for one ISBN query
"""

from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime
from statistics import median, mean


# ── Listing type constants ────────────────────────────────────────────────────
LISTING_ACTIVE = "active"    # Currently for sale
LISTING_SOLD   = "sold"      # Confirmed sold transaction

# ── Source constants ──────────────────────────────────────────────────────────
SOURCE_EBAY_BROWSE   = "ebay_browse"     # eBay Browse API (active listings)
SOURCE_EBAY_INSIGHTS = "ebay_insights"   # eBay Marketplace Insights (sold — restricted)
SOURCE_AMAZON        = "amazon"          # Future Amazon source
SOURCE_ABEBOOKS      = "abebooks"        # Future AbeBooks source

# ── Confidence constants (market-data layer) ──────────────────────────────────
# These are finer-grained than the scanner-level confidence levels.
# profit_analyzer maps these down to HIGH/MEDIUM/LOW/FALLBACK/NONE.
MCONF_SOLD_STRONG   = "sold_strong"    # 3+ actual sold comps
MCONF_SOLD_WEAK     = "sold_weak"      # 1–2 sold comps
MCONF_ACTIVE_STRONG = "active_strong"  # 10+ active listings
MCONF_ACTIVE_MEDIUM = "active_medium"  # 5–9 active listings
MCONF_ACTIVE_WEAK   = "active_weak"    # 1–4 active listings
MCONF_NONE          = "none"           # No data from any source


@dataclass
class MarketListing:
    """
    One listing from any market data source.

    Normalised so callers don't need to know which source provided it.
    """
    item_id:                str
    title:                  str
    price:                  float           # Item price only
    shipping:               float           # Buyer shipping cost (0 = free)
    total_price:            float           # price + shipping
    condition:              str             # Human label: "Very Good", "Good", etc.
    condition_id:           str             # Source-specific code: "2500", "Used", etc.
    listing_type:           str             # LISTING_ACTIVE or LISTING_SOLD
    source:                 str             # SOURCE_* constant
    listing_url:            str
    seller_feedback_score:  Optional[int]   = None
    seller_feedback_pct:    Optional[float] = None  # 0–100
    top_rated_seller:       Optional[bool]  = None
    image_url:              Optional[str]   = None
    sold_date:              Optional[datetime] = None  # Only for LISTING_SOLD
    retrieved_at:           datetime        = field(default_factory=datetime.now)


@dataclass
class PriceSummary:
    """
    Statistical summary of a set of listing prices.

    Built from total_price of each listing (price + shipping),
    which is the actual cost to the buyer and the realistic
    revenue signal for an eBay seller.
    """
    count:    int
    low:      float
    high:     float
    mean:     float
    median:   float
    p25:      float   # 25th percentile (conservative sell estimate)
    p75:      float   # 75th percentile (optimistic sell estimate)
    spread_pct: float  # (high - low) / median * 100 — pricing noise indicator

    @classmethod
    def from_listings(cls, listings: List[MarketListing]) -> Optional["PriceSummary"]:
        """Build a PriceSummary from a list of listings. Returns None if empty."""
        if not listings:
            return None
        prices = sorted(l.total_price for l in listings)
        n      = len(prices)
        med    = median(prices)

        def percentile(data, pct):
            idx = int(len(data) * pct / 100)
            return data[min(idx, len(data) - 1)]

        spread = round((prices[-1] - prices[0]) / med * 100, 1) if med > 0 else 0.0

        return cls(
            count       = n,
            low         = round(prices[0], 2),
            high        = round(prices[-1], 2),
            mean        = round(mean(prices), 2),
            median      = round(med, 2),
            p25         = round(percentile(prices, 25), 2),
            p75         = round(percentile(prices, 75), 2),
            spread_pct  = spread,
        )


@dataclass
class MarketResult:
    """
    Complete market data response for one book (ISBN query).

    Contains all listings found, a price summary, a confidence level,
    and diagnostic information for logging and alerting.

    profit_analyzer consumes this to build Opportunity fields.
    """
    isbn13:         str
    isbn10:         str
    source:         str                  # SOURCE_* constant(s), comma-joined if mixed
    listing_type:   str                  # LISTING_ACTIVE or LISTING_SOLD
    listings:       List[MarketListing]  = field(default_factory=list)
    price_summary:  Optional[PriceSummary] = None
    raw_total:      int                  = 0   # Total results API says exist (not just fetched)
    market_confidence: str               = MCONF_NONE
    retrieved_at:   datetime             = field(default_factory=datetime.now)
    error:          Optional[str]        = None   # Set if the API call failed
    fallback_used:  bool                 = False  # True if ISBN-13 failed and ISBN-10 was used

    def is_usable(self) -> bool:
        """True if this result contains enough data to estimate a sell price."""
        return self.price_summary is not None and self.price_summary.count > 0

    @property
    def best_price_estimate(self) -> Optional[float]:
        """
        Conservative sell price estimate.

        For active listings: median (typical asking price).
        We deliberately do NOT use p25 here — the caller (profit_analyzer)
        applies a discount factor to convert active-listing prices to
        expected sell prices. Using median gives the most stable signal.
        """
        if self.price_summary:
            return self.price_summary.median
        return None
