"""
market_data/amazon_stub.py — Future Amazon market data source.

STUB — not yet implemented.

WHY AMAZON DATA MATTERS FOR BOOK ARBITRAGE:
    Amazon is the primary price anchor for the used textbook market.
    Amazon list price is already in the BooksGoat sheet as a rough
    ceiling reference. A proper Amazon source would provide:
        - Current used prices (3rd-party sellers on Amazon)
        - Price history / Keepa-style trend data
        - Sales rank / demand signal
        - Buy Box price (actual competitive baseline)

IMPLEMENTATION OPTIONS (when ready):
    Option A: Amazon Product Advertising API (PA-API 5.0)
        - Official API, requires affiliate account
        - Returns: price, availability, sales rank, product metadata
        - Rate-limited: ~1 req/sec, requires qualifying affiliate sales
        - Endpoint: webservices.amazon.com/paapi5/searchitems
        - ASIN lookup by ISBN: search by Keywords=isbn, SearchIndex=Books
        - Credentials needed: AWS_ACCESS_KEY, AWS_SECRET_KEY, ASSOCIATE_TAG

    Option B: Keepa API
        - Third-party service that tracks Amazon price history
        - Returns: price history, sales rank history, offer counts
        - Paid service (~$15/mo for 5000 tokens)
        - Very useful for: "is this book actually selling, or just listed?"
        - Endpoint: https://api.keepa.com/product
        - Credential: KEEPA_API_KEY

    Option C: Direct scraping (not recommended)
        - Against Amazon ToS, fragile, requires proxies
        - Only viable if PA-API access is unavailable

RECOMMENDATION:
    Start with PA-API Option A. It's free with an affiliate account and
    provides the most direct Amazon pricing data. The affiliate requirement
    means you need some qualifying sales on your Amazon affiliate account.
    For a book arbitrage business with active eBay sales, this is achievable.

TO IMPLEMENT:
    1. Get PA-API credentials from affiliate.amazon.com
    2. Add AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, AMAZON_ASSOCIATE_TAG to .env
    3. pip install amazon-paapi5 or implement the request signing manually
    4. Implement get_active_listings() below using ISBN → ASIN lookup
    5. Change MARKET_DATA_SOURCES in market_data/__init__.py to include this
"""

import logging
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Optional
from market_data.base import AbstractMarketSource
from market_data.models import MarketResult, SOURCE_AMAZON, LISTING_ACTIVE, MCONF_NONE

logger = logging.getLogger(__name__)


class AmazonSource(AbstractMarketSource):
    """
    Amazon used book prices as a market data source.

    Provides a secondary validation layer alongside eBay data.
    When both eBay and Amazon data are available, the scanner
    can flag significant price gaps as arbitrage opportunities
    with higher confidence.

    NOT YET IMPLEMENTED — see module docstring for options.
    """

    @property
    def source_name(self) -> str:
        return SOURCE_AMAZON

    def is_available(self) -> bool:
        # Will check for Amazon credentials when implemented
        return False

    def get_active_listings(self, isbn13: str, isbn10: str = "") -> MarketResult:
        logger.debug(
            "AmazonSource.get_active_listings() called but not yet implemented. "
            "Returning empty result."
        )
        return MarketResult(
            isbn13            = isbn13,
            isbn10            = isbn10,
            source            = SOURCE_AMAZON,
            listing_type      = LISTING_ACTIVE,
            market_confidence = MCONF_NONE,
            error             = "Amazon source not yet implemented",
        )

    def get_sold_listings(self, isbn13: str, isbn10: str = "") -> Optional[MarketResult]:
        # Amazon doesn't expose sold listing history publicly
        return None
