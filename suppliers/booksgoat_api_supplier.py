"""
suppliers/booksgoat_api_supplier.py — Future: BooksGoat API supplier.

STUB — not yet implemented.

When BooksGoat exposes a proper API (or if you obtain API access),
implement this class to replace the CSV supplier. The scanner will
work without any other changes — just set SUPPLIER = "booksgoat_api"
in config.py.

Expected API behavior (assumed):
    - Endpoint returns a JSON list of books with pricing tiers
    - Requires an API key (store in .env as BOOKSGOAT_API_KEY)
    - Supports pagination or returns full catalog
"""

import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import List
from models import Book
from suppliers.base_supplier import BaseSupplier

logger = logging.getLogger(__name__)


class BooksGoatAPISupplier(BaseSupplier):
    """
    Fetches live inventory from the BooksGoat API.

    TODO: Implement when API credentials are available.
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("BOOKSGOAT_API_KEY", "")

    def get_books(self) -> List[Book]:
        """
        Call the BooksGoat API and return normalized Book objects.

        Implementation checklist:
        1. Authenticate with API key
        2. Paginate through full catalog
        3. Map API fields → Book dataclass (same fields as CSVSupplier)
        4. Handle rate limits and network errors gracefully
        """
        logger.warning(
            "BooksGoatAPISupplier is not yet implemented. "
            "Switch SUPPLIER back to 'csv' in config.py."
        )
        return []
