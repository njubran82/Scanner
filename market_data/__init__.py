"""
market_data/__init__.py — Factory and public interface for the market-data layer.

USAGE (from profit_analyzer.py):
    from market_data import get_market_data, reset_stats, get_health_summary

    result = get_market_data(isbn13, isbn10, book_title)
    health = get_health_summary()

HOW TO ADD A NEW SOURCE:
    1. Create market_data/your_source.py implementing AbstractMarketSource
    2. Import it below and add to MARKET_DATA_SOURCES
    3. No other changes needed

SOURCE PRIORITY:
    EbayBrowseSource — primary (sold comps + active listings)
    AmazonSource     — future secondary (stubbed)
"""

import logging
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_data.models import MarketResult, LISTING_ACTIVE, MCONF_NONE
from market_data.ebay_browse import (
    EbayBrowseSource,
    reset_stats as _ebay_reset,
    get_health_summary as _ebay_health,
)
from market_data.amazon_stub import AmazonSource

logger = logging.getLogger(__name__)

MARKET_DATA_SOURCES = [
    EbayBrowseSource(),
    AmazonSource(),
]


def get_market_data(
    isbn13: str,
    isbn10: str = "",
    book_title: str = "",
) -> MarketResult:
    """
    Fetch market data for a book, using the first available source.
    Passes book_title through to enable title-based validation and fallback.
    Returns a MarketResult. Never raises.
    """
    for source in MARKET_DATA_SOURCES:
        if not source.is_available():
            logger.debug(f"Market source {source.source_name} not available — skipping")
            continue

        logger.debug(f"Using market source: {source.source_name}")

        # Pass book_title if the source's get_active_listings accepts it
        import inspect
        sig = inspect.signature(source.get_active_listings)
        if "book_title" in sig.parameters:
            result = source.get_active_listings(isbn13, isbn10, book_title=book_title)
        else:
            result = source.get_active_listings(isbn13, isbn10)

        if result.error:
            logger.warning(
                f"{source.source_name} returned error: {result.error} "
                f"— trying next source"
            )
            continue

        return result

    logger.warning(
        f"No market data sources available for ISBN {isbn13}. "
        f"Check EBAY_APP_ID + EBAY_CERT_ID credentials."
    )
    return MarketResult(
        isbn13            = isbn13,
        isbn10            = isbn10,
        source            = "none",
        listing_type      = LISTING_ACTIVE,
        market_confidence = MCONF_NONE,
        error             = "No market data sources available",
    )


def reset_stats() -> None:
    _ebay_reset()


def get_health_summary() -> dict:
    return _ebay_health()
