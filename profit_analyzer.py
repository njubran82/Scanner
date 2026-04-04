"""
profit_analyzer.py — Scores each book for profitability using the market_data layer.

PIPELINE PER BOOK:
    1. Fetch market data (market_data.get_market_data → MarketResult)
    2. Determine revenue estimate:
         a) eBay active median × discount factor   (Browse API data — directional)
         b) Amazon price × amazon-to-ebay discount (supplier sheet fallback)
         c) None → skip the book entirely
    3. Calculate profit via fee_calculator.py
    4. Assign scanner confidence level (maps from market confidence)
    5. Assign concern flags
    6. Apply profit + margin threshold gates

REVENUE ESTIMATE FROM ACTIVE LISTINGS:
    The Browse API gives active listing prices, not sold prices. An active
    listing median is typically 10–20% higher than what things actually sell
    for, because:
        - Not all listed books sell
        - Sellers often price optimistically
        - The lowest-priced copies sell first, raising the remaining median

    We apply config.EBAY_ACTIVE_PRICE_DISCOUNT (default 0.10 = 10%) to
    convert the active median to an expected sell price.

    This means revenue_estimate = ebay_active_median × (1 - discount).

    Effect: A book with active median $100 becomes $90 revenue estimate.
    This is conservative and intentional — better to under-estimate profit
    than to over-estimate it for an arbitrage decision.

CONFIDENCE MAPPING (market_data → scanner):
    MCONF_SOLD_STRONG   → HIGH       (3+ sold comps — not currently achievable)
    MCONF_SOLD_WEAK     → MEDIUM     (1–2 sold comps)
    MCONF_ACTIVE_STRONG → MEDIUM     (10+ active listings — directional, not sold)
    MCONF_ACTIVE_MEDIUM → LOW        (5–9 active)
    MCONF_ACTIVE_WEAK   → LOW        (1–4 active)
    MCONF_NONE          → FALLBACK   (Amazon estimate) or NONE (no data)
"""

import logging
from typing import List

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    Book, Opportunity,
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
    CONFIDENCE_FALLBACK, CONFIDENCE_NONE,
    FLAG_MISSING_APP_ID, FLAG_API_FAILURE, FLAG_NO_SOLD_COMPS,
    FLAG_FEW_COMPS, FLAG_ACTIVE_ONLY, FLAG_FALLBACK_PRICING,
    FLAG_WIDE_SPREAD, FLAG_LOW_MARGIN, FLAG_NO_EBAY_DATA,
)
from market_data import get_market_data, reset_stats, get_health_summary
from market_data.models import (
    MarketResult,
    MCONF_SOLD_STRONG, MCONF_SOLD_WEAK,
    MCONF_ACTIVE_STRONG, MCONF_ACTIVE_MEDIUM, MCONF_ACTIVE_WEAK, MCONF_NONE,
)
from fee_calculator import calculate_profit
import config

logger = logging.getLogger(__name__)

# Spread above this % gets a WIDE_SPREAD flag
_SPREAD_WARN_PCT = 60.0

# Margin below this gets a LOW_MARGIN flag (not a filter — just a warning)
_MARGIN_WARN_PCT = 0.15


# ── Confidence mapper ─────────────────────────────────────────────────────────

def _map_confidence(market_conf: str, has_amazon: bool) -> str:
    """Map market-data confidence → scanner confidence level."""
    return {
        MCONF_SOLD_STRONG:   CONFIDENCE_HIGH,
        MCONF_SOLD_WEAK:     CONFIDENCE_MEDIUM,
        MCONF_ACTIVE_STRONG: CONFIDENCE_MEDIUM,
        MCONF_ACTIVE_MEDIUM: CONFIDENCE_LOW,
        MCONF_ACTIVE_WEAK:   CONFIDENCE_LOW,
        MCONF_NONE:          CONFIDENCE_FALLBACK if has_amazon else CONFIDENCE_NONE,
    }.get(market_conf, CONFIDENCE_NONE)


# ── Concern flags ─────────────────────────────────────────────────────────────

def _build_concern_flags(
    opp:    Opportunity,
    result: MarketResult,
) -> List[str]:
    flags = []

    # eBay credentials
    if not getattr(config, "EBAY_APP_ID",  ""):
        flags.append(FLAG_MISSING_APP_ID)
    if not getattr(config, "EBAY_CERT_ID", ""):
        flags.append("MISSING_CERT_ID")

    # Revenue source concerns
    if opp.revenue_source == "amazon_estimate":
        flags.append(FLAG_FALLBACK_PRICING)
        if not result.is_usable():
            flags.append(FLAG_NO_EBAY_DATA)

    elif opp.revenue_source == "ebay_active":
        # Active listings are always flagged — they are not sold comps
        flags.append(FLAG_NO_SOLD_COMPS)
        flags.append(FLAG_ACTIVE_ONLY)
        if opp.ebay_active_count < 5:
            flags.append(FLAG_FEW_COMPS)

    elif opp.revenue_source == "ebay_sold":
        # Only reachable if Marketplace Insights is implemented in future
        if opp.ebay_sold_count < 3:
            flags.append(FLAG_FEW_COMPS)

    # Price spread (unreliable pricing signal)
    if (
        result.price_summary
        and result.price_summary.spread_pct > _SPREAD_WARN_PCT
    ):
        flags.append(FLAG_WIDE_SPREAD)

    # Margin warning
    if 0 < opp.margin_pct < _MARGIN_WARN_PCT:
        flags.append(FLAG_LOW_MARGIN)

    return flags


# ── Core analysis ─────────────────────────────────────────────────────────────

def analyze_book(book: Book) -> Opportunity:
    """
    Run the full analysis pipeline for one book.
    Returns a fully-populated Opportunity.
    """
    opp = Opportunity(book=book)

    # ── 1. Fetch market data ─────────────────────────────────────────────
    result = get_market_data(
        isbn13     = book.isbn13,
        isbn10     = book.isbn10,
        book_title = book.title,
    )

    # Route results based on listing type (sold vs active)
    from market_data.models import LISTING_SOLD
    is_sold_result = result.listing_type == LISTING_SOLD and result.listings

    if is_sold_result:
        # Sold comps — populate sold fields
        opp.ebay_sold_listings  = result.listings
        opp.ebay_sold_count     = len(result.listings)
        opp.ebay_sold_median    = result.price_summary.median if result.price_summary else None
        opp.price_spread_pct    = result.price_summary.spread_pct if result.price_summary else None
        opp.ebay_active_listings = []
        opp.ebay_active_count    = 0
        opp.ebay_active_median   = None
    else:
        # Active listings
        opp.ebay_active_listings = result.listings
        opp.ebay_active_count    = len(result.listings)
        opp.ebay_active_median   = result.price_summary.median if result.price_summary else None
        opp.price_spread_pct     = result.price_summary.spread_pct if result.price_summary else None
        opp.ebay_sold_listings   = []
        opp.ebay_sold_count      = 0
        opp.ebay_sold_median     = None

    # ── 2. Choose revenue estimate ───────────────────────────────────────
    if result.is_usable() and is_sold_result:
        # Sold comps — use median directly, no discount needed
        raw_median           = result.price_summary.median
        opp.revenue_estimate = raw_median
        opp.revenue_source   = "ebay_sold"
        opp.confidence       = _map_confidence(result.market_confidence, bool(book.amazon_price))
        logger.info(
            f"Revenue: eBay SOLD median ${raw_median:.2f} "
            f"({opp.ebay_sold_count} comps) — '{book.title[:45]}'"
        )

    elif result.is_usable():
        # Active listing median, discounted to estimate sell price
        discount             = getattr(config, "EBAY_ACTIVE_PRICE_DISCOUNT", 0.10)
        raw_median           = result.price_summary.median
        opp.revenue_estimate = round(raw_median * (1.0 - discount), 2)
        opp.revenue_source   = "ebay_active"
        opp.confidence       = _map_confidence(result.market_confidence, bool(book.amazon_price))
        logger.debug(
            f"Revenue: eBay active median ${raw_median:.2f} "
            f"× {1.0-discount:.2f} discount = ${opp.revenue_estimate:.2f} "
            f"({opp.ebay_active_count} listings) — '{book.title[:45]}'"
        )

    elif config.USE_AMAZON_FALLBACK and book.amazon_price is not None:
        opp.revenue_estimate = round(
            book.amazon_price * config.AMAZON_TO_EBAY_DISCOUNT, 2
        )
        opp.revenue_source   = "amazon_estimate"
        opp.confidence       = CONFIDENCE_FALLBACK
        logger.info(
            f"Revenue: AMAZON FALLBACK ${book.amazon_price:.2f} "
            f"× {config.AMAZON_TO_EBAY_DISCOUNT} = ${opp.revenue_estimate:.2f} "
            f"— '{book.title[:45]}'"
        )

    else:
        opp.confidence  = CONFIDENCE_NONE
        opp.skip_reason = "no_market_data"
        logger.info(f"SKIP (no data): '{book.title[:60]}'")
        return opp

    # ── 3. Velocity filter (optional, off by default) ────────────────────
    if getattr(config, "VELOCITY_FILTER_ENABLED", False):
        min_sold = getattr(config, "VELOCITY_MIN_SOLD", 3)
        if opp.ebay_sold_count < min_sold:
            opp.skip_reason = f"velocity_{opp.ebay_sold_count}_below_{min_sold}"
            logger.info(
                f"SKIP (velocity {opp.ebay_sold_count} < {min_sold}): "
                f"'{book.title[:55]}'"
            )
            return opp

    # ── 4. Profit calculation ─────────────────────────────────────────────
    calc              = calculate_profit(cost=book.cost, revenue=opp.revenue_estimate)
    opp.ebay_fee      = calc["ebay_fee"]
    opp.shipping_cost = calc["shipping_cost"]
    opp.cogs          = calc["cogs"]
    opp.profit        = calc["profit"]
    opp.margin_pct    = calc["margin_pct"]

    # ── 5. Concern flags ──────────────────────────────────────────────────
    opp.concern_flags = _build_concern_flags(opp, result)

    # ── 6. Threshold gates ────────────────────────────────────────────────
    if opp.profit < config.MIN_PROFIT:
        opp.skip_reason = (
            f"profit_${opp.profit:.2f}_below_${config.MIN_PROFIT:.2f}"
        )
        logger.info(
            f"SKIP (profit ${opp.profit:.2f} < ${config.MIN_PROFIT:.2f}): "
            f"'{book.title[:55]}'"
        )
    elif opp.margin_pct < config.MIN_MARGIN:
        opp.skip_reason = (
            f"margin_{opp.margin_pct*100:.1f}%_below_{config.MIN_MARGIN*100:.0f}%"
        )
        logger.info(
            f"SKIP (margin {opp.margin_pct*100:.1f}% < "
            f"{config.MIN_MARGIN*100:.0f}%): '{book.title[:55]}'"
        )
    else:
        opp.is_opportunity = True
        flags_str = f" ⚠ {opp.concern_str}" if opp.concern_flags else ""
        logger.info(
            f"✓ [{opp.confidence}] ${opp.profit:.2f} profit "
            f"({opp.margin_pct*100:.0f}% | {opp.revenue_source}): "
            f"'{book.title[:50]}'{flags_str}"
        )

    return opp


def analyze_all(books: List[Book]) -> List[Opportunity]:
    """
    Analyze every book. Returns all Opportunity objects sorted by profit desc.
    """
    reset_stats()
    results = []
    total   = len(books)

    for i, book in enumerate(books, 1):
        logger.info(f"[{i}/{total}] '{book.title[:65]}'")
        results.append(analyze_book(book))

    results.sort(key=lambda o: (not o.is_opportunity, -o.profit))
    return results
