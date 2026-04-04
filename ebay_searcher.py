"""
ebay_searcher.py — DEPRECATED. Kept for reference only.

This module implemented the legacy eBay Finding API (findCompletedItems /
findItemsAdvanced). It has been replaced by the market_data/ package which
uses the current eBay Browse API with OAuth 2.0.

WHY IT WAS REPLACED:
    The Finding API uses App-ID-only auth (no OAuth) and is deprecated.
    eBay stopped reliably serving findCompletedItems for many developer
    accounts around 2024. The 181 failed calls in the scan output confirmed
    the Finding API was no longer working.

    The Browse API (market_data/ebay_browse.py) is eBay's current supported
    API and requires EBAY_APP_ID + EBAY_CERT_ID via OAuth 2.0.

DO NOT USE THIS MODULE IN NEW CODE.
    profit_analyzer.py now imports from market_data/, not this file.
    This file is retained only so existing imports don't break during
    the transition period. It will be removed in a future cleanup.
"""

import logging
logger = logging.getLogger(__name__)
logger.warning(
    "ebay_searcher.py is deprecated and no longer used. "
    "Market data now comes from market_data/ebay_browse.py."
)

# Stub exports so any accidental imports don't crash
from typing import List, Optional
from models import Book, EbaySoldListing, EbayActiveListing


def search_ebay_sold(book: Book) -> List[EbaySoldListing]:
    logger.error("search_ebay_sold() is deprecated. Use market_data.get_market_data() instead.")
    return []


def search_ebay_active(book: Book) -> List[EbayActiveListing]:
    logger.error("search_ebay_active() is deprecated. Use market_data.get_market_data() instead.")
    return []


def get_sold_median(listings):
    return None


def get_active_median(listings):
    return None


def get_price_spread_pct(listings):
    return None


def get_confidence(sold_count: int) -> str:
    return "NONE"


def reset_api_stats():
    pass


def get_api_health_summary() -> dict:
    return {
        "run_mode":        "DEPRECATED",
        "run_mode_reason": "ebay_searcher.py is deprecated — use market_data/",
        "sold_successes":  0,
        "sold_failures":   0,
        "api_key_missing": True,
    }
