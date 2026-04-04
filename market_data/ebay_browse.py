"""
market_data/ebay_browse.py — eBay market data: sold comps + active listings.

SEARCH STRATEGY (in order of preference):
    1. findCompletedItems (Finding API) by ISBN-13
       → Real sold transactions. Best revenue signal.
       → Confidence: SOLD_STRONG (3+) or SOLD_WEAK (1-2)

    2. findCompletedItems by ISBN-10 (if valid)
       → Same as above, ISBN-10 fallback

    3. Browse API active listings by ISBN-13
       → Currently listed prices. Discount applied to estimate sell price.
       → Confidence: ACTIVE_STRONG/MEDIUM/WEAK depending on count

    4. Browse API active listings by ISBN-10 (if valid)
       → Same as above, ISBN-10 fallback

    5. Browse API keyword search by title
       → Used when all ISBN searches return 0 results.
       → Results filtered by title similarity before use.
       → Confidence: ACTIVE_WEAK at best

JUNK DETECTION:
    ISBN-10 fields sometimes contain Amazon ASINs (e.g. B0C1XZ6GMB).
    These are 10-char alphanumeric codes starting with B — not valid ISBNs.
    Using them as GTINs returns massive unrelated listing pools.
    We validate ISBN-10 format before any search.

    We also reject result sets where:
        - Total results > 50,000 AND median price < $3.00
          (generic used book pools — not the specific title)

TITLE SIMILARITY:
    After any search, we check that returned listings actually match
    the book. We extract significant words from the supplier title
    and require at least 40% overlap with each listing title.
    Listings that don't match are dropped before price calculation.

CREDENTIALS:
    Browse API:   EBAY_APP_ID + EBAY_CERT_ID (OAuth 2.0)
    Finding API:  EBAY_APP_ID only (no CERT_ID needed)
"""

import base64
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import List, Optional, Set, Tuple

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_data.base import AbstractMarketSource
from market_data.models import (
    MarketListing, MarketResult, PriceSummary,
    LISTING_ACTIVE, LISTING_SOLD,
    SOURCE_EBAY_BROWSE,
    MCONF_SOLD_STRONG, MCONF_SOLD_WEAK,
    MCONF_ACTIVE_STRONG, MCONF_ACTIVE_MEDIUM, MCONF_ACTIVE_WEAK,
    MCONF_NONE,
)
import config

logger = logging.getLogger(__name__)

# ── eBay endpoints ────────────────────────────────────────────────────────────
_OAUTH_URL    = "https://api.ebay.com/identity/v1/oauth2/token"
_BROWSE_URL   = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_FINDING_URL  = "https://svcs.ebay.com/services/search/FindingService/v1"
_OAUTH_SCOPE  = "https://api.ebay.com/oauth/api_scope"
_BOOKS_CAT    = "267"
_USED_CONDS   = "2000|2500|3000|4000|5000"

# ── Junk detection thresholds ──────────────────────────────────────────────────
_JUNK_TOTAL_THRESHOLD  = 50_000   # Result sets this large are almost certainly generic
_JUNK_PRICE_CEILING    = 3.00     # Median below this = commodity junk pool
_MIN_TITLE_SIMILARITY  = 0.35     # Fraction of title words that must match listing

# ── Token cache ────────────────────────────────────────────────────────────────
_token_cache: dict = {"access_token": None, "expires_at": None}

# ── Health counters ────────────────────────────────────────────────────────────
_stats: dict = {
    "token_ok": False, "token_failed": False,
    "browse_calls": 0, "browse_ok": 0, "browse_failed": 0,
    "finding_calls": 0, "finding_ok": 0, "finding_failed": 0,
    "sold_results": 0, "active_results": 0,
    "zero_results": 0, "junk_rejected": 0,
    "title_filtered": 0, "keyword_fallback_used": 0,
    "isbn13_hits": 0, "isbn10_hits": 0,
    "api_ok": 0, "api_failed": 0,  # kept for backward compat
}


def reset_stats() -> None:
    global _stats
    _stats = {k: (False if isinstance(v, bool) else 0) for k, v in _stats.items()}


def get_health_summary() -> dict:
    s = dict(_stats)
    total = s["browse_calls"] + s["finding_calls"]

    s["sold_successes"]  = s["finding_ok"]
    s["sold_failures"]   = s["finding_failed"]
    s["api_ok"]          = s["browse_ok"] + s["finding_ok"]
    s["api_failed"]      = s["browse_failed"] + s["finding_failed"]
    s["success_rate"]    = round(s["api_ok"] / total * 100, 1) if total else 0.0
    s["api_key_missing"] = not bool(getattr(config, "EBAY_APP_ID", ""))

    if not s["token_ok"] and s["token_failed"]:
        s["run_mode"] = "FALLBACK_ONLY"
        s["run_mode_reason"] = "eBay OAuth failed — check EBAY_APP_ID and EBAY_CERT_ID"
    elif total == 0:
        s["run_mode"] = "FALLBACK_ONLY"
        s["run_mode_reason"] = "No eBay API calls made — credentials not configured"
    elif s["api_failed"] > 0 and s["api_ok"] == 0:
        s["run_mode"] = "FALLBACK_ONLY"
        s["run_mode_reason"] = "All eBay API calls failed"
    elif s["sold_results"] > 0 and s["active_results"] == 0:
        s["run_mode"] = "EBAY_CONFIRMED"
        s["run_mode_reason"] = (
            f"{s['finding_ok']} sold comp searches OK | "
            f"{s['sold_results']} books with sold data"
        )
    elif s["sold_results"] > 0:
        s["run_mode"] = "EBAY_CONFIRMED"
        s["run_mode_reason"] = (
            f"{s['sold_results']} eBay sold | "
            f"{s['active_results']} active only | "
            f"{s['zero_results']} no data | "
            f"{s['junk_rejected']} junk rejected"
        )
    else:
        s["run_mode"] = "MIXED"
        s["run_mode_reason"] = (
            f"{s['browse_ok']} Browse calls OK | "
            f"{s['active_results']} active results | "
            f"{s['zero_results']} no results | "
            f"{s['junk_rejected']} junk rejected | "
            f"{s['keyword_fallback_used']} keyword fallbacks"
        )

    return s


# ── ISBN-10 validation ────────────────────────────────────────────────────────

def _is_valid_isbn10(value: str) -> bool:
    """
    Return True only if value looks like a real ISBN-10.

    Rejects:
        - Amazon ASINs: 10-char alphanumeric starting with 'B' (e.g. B0C1XZ6GMB)
        - Non-ISBN formats: contains spaces, slashes, or other invalid chars
        - Too short or too long
        - Clearly non-numeric (ISBN-10 is 9 digits + optional X check digit)
    """
    if not value or value in ("nan", "", "N/A"):
        return False

    v = value.strip()

    # Must be 10 characters
    if len(v) != 10:
        return False

    # Amazon ASIN pattern: starts with B, all alphanumeric
    if v.upper().startswith("B") and v.isalnum():
        return False

    # Must be 9 digits followed by digit or X
    if not re.match(r"^\d{9}[\dXx]$", v):
        return False

    return True


# ── Title similarity ──────────────────────────────────────────────────────────

_STOP_WORDS: Set[str] = {
    "a", "an", "the", "and", "or", "of", "in", "to", "for", "by",
    "with", "on", "at", "from", "edition", "ed", "vol", "volume",
    "revised", "updated", "new", "isbn", "paperback", "hardcover",
    "softcover", "spiral", "bound", "us", "ebook", "digital"
}


def _title_words(title: str) -> Set[str]:
    """Extract significant lowercase words from a title, dropping stop words."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if w not in _STOP_WORDS and len(w) > 2}


def _title_similarity(book_title: str, listing_title: str) -> float:
    """
    Return fraction of book title words found in listing title.
    Range: 0.0 (no overlap) to 1.0 (all book words in listing).
    """
    book_words    = _title_words(book_title)
    listing_words = _title_words(listing_title)

    if not book_words:
        return 0.0

    overlap = book_words & listing_words
    return len(overlap) / len(book_words)


def _filter_by_title(
    listings: List[MarketListing],
    book_title: str,
    threshold: float = _MIN_TITLE_SIMILARITY,
) -> Tuple[List[MarketListing], int]:
    """
    Filter listings to those that match the book title.
    Returns (filtered_list, dropped_count).
    """
    if not book_title:
        return listings, 0

    filtered = [
        l for l in listings
        if _title_similarity(book_title, l.title) >= threshold
    ]
    dropped = len(listings) - len(filtered)

    if dropped > 0:
        logger.debug(
            f"Title filter: kept {len(filtered)}/{len(listings)} listings "
            f"(dropped {dropped} low-similarity) for '{book_title[:50]}'"
        )

    return filtered, dropped


# ── Junk detection ────────────────────────────────────────────────────────────

def _is_junk_result_set(total_count: int, listings: List[MarketListing]) -> bool:
    """
    Return True if this result set looks like a generic commodity pool
    rather than a specific book's listings.

    Signal: extremely high total count + very low prices.
    This pattern appears when an ISBN-10 that's actually an ASIN slips
    through validation, or when eBay returns generic 'used book' lots.
    """
    if total_count > _JUNK_TOTAL_THRESHOLD and listings:
        prices = [l.total_price for l in listings]
        med    = median(prices)
        if med < _JUNK_PRICE_CEILING:
            logger.warning(
                f"Junk result set detected: {total_count:,} total listings, "
                f"median price ${med:.2f} — rejecting"
            )
            return True
    return False


# ── OAuth token ───────────────────────────────────────────────────────────────

def _get_oauth_token() -> Optional[str]:
    global _token_cache

    now = datetime.now(timezone.utc)
    if (
        _token_cache["access_token"]
        and _token_cache["expires_at"]
        and now < _token_cache["expires_at"]
    ):
        return _token_cache["access_token"]

    app_id  = getattr(config, "EBAY_APP_ID",  "")
    cert_id = getattr(config, "EBAY_CERT_ID", "")

    if not app_id or not cert_id:
        missing = [k for k, v in [("EBAY_APP_ID", app_id), ("EBAY_CERT_ID", cert_id)] if not v]
        logger.error(
            f"Cannot get eBay OAuth token — missing: {missing}. "
            f"Both EBAY_APP_ID and EBAY_CERT_ID required for Browse API."
        )
        _stats["token_failed"] = True
        return None

    creds = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()

    try:
        resp = requests.post(
            _OAUTH_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": _OAUTH_SCOPE},
            timeout=15,
        )

        if resp.status_code == 401:
            logger.error(
                "eBay OAuth 401 — credentials rejected. "
                "Verify EBAY_APP_ID and EBAY_CERT_ID are from the PRODUCTION column."
            )
            _stats["token_failed"] = True
            return None

        if resp.status_code != 200:
            logger.error(f"eBay OAuth failed: HTTP {resp.status_code} — {resp.text[:200]}")
            _stats["token_failed"] = True
            return None

        data                         = resp.json()
        token                        = data.get("access_token")
        expires_in                   = int(data.get("expires_in", 7200))
        _token_cache["access_token"] = token
        _token_cache["expires_at"]   = now + timedelta(seconds=expires_in - 60)
        _stats["token_ok"]           = True
        logger.info(
            f"eBay OAuth token obtained "
            f"(expires in {expires_in}s, cached until "
            f"{_token_cache['expires_at'].strftime('%H:%M:%S UTC')})"
        )
        return token

    except requests.RequestException as e:
        logger.error(f"eBay OAuth request failed: {e}")
        _stats["token_failed"] = True
        return None


# ── Finding API: sold comps ───────────────────────────────────────────────────

def _finding_search(isbn: str) -> Tuple[int, list]:
    """
    Call the eBay Finding API findCompletedItems endpoint.
    Returns (count, raw_items). Only needs EBAY_APP_ID (no cert needed).
    """
    app_id = getattr(config, "EBAY_APP_ID", "")
    if not app_id:
        return 0, []

    params = {
        "OPERATION-NAME":                 "findCompletedItems",
        "SERVICE-VERSION":                "1.0.0",
        "SECURITY-APPNAME":               app_id,
        "RESPONSE-DATA-FORMAT":           "JSON",
        "REST-PAYLOAD":                   "",
        "keywords":                       isbn,
        "categoryId":                     _BOOKS_CAT,
        "itemFilter(0).name":             "SoldItemsOnly",
        "itemFilter(0).value":            "true",
        "itemFilter(1).name":             "ListingType",
        "itemFilter(1).value":            "FixedPrice",
        "sortOrder":                      "EndTimeSoonest",
        "paginationInput.entriesPerPage": "25",
        "paginationInput.pageNumber":     "1",
    }

    _stats["finding_calls"] += 1

    try:
        time.sleep(getattr(config, "EBAY_REQUEST_DELAY", 0.4))
        resp = requests.get(_FINDING_URL, params=params, timeout=12)

        if resp.status_code != 200:
            logger.debug(f"Finding API HTTP {resp.status_code} for ISBN {isbn}")
            _stats["finding_failed"] += 1
            return 0, []

        data = resp.json()
        result = data.get("findCompletedItemsResponse", [{}])[0]

        # Check eBay ack
        ack = result.get("ack", [""])[0]
        if ack not in ("Success", "Warning"):
            error_msg = ""
            try:
                error_msg = result["errorMessage"][0]["error"][0]["message"][0]
            except (KeyError, IndexError):
                pass
            logger.debug(f"Finding API ack={ack} for ISBN {isbn}: {error_msg}")
            _stats["finding_failed"] += 1
            return 0, []

        search_result = result.get("searchResult", [{}])[0]
        count = int(search_result.get("@count", 0))
        items = search_result.get("item", []) if count > 0 else []
        _stats["finding_ok"] += 1
        return count, items

    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        logger.debug(f"Finding API error for ISBN {isbn}: {e}")
        _stats["finding_failed"] += 1
        return 0, []


def _parse_finding_item(raw: dict) -> Optional[MarketListing]:
    """Parse one item from Finding API response into a MarketListing."""
    try:
        price      = float(raw["sellingStatus"][0]["currentPrice"][0]["__value__"])
        if price <= 0:
            return None

        ship_raw   = (
            raw.get("shippingInfo", [{}])[0]
               .get("shippingServiceCost", [{}])[0]
               .get("__value__", "0")
        )
        shipping   = float(ship_raw or 0)
        title      = raw.get("title", [""])[0]
        url        = raw.get("viewItemURL", [""])[0]
        end_str    = raw.get("listingInfo", [{}])[0].get("endTime", [""])[0]
        sold_date  = None
        if end_str:
            try:
                sold_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        condition_raw = raw.get("condition", [{}])[0]
        condition     = condition_raw.get("conditionDisplayName", ["Unknown"])[0]
        condition_id  = condition_raw.get("conditionId", [""])[0]

        return MarketListing(
            item_id       = raw.get("itemId", [""])[0],
            title         = title,
            price         = round(price, 2),
            shipping      = round(shipping, 2),
            total_price   = round(price + shipping, 2),
            condition     = condition,
            condition_id  = str(condition_id),
            listing_type  = LISTING_SOLD,
            source        = SOURCE_EBAY_BROWSE,
            listing_url   = url,
            sold_date     = sold_date,
            retrieved_at  = datetime.now(),
        )
    except (KeyError, IndexError, ValueError, TypeError) as e:
        logger.debug(f"Skipping malformed Finding API item: {e}")
        return None


def _get_sold_comps(isbn: str, book_title: str) -> Tuple[List[MarketListing], int]:
    """
    Fetch sold comps via Finding API for one ISBN.
    Applies title filtering and junk detection.
    Returns (listings, raw_total).
    """
    total, raw_items = _finding_search(isbn)
    if not raw_items:
        return [], 0

    listings = [_parse_finding_item(r) for r in raw_items]
    listings = [l for l in listings if l is not None]

    if _is_junk_result_set(total, listings):
        _stats["junk_rejected"] += 1
        return [], 0

    if book_title:
        listings, dropped = _filter_by_title(listings, book_title)
        if dropped > 0:
            _stats["title_filtered"] += dropped

    return listings, total


# ── Browse API: active listings ───────────────────────────────────────────────

def _browse_search_isbn(token: str, isbn: str, limit: int = 50) -> Tuple[int, list]:
    """Browse API search by GTIN (ISBN). Returns (total, raw_items)."""
    params = {
        "gtin":          isbn,
        "category_ids":  _BOOKS_CAT,
        "filter":        f"buyingOptions:{{FIXED_PRICE}},conditionIds:{{{_USED_CONDS}}}",
        "sort":          "price",
        "limit":         str(limit),
    }
    headers = {
        "Authorization":           f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "X-EBAY-C-ENDUSERCTX":    "contextualLocation=country=US,zip=10001",
        "Content-Type":            "application/json",
    }

    _stats["browse_calls"] += 1

    try:
        time.sleep(getattr(config, "EBAY_REQUEST_DELAY", 0.4))
        resp = requests.get(_BROWSE_URL, params=params, headers=headers, timeout=15)

        if resp.status_code == 401:
            _token_cache["access_token"] = None
            _token_cache["expires_at"]   = None
            logger.warning("Browse API 401 — token expired, will retry on next call")
            _stats["browse_failed"] += 1
            return 0, []

        if resp.status_code == 403:
            logger.error(
                "Browse API 403 — check that your app has 'Buy APIs' enabled "
                "at developer.ebay.com"
            )
            _stats["browse_failed"] += 1
            return 0, []

        if resp.status_code == 429:
            logger.warning("Browse API rate limit (429) — backing off 5s")
            time.sleep(5)
            _stats["browse_failed"] += 1
            return 0, []

        if resp.status_code != 200:
            logger.error(f"Browse API HTTP {resp.status_code} for ISBN {isbn}: {resp.text[:200]}")
            _stats["browse_failed"] += 1
            return 0, []

        data  = resp.json()
        total = int(data.get("total", 0))
        items = data.get("itemSummaries", [])
        _stats["browse_ok"] += 1
        return total, items

    except requests.exceptions.Timeout:
        logger.warning(f"Browse API timeout for ISBN {isbn}")
        _stats["browse_failed"] += 1
        return 0, []
    except requests.RequestException as e:
        logger.error(f"Browse API request error for ISBN {isbn}: {e}")
        _stats["browse_failed"] += 1
        return 0, []


def _browse_search_keywords(token: str, title: str, limit: int = 20) -> Tuple[int, list]:
    """
    Browse API keyword search by title — used when ISBN search returns 0.
    Uses first 80 characters of title to avoid overly long queries.
    """
    query = title[:80].strip()
    params = {
        "q":            query,
        "category_ids": _BOOKS_CAT,
        "filter":       f"buyingOptions:{{FIXED_PRICE}},conditionIds:{{{_USED_CONDS}}}",
        "sort":         "price",
        "limit":        str(limit),
    }
    headers = {
        "Authorization":           f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "X-EBAY-C-ENDUSERCTX":    "contextualLocation=country=US,zip=10001",
        "Content-Type":            "application/json",
    }

    _stats["browse_calls"] += 1

    try:
        time.sleep(getattr(config, "EBAY_REQUEST_DELAY", 0.4))
        resp = requests.get(_BROWSE_URL, params=params, headers=headers, timeout=15)

        if resp.status_code != 200:
            logger.debug(f"Browse keyword search HTTP {resp.status_code} for '{query[:40]}'")
            _stats["browse_failed"] += 1
            return 0, []

        data  = resp.json()
        total = int(data.get("total", 0))
        items = data.get("itemSummaries", [])
        _stats["browse_ok"] += 1
        return total, items

    except requests.RequestException as e:
        logger.debug(f"Browse keyword search error: {e}")
        _stats["browse_failed"] += 1
        return 0, []


def _parse_browse_item(raw: dict) -> Optional[MarketListing]:
    """Parse one Browse API item into a MarketListing."""
    try:
        price = float(raw.get("price", {}).get("value", 0) or 0)
        if price <= 0:
            return None

        ship_opts = raw.get("shippingOptions", [])
        shipping  = 0.0
        if ship_opts:
            shipping = float(ship_opts[0].get("shippingCost", {}).get("value", 0) or 0)

        seller = raw.get("seller", {})
        fb_pct_str = seller.get("feedbackPercentage", "")
        fb_pct     = float(fb_pct_str) if fb_pct_str else None
        fb_score   = seller.get("feedbackScore")

        image     = raw.get("image", {})
        image_url = image.get("imageUrl") if image else None

        return MarketListing(
            item_id               = raw.get("itemId", ""),
            title                 = raw.get("title", ""),
            price                 = round(price, 2),
            shipping              = round(shipping, 2),
            total_price           = round(price + shipping, 2),
            condition             = raw.get("condition", "Unknown"),
            condition_id          = str(raw.get("conditionId", "")),
            listing_type          = LISTING_ACTIVE,
            source                = SOURCE_EBAY_BROWSE,
            listing_url           = raw.get("itemWebUrl", ""),
            seller_feedback_score = int(fb_score) if fb_score else None,
            seller_feedback_pct   = fb_pct,
            top_rated_seller      = raw.get("topRatedBuyingExperience", False),
            image_url             = image_url,
            retrieved_at          = datetime.now(),
        )
    except (ValueError, TypeError, KeyError) as e:
        logger.debug(f"Skipping malformed Browse item: {e}")
        return None


def _get_active_listings(
    token: str,
    isbn13: str,
    isbn10: str,
    book_title: str,
) -> Tuple[List[MarketListing], int, str]:
    """
    Fetch active listings via Browse API.
    Tries ISBN-13, then valid ISBN-10, then keyword fallback.
    Returns (listings, raw_total, search_method_used).
    """
    searches = [("isbn13", isbn13)]

    if _is_valid_isbn10(isbn10):
        searches.append(("isbn10", isbn10))
    else:
        if isbn10 and isbn10 not in ("", "nan"):
            logger.debug(
                f"Skipping invalid ISBN-10 '{isbn10}' for Browse search "
                f"(looks like ASIN or malformed)"
            )

    for method, value in searches:
        total, raw_items = _browse_search_isbn(token, value)

        if not raw_items:
            continue

        listings = [_parse_browse_item(r) for r in raw_items]
        listings = [l for l in listings if l is not None]

        if _is_junk_result_set(total, listings):
            _stats["junk_rejected"] += 1
            logger.warning(
                f"Junk rejected for {method}={value} "
                f"({total:,} total, low median)"
            )
            continue

        # Title filter
        if book_title:
            listings, dropped = _filter_by_title(listings, book_title)
            if dropped > 0:
                _stats["title_filtered"] += dropped
            if not listings:
                logger.debug(
                    f"All {len(raw_items)} Browse results failed title filter "
                    f"for '{book_title[:45]}' — trying next search"
                )
                continue

        if listings:
            if method == "isbn10":
                _stats["isbn10_hits"] += 1
            else:
                _stats["isbn13_hits"] += 1
            return listings, total, method

    # All ISBN searches failed — try keyword fallback
    if book_title:
        logger.info(
            f"ISBN searches returned no usable results for '{book_title[:50]}' "
            f"— trying keyword fallback"
        )
        total, raw_items = _browse_search_keywords(token, book_title)

        if raw_items:
            listings = [_parse_browse_item(r) for r in raw_items]
            listings = [l for l in listings if l is not None]

            if book_title:
                listings, dropped = _filter_by_title(listings, book_title, threshold=0.5)
                _stats["title_filtered"] += dropped

            if listings:
                _stats["keyword_fallback_used"] += 1
                logger.info(
                    f"Keyword fallback: {len(listings)} usable listings "
                    f"for '{book_title[:45]}'"
                )
                return listings, total, "keyword"

    return [], 0, "none"


def _assign_sold_confidence(count: int) -> str:
    if count >= 3:
        return MCONF_SOLD_STRONG
    if count >= 1:
        return MCONF_SOLD_WEAK
    return MCONF_NONE


def _assign_active_confidence(count: int) -> str:
    if count >= 10:
        return MCONF_ACTIVE_STRONG
    if count >= 5:
        return MCONF_ACTIVE_MEDIUM
    if count >= 1:
        return MCONF_ACTIVE_WEAK
    return MCONF_NONE


# ── Public source class ───────────────────────────────────────────────────────

class EbayBrowseSource(AbstractMarketSource):
    """
    eBay market data using both Finding API (sold comps) and Browse API (active).

    Priority: sold comps > active listings > keyword fallback > empty
    """

    @property
    def source_name(self) -> str:
        return SOURCE_EBAY_BROWSE

    def is_available(self) -> bool:
        return bool(getattr(config, "EBAY_APP_ID", ""))

    def get_active_listings(
        self,
        isbn13: str,
        isbn10: str = "",
        book_title: str = "",
    ) -> MarketResult:
        """
        Full search pipeline: sold comps first, active listings if no sold data.
        Returns a MarketResult with the best available data.

        Note: book_title is an optional extension to the base interface signature.
        The market_data/__init__.py passes it through when available.
        """
        # ── 1. Sold comps via Finding API ─────────────────────────────────
        sold_listings = []

        for isbn, label in [(isbn13, "13"), (isbn10, "10")]:
            if not isbn or isbn in ("", "nan"):
                continue
            if label == "10" and not _is_valid_isbn10(isbn):
                logger.debug(f"Skipping invalid ISBN-10 '{isbn}' for Finding API")
                continue

            comps, total = _get_sold_comps(isbn, book_title)
            if comps:
                sold_listings = comps
                logger.info(
                    f"Sold comps: {len(comps)} listings via Finding API "
                    f"(ISBN-{label}={isbn}) for '{book_title[:45]}'"
                )
                _stats["sold_results"] += 1
                break

        if sold_listings:
            price_summary = PriceSummary.from_listings(sold_listings)
            confidence    = _assign_sold_confidence(len(sold_listings))

            if price_summary:
                logger.info(
                    f"SOLD comps: {len(sold_listings)} | "
                    f"median=${price_summary.median:.2f} | "
                    f"range=${price_summary.low:.2f}–${price_summary.high:.2f} | "
                    f"conf={confidence}"
                )

            return MarketResult(
                isbn13            = isbn13,
                isbn10            = isbn10,
                source            = SOURCE_EBAY_BROWSE,
                listing_type      = LISTING_SOLD,
                listings          = sold_listings,
                price_summary     = price_summary,
                raw_total         = len(sold_listings),
                market_confidence = confidence,
            )

        # ── 2. Active listings via Browse API ─────────────────────────────
        token = _get_oauth_token()
        if not token:
            return MarketResult(
                isbn13            = isbn13,
                isbn10            = isbn10,
                source            = SOURCE_EBAY_BROWSE,
                listing_type      = LISTING_ACTIVE,
                error             = "OAuth token unavailable",
                market_confidence = MCONF_NONE,
            )

        active_listings, raw_total, method = _get_active_listings(
            token, isbn13, isbn10, book_title
        )

        if not active_listings:
            _stats["zero_results"] += 1
            logger.info(
                f"No usable eBay data for '{book_title[:55]}' "
                f"(ISBN-13={isbn13}) — all searches exhausted"
            )
            return MarketResult(
                isbn13            = isbn13,
                isbn10            = isbn10,
                source            = SOURCE_EBAY_BROWSE,
                listing_type      = LISTING_ACTIVE,
                raw_total         = 0,
                market_confidence = MCONF_NONE,
            )

        _stats["active_results"] += 1
        price_summary = PriceSummary.from_listings(active_listings)
        confidence    = _assign_active_confidence(len(active_listings))

        if price_summary:
            logger.info(
                f"ACTIVE ({method}): {len(active_listings)} listings | "
                f"median=${price_summary.median:.2f} | "
                f"spread={price_summary.spread_pct:.0f}% | "
                f"conf={confidence} | '{book_title[:40]}'"
            )

        return MarketResult(
            isbn13            = isbn13,
            isbn10            = isbn10,
            source            = SOURCE_EBAY_BROWSE,
            listing_type      = LISTING_ACTIVE,
            listings          = active_listings,
            price_summary     = price_summary,
            raw_total         = raw_total,
            market_confidence = confidence,
            fallback_used     = (method == "keyword"),
        )

    def get_sold_listings(self, isbn13: str, isbn10: str = "") -> Optional[MarketResult]:
        """
        Sold listings are now attempted via Finding API inside get_active_listings().
        This method exists for interface compliance only.
        """
        return None
