"""
market_data/base.py — Abstract interface every market data source must implement.

WHY THIS EXISTS:
    The scanner doesn't care whether market data comes from eBay Browse API,
    eBay Marketplace Insights, Amazon, AbeBooks, or any future source.
    Every source implements get_active_listings() and/or get_sold_listings()
    and returns MarketResult objects in a normalised format.

    Adding a new data source = create a new file, implement this interface,
    register it in __init__.py. Nothing else changes.

DESIGN NOTE — ACTIVE vs SOLD:
    Active listings (what's currently for sale) and sold listings (what
    actually transacted) are separated at the interface level because:
    1. Different APIs provide different data types
    2. The caller (profit_analyzer) handles them with different confidence weights
    3. Some sources may only provide one type
"""

from abc import ABC, abstractmethod
from typing import Optional
from market_data.models import MarketResult


class AbstractMarketSource(ABC):
    """
    Base class for all market data sources.

    Implementors must provide at least get_active_listings().
    get_sold_listings() defaults to returning None (unavailable)
    since most sources don't provide sold data.
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Short identifier for this source, e.g. 'ebay_browse'."""
        raise NotImplementedError

    @abstractmethod
    def is_available(self) -> bool:
        """
        Return True if this source is configured and ready.
        Check for required credentials/config here.
        Called before any search to avoid unnecessary failures.
        """
        raise NotImplementedError

    @abstractmethod
    def get_active_listings(self, isbn13: str, isbn10: str = "") -> MarketResult:
        """
        Fetch currently active (for sale) listings for a book.

        Must return a MarketResult even on failure — set result.error
        and return an empty result rather than raising.

        Args:
            isbn13: ISBN-13 (preferred)
            isbn10: ISBN-10 fallback

        Returns:
            MarketResult with listing_type = LISTING_ACTIVE
        """
        raise NotImplementedError

    def get_sold_listings(self, isbn13: str, isbn10: str = "") -> Optional[MarketResult]:
        """
        Fetch completed/sold listings for a book.

        Default implementation returns None (not available).
        Override in sources that provide sold data.

        Returns:
            MarketResult with listing_type = LISTING_SOLD, or None
        """
        return None

    def __repr__(self) -> str:
        status = "ready" if self.is_available() else "unavailable"
        return f"<{self.__class__.__name__} [{status}]>"
