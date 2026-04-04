"""
suppliers/__init__.py — Supplier factory.

Call get_supplier() to get the active supplier based on config.SUPPLIER.

Supplier options:
    "url_csv"             → URLCSVSupplier  (PRIMARY: live Google Sheets URL)
    "csv"                 → CSVSupplier     (FALLBACK: local file only)
    "booksgoat_api"       → BooksGoatAPISupplier   (future)
    "booksgoat_scraper"   → BooksGoatScraperSupplier (future, optional)

To add a new supplier:
    1. Create a class in suppliers/ that extends BaseSupplier
    2. Add a case in get_supplier() below
    3. Set SUPPLIER = "your_new_key" in config.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from suppliers.base_supplier import BaseSupplier


def get_supplier() -> BaseSupplier:
    name = config.SUPPLIER.lower()

    if name == "url_csv":
        from suppliers.csv_supplier import URLCSVSupplier
        return URLCSVSupplier()

    elif name == "csv":
        from suppliers.csv_supplier import CSVSupplier
        return CSVSupplier()

    elif name == "booksgoat_api":
        from suppliers.booksgoat_api_supplier import BooksGoatAPISupplier
        return BooksGoatAPISupplier()

    elif name == "booksgoat_scraper":
        from suppliers.booksgoat_scraper_supplier import BooksGoatScraperSupplier
        return BooksGoatScraperSupplier()

    else:
        raise ValueError(
            f"Unknown supplier '{name}'. "
            f"Valid options: url_csv, csv, booksgoat_api, booksgoat_scraper"
        )
