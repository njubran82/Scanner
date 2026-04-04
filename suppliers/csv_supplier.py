"""
suppliers/csv_supplier.py — Primary supplier: BooksGoat merchant sheet.

DATA SOURCE STRATEGY:
    PRIMARY:  Fetch live from Google Sheets URL on every run.
              The sheet refreshes weekly (Sundays). Fetching live ensures
              the scanner always works from the latest pricing and catalog.

    FALLBACK: If the URL is unreachable, optionally fall back to a local
              CSV file (config.FALLBACK_TO_LOCAL = True). This prevents
              the scanner from dying completely if the URL is temporarily
              down or rate-limited.

SHEET SCHEMA (current as of April 2025):
    Title        → book.title
    ISBN-13      → book.isbn13
    ISBN-10      → book.isbn10
    5 Qty        → book.cost          (primary cost tier — per-order dropship price)
    10 Qty       → book.cost_10qty    (stored for reference)
    25 Qty       → book.cost_25qty    (stored for reference)
    Amazon Price → book.amazon_price  (may be blank for some books)
    Amazon Rank  → book.amazon_rank   (informational only)

DESIGN NOTE — TREATING THE SHEET AS A PROTO-API:
    The sheet is not a real API, but we treat it like one:
    - Fetched fresh on every run (no stale local copies)
    - Parsed defensively (unknown columns are ignored, not errors)
    - Returns a consistent List[Book] regardless of source
    When BooksGoat adds a real API, only this file needs to change.
"""

import io
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from typing import List, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Book
from suppliers.base_supplier import BaseSupplier
import config

logger = logging.getLogger(__name__)

# HTTP headers that mimic a browser — some Google Sheets URLs block bare requests
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _parse_price(value) -> Optional[float]:
    """
    Convert '$57.99', '57.99', or similar into a float.
    Returns None if the value is blank, NaN, or unparseable.
    """
    if pd.isna(value) or str(value).strip() in ("", "N/A", "—", "nan"):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def _fetch_url_with_retry(url: str) -> Optional[bytes]:
    """
    Fetch a URL with retry + exponential backoff.

    Returns the raw response bytes on success, or None if all retries fail.
    Delay doubles each attempt: 5s → 10s → 20s (configurable via config).
    """
    delay = config.URL_FETCH_RETRY_DELAY

    for attempt in range(1, config.URL_FETCH_RETRIES + 1):
        try:
            logger.info(f"Fetching supplier URL (attempt {attempt}/{config.URL_FETCH_RETRIES})")
            response = requests.get(url, headers=_HEADERS, timeout=30)
            response.raise_for_status()
            logger.info(
                f"Supplier URL fetched successfully "
                f"({len(response.content):,} bytes)"
            )
            return response.content

        except requests.exceptions.HTTPError as e:
            logger.warning(f"HTTP error on attempt {attempt}: {e}")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Connection error on attempt {attempt}: {e}")
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout on attempt {attempt} (30s limit)")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Request failed on attempt {attempt}: {e}")

        if attempt < config.URL_FETCH_RETRIES:
            logger.info(f"Retrying in {delay}s...")
            time.sleep(delay)
            delay *= 2   # Exponential backoff

    logger.error(
        f"All {config.URL_FETCH_RETRIES} attempts to fetch supplier URL failed."
    )
    return None


def _load_dataframe_from_bytes(raw_bytes: bytes) -> Optional[pd.DataFrame]:
    """Parse raw CSV bytes into a DataFrame, or return None on failure."""
    try:
        df = pd.read_csv(io.BytesIO(raw_bytes), dtype=str)
        logger.info(f"Parsed CSV: {len(df)} rows, columns: {df.columns.tolist()}")
        return df
    except Exception as e:
        logger.error(f"Failed to parse CSV content: {e}")
        return None


def _load_dataframe_from_file(path: str) -> Optional[pd.DataFrame]:
    """Load a local CSV file as a DataFrame fallback."""
    if not os.path.exists(path):
        logger.error(f"Local fallback file not found: {path}")
        return None
    try:
        df = pd.read_csv(path, dtype=str)
        logger.info(
            f"Loaded local fallback CSV: {len(df)} rows from '{path}'"
        )
        return df
    except Exception as e:
        logger.error(f"Failed to load local CSV '{path}': {e}")
        return None


def _dataframe_to_books(df: pd.DataFrame, cost_tier: str, source_label: str) -> List[Book]:
    """
    Convert a DataFrame (from URL or local file) into a list of Book objects.

    Rows missing a title, ISBN-13, or valid cost are skipped with a warning.
    Unknown extra columns in the sheet are silently ignored — this ensures
    the parser doesn't break when BooksGoat adds new columns in future refreshes.
    """
    required_cols = {"Title", "ISBN-13", cost_tier}
    missing = required_cols - set(df.columns)
    if missing:
        logger.error(
            f"Supplier sheet is missing required columns: {missing}. "
            f"Available: {df.columns.tolist()}"
        )
        return []

    books   = []
    skipped = 0
    fetched_at = datetime.now(timezone.utc)

    for idx, row in df.iterrows():
        title  = str(row.get("Title", "")).strip()
        isbn13 = str(row.get("ISBN-13", "")).strip()
        isbn10 = str(row.get("ISBN-10", "")).strip()
        cost   = _parse_price(row.get(cost_tier))

        if not title or title == "nan":
            skipped += 1
            continue
        if not isbn13 or isbn13 == "nan":
            logger.warning(f"Row {idx} ('{title[:40]}'): skipping — no ISBN-13")
            skipped += 1
            continue
        if cost is None:
            logger.warning(f"Row {idx} ('{title[:40]}'): skipping — no cost in '{cost_tier}'")
            skipped += 1
            continue

        books.append(Book(
            title        = title,
            isbn13       = isbn13,
            isbn10       = isbn10 if isbn10 != "nan" else "",
            cost         = cost,
            cost_10qty   = _parse_price(row.get("10 Qty")),
            cost_25qty   = _parse_price(row.get("25 Qty")),
            amazon_price = _parse_price(row.get("Amazon Price")),
            amazon_rank  = str(row.get("Amazon Rank", "")).strip() or None,
            source       = source_label,
            fetched_at   = fetched_at,
        ))

    logger.info(
        f"Built {len(books)} Book objects "
        f"({skipped} rows skipped) from '{source_label}'"
    )
    return books


class URLCSVSupplier(BaseSupplier):
    """
    PRIMARY SUPPLIER — Fetches the BooksGoat merchant sheet from a live URL.

    The sheet is treated as a structured data feed (proto-API):
    - Fetched fresh on every run
    - Weekly updates from BooksGoat are picked up automatically
    - New books added to the sheet appear on the next run
    - Falls back to a local file if the URL is unreachable (optional)

    Usage:
        supplier = URLCSVSupplier()
        books = supplier.get_books()
    """

    def __init__(self, url: str = None, cost_tier: str = None):
        self.url       = url       or config.SUPPLIER_CSV_URL
        self.cost_tier = cost_tier or config.COST_TIER

    def get_books(self) -> List[Book]:
        logger.info(f"URLCSVSupplier: fetching from {self.url}")

        # ── Attempt 1: Live URL ──────────────────────────────────────────
        raw = _fetch_url_with_retry(self.url)
        if raw:
            df = _load_dataframe_from_bytes(raw)
            if df is not None:
                return _dataframe_to_books(
                    df, self.cost_tier, f"url:{self.url}"
                )
            logger.warning("URL content fetched but could not be parsed as CSV.")

        # ── Attempt 2: Local fallback ────────────────────────────────────
        if config.FALLBACK_TO_LOCAL:
            logger.warning(
                f"Falling back to local CSV: {config.CSV_FALLBACK_PATH}"
            )
            df = _load_dataframe_from_file(config.CSV_FALLBACK_PATH)
            if df is not None:
                return _dataframe_to_books(
                    df, self.cost_tier, f"local:{config.CSV_FALLBACK_PATH}"
                )

        logger.error(
            "URLCSVSupplier: all data sources failed. Returning empty list."
        )
        return []


class CSVSupplier(BaseSupplier):
    """
    FALLBACK SUPPLIER — Reads books from a local CSV file only.

    Use this for offline testing or if the live URL is permanently unavailable.
    Set SUPPLIER = "csv" in config.py to activate.
    """

    def __init__(self, csv_path: str = None, cost_tier: str = None):
        self.csv_path  = csv_path  or config.CSV_FALLBACK_PATH
        self.cost_tier = cost_tier or config.COST_TIER

    def get_books(self) -> List[Book]:
        logger.info(f"CSVSupplier: loading from local file '{self.csv_path}'")
        df = _load_dataframe_from_file(self.csv_path)
        if df is None:
            return []
        return _dataframe_to_books(df, self.cost_tier, f"local:{self.csv_path}")
