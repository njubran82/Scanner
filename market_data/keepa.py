"""
market_data/keepa.py
─────────────────────────────────────────────────────────────────
Keepa API integration for live Amazon price comparison.

Fetches current Amazon new price by ISBN-13 and adds it to the
profit analysis so you can see:
  - What Amazon is currently selling the book for (new)
  - Whether your eBay price is competitive vs Amazon
  - Whether BooksGoat cost leaves room for profit vs Amazon

API docs: https://keepa.com/#!api
Pricing:  ~$20/month for 1 plan (1 token/minute, sufficient for
          weekly scans of ~100 books)

Setup:
  1. Sign up at https://keepa.com
  2. Subscribe to any paid plan
  3. Get your API key from https://keepa.com/#!api
  4. Add KEEPA_API_KEY to your .env and GitHub Secrets

Keepa price encoding:
  Prices are stored as integers (cents * 10), so divide by 100.
  A value of -1 means "not available / no listing".
─────────────────────────────────────────────────────────────────
"""

import os
import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

KEEPA_BASE = "https://api.keepa.com/product"

# Keepa domain codes
DOMAIN_US = 1

# Keepa price array indices for new item prices
# https://keepa.com/#!discuss/t/product-object/116
IDX_AMAZON_NEW   = 0   # Amazon's own listing (Buy Box)
IDX_NEW_3P       = 1   # Third-party new sellers
IDX_USED         = 2   # Used listings

# How long to cache results (seconds) — avoids re-fetching same ISBN
_CACHE: dict = {}
CACHE_TTL = 3600  # 1 hour


def _keepa_price_to_dollars(raw: int) -> Optional[float]:
    """Convert Keepa price integer to dollars. Returns None if unavailable."""
    if raw is None or raw < 0:
        return None
    return round(raw / 100.0, 2)


def get_amazon_price(isbn: str, api_key: Optional[str] = None) -> dict:
    """
    Fetch current Amazon new price for a book by ISBN-13.

    Returns a dict:
    {
        "isbn":         str,
        "amazon_new":   float | None,  # Amazon's own new price
        "new_3p_low":   float | None,  # Lowest third-party new price
        "asin":         str | None,
        "title":        str | None,
        "available":    bool,
        "source":       "keepa" | "cache" | "error",
        "error":        str | None,
    }
    """
    if not api_key:
        api_key = os.getenv("KEEPA_API_KEY", "")

    if not api_key:
        return _error_result(isbn, "KEEPA_API_KEY not set")

    # Check cache
    cache_key = f"isbn:{isbn}"
    if cache_key in _CACHE:
        entry = _CACHE[cache_key]
        if time.time() - entry["cached_at"] < CACHE_TTL:
            result = dict(entry["data"])
            result["source"] = "cache"
            return result

    try:
        resp = requests.get(
            KEEPA_BASE,
            params={
                "key":    api_key,
                "domain": DOMAIN_US,
                "code":   isbn,        # ISBN-13 lookup
                "stats":  0,           # Skip stats to save tokens
                "history": 0,          # Skip price history to save tokens
            },
            timeout=15,
        )

        if resp.status_code == 429:
            return _error_result(isbn, "Rate limited — too many requests")

        if resp.status_code == 403:
            return _error_result(isbn, "Invalid Keepa API key")

        if resp.status_code != 200:
            return _error_result(isbn, f"HTTP {resp.status_code}")

        data = resp.json()

        # Check token status
        tokens_left = data.get("tokensLeft", -1)
        if tokens_left == 0:
            logger.warning("Keepa tokens exhausted — consider upgrading plan")

        products = data.get("products", [])
        if not products:
            result = {
                "isbn":       isbn,
                "amazon_new": None,
                "new_3p_low": None,
                "asin":       None,
                "title":      None,
                "available":  False,
                "source":     "keepa",
                "error":      None,
            }
            _cache_result(cache_key, result)
            return result

        product = products[0]
        asin    = product.get("asin")
        title   = product.get("title", "")[:80] if product.get("title") else None

        # Current prices are in the 'stats' block when stats=1,
        # or in the csv arrays as the last value when stats=0.
        # Use the stats endpoint approach via 'csv' last values:
        csv_data = product.get("csv", [])

        amazon_new_raw = _get_current_price(csv_data, IDX_AMAZON_NEW)
        new_3p_raw     = _get_current_price(csv_data, IDX_NEW_3P)

        amazon_new = _keepa_price_to_dollars(amazon_new_raw)
        new_3p_low = _keepa_price_to_dollars(new_3p_raw)

        # Best available new price
        candidates = [p for p in [amazon_new, new_3p_low] if p is not None]
        best_new   = min(candidates) if candidates else None

        result = {
            "isbn":       isbn,
            "amazon_new": amazon_new,
            "new_3p_low": new_3p_low,
            "best_new":   best_new,
            "asin":       asin,
            "title":      title,
            "available":  best_new is not None,
            "source":     "keepa",
            "error":      None,
            "tokens_left": tokens_left,
        }

        _cache_result(cache_key, result)
        return result

    except requests.RequestException as e:
        return _error_result(isbn, f"Request error: {e}")
    except Exception as e:
        return _error_result(isbn, f"Unexpected error: {e}")


def _get_current_price(csv_data: list, index: int) -> Optional[int]:
    """
    Extract the most recent price from Keepa's csv array.
    Keepa stores price history as [timestamp, price, timestamp, price, ...]
    The last price value is the most recent.
    """
    try:
        series = csv_data[index] if index < len(csv_data) else None
        if not series:
            return None
        # Series format: [time0, price0, time1, price1, ...]
        # Last element is the most recent price (odd index)
        for i in range(len(series) - 1, 0, -2):
            val = series[i]
            if val is not None and val >= 0:
                return val
        return None
    except (IndexError, TypeError):
        return None


def _error_result(isbn: str, error: str) -> dict:
    logger.warning(f"Keepa lookup failed for {isbn}: {error}")
    return {
        "isbn":       isbn,
        "amazon_new": None,
        "new_3p_low": None,
        "best_new":   None,
        "asin":       None,
        "title":      None,
        "available":  False,
        "source":     "error",
        "error":      error,
    }


def _cache_result(key: str, result: dict):
    _CACHE[key] = {"data": result, "cached_at": time.time()}


def enrich_opportunities(opportunities: list, api_key: Optional[str] = None,
                          delay: float = 1.2) -> list:
    """
    Add Amazon price data to a list of scanner opportunity dicts.
    Adds 'amazon_new', 'amazon_3p_low', 'amazon_vs_ebay' fields.

    delay: seconds between Keepa API calls (plan = 1 token/minute = ~1.2s safe)
    """
    if not api_key:
        api_key = os.getenv("KEEPA_API_KEY", "")

    if not api_key:
        logger.warning("KEEPA_API_KEY not set — skipping Amazon price enrichment")
        return opportunities

    enriched = []
    total    = len(opportunities)

    for i, opp in enumerate(opportunities, 1):
        isbn = opp.get("ISBN-13", "").strip()
        if not isbn:
            enriched.append(opp)
            continue

        print(f"  Keepa [{i}/{total}] {isbn}...", end=" ", flush=True)
        kdata = get_amazon_price(isbn, api_key)

        opp = dict(opp)  # Don't mutate original
        opp["amazon_new"]     = f"${kdata['amazon_new']:.2f}" if kdata.get("amazon_new") else "N/A"
        opp["amazon_3p_low"]  = f"${kdata['new_3p_low']:.2f}" if kdata.get("new_3p_low") else "N/A"
        opp["amazon_best"]    = f"${kdata['best_new']:.2f}"   if kdata.get("best_new")   else "N/A"

        # Flag if Amazon is cheaper than your eBay listing
        try:
            ebay_rev  = float(str(opp.get("Revenue", "0")).replace("$","").replace(",",""))
            amz_best  = kdata.get("best_new")
            if amz_best and ebay_rev:
                diff = ((ebay_rev - amz_best) / amz_best) * 100
                opp["amazon_vs_ebay"] = f"{diff:+.1f}%"
                # If eBay price is 10%+ higher than Amazon, flag it
                if diff > 10:
                    opp["amazon_vs_ebay"] += " ⚠️ eBay higher"
                elif diff < -10:
                    opp["amazon_vs_ebay"] += " ✅ eBay cheaper"
            else:
                opp["amazon_vs_ebay"] = "N/A"
        except (ValueError, TypeError):
            opp["amazon_vs_ebay"] = "N/A"

        source = kdata.get("source", "")
        print(f"${kdata['best_new']:.2f}" if kdata.get("best_new") else kdata.get("error", "no data"),
              f"({'cached' if source == 'cache' else 'live'})")

        enriched.append(opp)

        # Rate limit — skip delay if result was cached
        if kdata.get("source") != "cache":
            time.sleep(delay)

    return enriched


if __name__ == "__main__":
    # Quick test
    import sys
    key  = os.getenv("KEEPA_API_KEY", "")
    isbn = sys.argv[1] if len(sys.argv) > 1 else "9781305100558"  # Milady Barbering
    print(f"Testing Keepa lookup for ISBN {isbn}...")
    result = get_amazon_price(isbn, key)
    for k, v in result.items():
        print(f"  {k}: {v}")
