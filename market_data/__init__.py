"""
market_data/__init__.py — Factory and public interface for the market-data layer.

USAGE (from profit_analyzer.py):
    from market_data import get_market_data, reset_stats, get_health_summary

    result = get_market_data(book)          # → MarketResult
    health = get_health_summary()           # → dict for email/logging

HOW TO ADD A NEW SOURCE:
    1. Create market_data/your_source.py implementing AbstractMarketSource
    2. Import it below
    3. Add it to MARKET_DATA_SOURCES

The system tries sources in order and uses the first one that is_available()
and returns results. eBay Browse API is the primary source. Amazon is a
future secondary source (currently stubbed out).
"""

import logging
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_data.models import MarketResult, LISTING_ACTIVE, MCONF_NONE
from market_data.ebay_browse import EbayBrowseSource, reset_stats as _ebay_reset, get_health_summary as _ebay_health
from market_data.amazon_stub import AmazonSource

logger = logging.getLogger(__name__)

# ── Registered sources (in priority order) ───────────────────────────────────
# Primary:   eBay Browse API — active listings, supported, well-documented
# Secondary: Amazon — stubbed, add PA-API credentials to activate
MARKET_DATA_SOURCES = [
    EbayBrowseSource(),
    AmazonSource(),
]


def get_market_data(isbn13: str, isbn10: str = "") -> MarketResult:
    """
    Fetch active market listings for a book, using the first available source.

    Returns a MarketResult. Never raises — errors are captured in result.error.
    Logs clearly which source was used or why all sources were skipped.
    """
    for source in MARKET_DATA_SOURCES:
        if not source.is_available():
            logger.debug(f"Market source {source.source_name} not available — skipping")
            continue

        logger.debug(f"Using market source: {source.source_name}")
        result = source.get_active_listings(isbn13, isbn10)

        if result.error:
            logger.warning(
                f"{source.source_name} returned error: {result.error} "
                f"— trying next source"
            )
            continue

        return result

    # All sources unavailable or failed
    logger.warning(
        f"No market data sources available for ISBN {isbn13}. "
        f"Check credentials (EBAY_APP_ID + EBAY_CERT_ID for eBay Browse API)."
    )
    return MarketResult(
        isbn13            = isbn13,
        isbn10            = isbn10,
        source            = "none",
        listing_type      = LISTING_ACTIVE,
        market_confidence = MCONF_NONE,
        error             = "No market data sources available or configured",
    )


def reset_stats() -> None:
    """Reset all source health counters. Call at the start of each scan run."""
    _ebay_reset()


def get_health_summary() -> dict:
    """
    Return combined health summary across all sources.
    Currently delegates to eBay Browse since it's the only active source.
    Extend this when additional sources are live.
    """
    return _ebay_health()
