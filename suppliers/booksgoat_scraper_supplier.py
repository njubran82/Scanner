"""
suppliers/booksgoat_scraper_supplier.py — Future: BooksGoat web scraper.

STUB — not yet implemented.

If you want to scrape BooksGoat directly instead of using a CSV export,
implement this class. Set SUPPLIER = "booksgoat_scraper" in config.py.

Notes from prior project experience:
- BooksGoat blocks scrapers; use retry logic with exponential backoff
- Up to 3 attempts recommended; log-and-skip on failure
- Use Playwright for JavaScript-rendered pages
"""

import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import List
from models import Book
from suppliers.base_supplier import BaseSupplier

logger = logging.getLogger(__name__)


class BooksGoatScraperSupplier(BaseSupplier):
    """
    Scrapes live inventory directly from BooksGoat.com.

    TODO: Implement with Playwright once scraping approach is validated.
    """

    def get_books(self) -> List[Book]:
        """
        Scrape BooksGoat and return normalized Book objects.

        Implementation checklist:
        1. Use Playwright to navigate the BooksGoat catalog
        2. Handle anti-bot blocks with retry + backoff (3 attempts max)
        3. Extract title, ISBN, and pricing tiers per book
        4. Map to Book dataclass (same as CSVSupplier output)
        5. Rate-limit requests to avoid IP bans
        """
        logger.warning(
            "BooksGoatScraperSupplier is not yet implemented. "
            "Switch SUPPLIER back to 'csv' in config.py."
        )
        return []
