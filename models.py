"""
models.py — Shared data structures used across the entire scanner.

Using dataclasses keeps data clean, typed, and self-documenting.
All supplier adapters and analysis modules speak this shared language.

BUSINESS MODEL CONTEXT:
    This is a dropshipping system. "cost" on a Book is the per-order
    fulfillment price you pay the supplier after a customer buys —
    not pre-purchased inventory. There is no inbound or outbound
    shipping cost under the current supplier arrangement.

CONFIDENCE LEVELS (on Opportunity):
    HIGH      — 3+ eBay sold comps within lookback window
    MEDIUM    — 1–2 eBay sold comps (low sample, treat with caution)
    LOW       — Active eBay listings found but no sold history
    FALLBACK  — Amazon price estimate used; no eBay data at all
    NONE      — No market data of any kind

CONCERN FLAGS (on Opportunity.concern_flags):
    MISSING_APP_ID    — EBAY_APP_ID not configured; all data is fallback
    API_FAILURE       — eBay API call failed (network or auth error)
    NO_SOLD_COMPS     — Zero sold listings found in lookback window
    FEW_COMPS         — Only 1–2 sold comps; sample size is low
    ACTIVE_ONLY       — Price based on active listings, not sold history
    FALLBACK_PRICING  — Revenue estimated from Amazon price, not eBay
    WIDE_SPREAD       — Sold prices vary widely; revenue estimate uncertain
    LOW_MARGIN        — Margin below 15% (profitable but thin)
    NO_EBAY_DATA      — No sold or active eBay data found at all
"""

from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime


# ── Confidence level constants ────────────────────────────────────────────────
CONFIDENCE_HIGH     = "HIGH"       # 3+ sold comps
CONFIDENCE_MEDIUM   = "MEDIUM"     # 1–2 sold comps
CONFIDENCE_LOW      = "LOW"        # Active listings only
CONFIDENCE_FALLBACK = "FALLBACK"   # Amazon estimate
CONFIDENCE_NONE     = "NONE"       # No data

# ── Concern flag constants ─────────────────────────────────────────────────────
FLAG_MISSING_APP_ID   = "MISSING_APP_ID"
FLAG_API_FAILURE      = "API_FAILURE"
FLAG_NO_SOLD_COMPS    = "NO_SOLD_COMPS"
FLAG_FEW_COMPS        = "FEW_COMPS"
FLAG_ACTIVE_ONLY      = "ACTIVE_ONLY"
FLAG_FALLBACK_PRICING = "FALLBACK_PRICING"
FLAG_WIDE_SPREAD      = "WIDE_SPREAD"
FLAG_LOW_MARGIN       = "LOW_MARGIN"
FLAG_NO_EBAY_DATA     = "NO_EBAY_DATA"

# ── Run mode constants (used in email header) ──────────────────────────────────
RUN_MODE_EBAY_CONFIRMED = "EBAY_CONFIRMED"   # All opps backed by sold data
RUN_MODE_MIXED          = "MIXED"            # Some eBay, some fallback
RUN_MODE_FALLBACK_ONLY  = "FALLBACK_ONLY"    # No eBay data used at all


@dataclass
class Book:
    """
    One book from any supplier source (URL, CSV, API, scraper, etc.)

    All supplier adapters must produce Book objects. This is the
    common language the rest of the system speaks regardless of source.
    """
    title:         str
    isbn13:        str
    isbn10:        str
    cost:          float                  # Dropship fulfillment cost (5-qty tier default)
    amazon_price:  Optional[float]        # Amazon list price (may be None)
    amazon_rank:   Optional[str]          # Amazon bestseller rank (informational)
    cost_10qty:    Optional[float] = None # Alternate tier — stored for reference
    cost_25qty:    Optional[float] = None # Alternate tier — stored for reference
    source:        str = "unknown"        # Which supplier/method produced this record
    fetched_at:    Optional[datetime] = None  # When this record was retrieved


@dataclass
class EbaySoldListing:
    """One completed/sold eBay listing for a book."""
    title:          str
    sold_price:     float
    shipping_price: float     # Shipping charged to buyer (0 if free shipping)
    total_price:    float     # sold_price + shipping_price
    sold_date:      Optional[datetime]
    listing_url:    str = ""


@dataclass
class EbayActiveListing:
    """One currently active eBay listing for a book."""
    title:          str
    current_price:  float
    shipping_price: float
    total_price:    float
    listing_url:    str = ""


@dataclass
class Opportunity:
    """
    The result of analyzing one book.
    Contains all numbers needed to decide whether to list on eBay.
    """
    book: Book

    # eBay sold market data
    ebay_sold_listings:  List[EbaySoldListing]  = field(default_factory=list)
    ebay_sold_count:     int                    = 0
    ebay_sold_median:    Optional[float]        = None
    price_spread_pct:    Optional[float]        = None  # (max-min)/median as %

    # eBay active listing data (reference point)
    ebay_active_listings: List[EbayActiveListing] = field(default_factory=list)
    ebay_active_count:    int                     = 0
    ebay_active_median:   Optional[float]         = None

    # Revenue signal used for profit calc
    revenue_estimate: float = 0.0
    revenue_source:   str   = "none"  # "ebay_sold" | "ebay_active" | "amazon_estimate" | "none"

    # Cost breakdown
    ebay_fee:      float = 0.0
    shipping_cost: float = 0.0        # Always $0 in current dropshipping model
    cogs:          float = 0.0        # Supplier fulfillment cost

    # Profit
    profit:     float = 0.0
    margin_pct: float = 0.0           # profit / revenue_estimate

    # Confidence and concern labeling
    confidence:    str       = CONFIDENCE_NONE
    concern_flags: List[str] = field(default_factory=list)

    # Decision
    is_opportunity: bool = False
    skip_reason:    str  = ""

    @property
    def concern_str(self) -> str:
        """Pipe-separated concern flags for CSV output."""
        return " | ".join(self.concern_flags) if self.concern_flags else ""

    @property
    def summary_line(self) -> str:
        flags = f" ⚠ {self.concern_str}" if self.concern_flags else ""
        return (
            f"{self.book.title[:55]} | "
            f"Cost: ${self.book.cost:.2f} | "
            f"Revenue: ${self.revenue_estimate:.2f} ({self.revenue_source}) | "
            f"Profit: ${self.profit:.2f} ({self.margin_pct*100:.0f}%) | "
            f"[{self.confidence}]{flags}"
        )


@dataclass
class TrackedOpportunity:
    """
    A persisted record of a known opportunity used for state tracking.
    Stored in scanner_state.json between runs to suppress repeat alerts.
    """
    isbn13:           str
    title:            str
    profit:           float
    revenue_estimate: float
    revenue_source:   str
    first_seen:       str    # ISO datetime string
    last_alerted:     str    # ISO datetime string
    alert_count:      int = 1
