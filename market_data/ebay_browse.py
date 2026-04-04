"""
market_data/ebay_browse.py — eBay Browse API market data source.

═══════════════════════════════════════════════════════════════════════════════
ARCHITECTURE NOTE — WHY BROWSE API, NOT FINDING API
═══════════════════════════════════════════════════════════════════════════════

The legacy eBay Finding API (findCompletedItems / findItemsByKeywords) uses
App-ID-only authentication and an older XML-style endpoint. eBay has been
deprecating it since ~2022. The 181 failures in your scan are consistent
with eBay revoking Finding API access for new or inactive developer accounts.

The eBay Browse API is the current supported replacement:
    - Full OAuth 2.0 (Client Credentials flow)
    - RESTful JSON — clean and well-documented
    - Actively maintained, eBay's stated long-term direction
    - Supports GTIN/ISBN search natively

═══════════════════════════════════════════════════════════════════════════════
THE CRITICAL LIMITATION: NO SOLD DATA
═══════════════════════════════════════════════════════════════════════════════

This is the most important thing to understand about eBay's API landscape:

    Browse API         → Active listings only. No sold data.
    Finding API        → Deprecated. findCompletedItems is unreliable.
    Marketplace Insights API → DOES provide sold data, but requires special
                               access approval from eBay. It is not available
                               to standard developer accounts.

THE WORKAROUND USED HERE:
    1. Fetch active listings via Browse API (reliable, supported)
    2. Apply a configurable discount factor (default 10–15%) to convert
       active "asking prices" to estimated "sell prices"
    3. Label confidence as MEDIUM/LOW (not HIGH) to flag this limitation
    4. Log clearly when estimates are active-based, not sold-based

HOW TO GET SOLD DATA (future path):
    Apply for Marketplace Insights API access at developer.ebay.com.
    Email developer-relations@ebay.com with your use case. If approved,
    implement EbayInsightsSource (see amazon_stub.py for the pattern)
    and register it in __init__.py. No other code changes required.

═══════════════════════════════════════════════════════════════════════════════
CREDENTIALS REQUIRED
═══════════════════════════════════════════════════════════════════════════════

The Browse API uses OAuth 2.0 Client Credentials flow. You need TWO credentials
(unlike the Finding API which only needed the App ID):

    EBAY_APP_ID   — Client ID   (already in your .env and GitHub Secrets)
    EBAY_CERT_ID  — Client Secret (NEW — add to .env and GitHub Secrets)

Both are found at: developer.ebay.com → My Account → Application Keys
Use the PRODUCTION keys (the ones labeled "PRD", not "SBX").
The Client Secret is the long alphanumeric string in the "Cert ID" column.

═══════════════════════════════════════════════════════════════════════════════
TOKEN CACHING
═══════════════════════════════════════════════════════════════════════════════

OAuth tokens expire after 7200 seconds (2 hours). We cache the token
in memory and reuse it until 60 seconds before expiry. On GitHub Actions
each run is a fresh process so a new token is fetched each time (fine).
On Oracle Cloud the token is reused across multiple scans (efficient).
"""

import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

import requests

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_data.base import AbstractMarketSource
from market_data.models import (
    MarketListing, MarketResult, PriceSummary,
    LISTING_ACTIVE, SOURCE_EBAY_BROWSE,
    MCONF_ACTIVE_STRONG, MCONF_ACTIVE_MEDIUM, MCONF_ACTIVE_WEAK, MCONF_NONE,
)
import config

logger = logging.getLogger(__name__)

# ── eBay API endpoints ────────────────────────────────────────────────────────
_OAUTH_URL  = "https://api.ebay.com/identity/v1/oauth2/token"
_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay Books & Magazines category ID
_BOOKS_CATEGORY_ID = "267"

# eBay condition IDs for used books (exclude new to focus on used market comps)
# 1000=New, 2000=Excellent, 2500=Very Good, 3000=Good, 4000=Acceptable, 5000=Used
_USED_CONDITION_IDS = "2000|2500|3000|4000|5000"

# ── Module-level OAuth token cache ────────────────────────────────────────────
_token_cache: dict = {
    "access_token": None,
    "expires_at":   None,   # datetime
}

# ── Module-level run health counters (reset per scan via reset_stats()) ───────
_stats: dict = {
    "token_ok":      False,
    "token_failed":  False,
    "api_calls":     0,
    "api_ok":        0,
    "api_failed":    0,
    "zero_results":  0,
    "isbn13_hits":   0,
    "isbn10_hits":   0,
}


def reset_stats() -> None:
    """Reset health counters. Call at the start of each scan run."""
    global _stats, _token_cache
    _stats = {k: (False if isinstance(v, bool) else 0) for k, v in _stats.items()}
    # Don't reset token cache — reuse valid tokens across scans


def get_health_summary() -> dict:
    """
    Return a dict summarising eBay API usage for the current run.
    Used by scanner.py to determine run_mode and build email header.
    """
    s     = dict(_stats)
    total = s["api_calls"]

    s["success_rate"] = round(s["api_ok"] / total * 100, 1) if total else 0.0

    if not s["token_ok"] and s["token_failed"]:
        s["run_mode"]        = "FALLBACK_ONLY"
        s["run_mode_reason"] = "eBay OAuth token request failed — check EBAY_APP_ID and EBAY_CERT_ID"
    elif total == 0:
        s["run_mode"]        = "FALLBACK_ONLY"
        s["run_mode_reason"] = "No eBay API calls made — credentials not configured"
    elif s["api_failed"] > 0 and s["api_ok"] == 0:
        s["run_mode"]        = "FALLBACK_ONLY"
        s["run_mode_reason"] = f"All {s['api_failed']} eBay API calls failed"
    elif s["api_failed"] > 0 or s["zero_results"] > 0:
        s["run_mode"]        = "MIXED"
        s["run_mode_reason"] = (
            f"{s['api_ok']} eBay calls OK | "
            f"{s['api_failed']} failed | "
            f"{s['zero_results']} no results | "
            f"(active listings — not sold data)"
        )
    else:
        s["run_mode"]        = "EBAY_ACTIVE"   # Better label than EBAY_CONFIRMED
        s["run_mode_reason"] = (
            f"{s['api_ok']} eBay Browse API calls OK "
            f"(active listings — apply discount for sell-price estimate)"
        )

    # sold_successes kept for backward compat with scanner.py / notifier.py
    s["sold_successes"] = s["api_ok"]
    s["sold_failures"]  = s["api_failed"]
    s["api_key_missing"] = not bool(getattr(config, "EBAY_APP_ID", ""))

    return s


# ── OAuth 2.0 Client Credentials ──────────────────────────────────────────────

def _get_oauth_token() -> Optional[str]:
    """
    Return a valid OAuth access token, using cache when possible.

    Flow: POST to /identity/v1/oauth2/token with Basic Auth
          (base64 of "app_id:cert_id") and client_credentials grant.

    Returns the token string, or None if auth fails.
    """
    global _token_cache

    now = datetime.now(timezone.utc)
    if (
        _token_cache["access_token"]
        and _token_cache["expires_at"]
        and now < _token_cache["expires_at"]
    ):
        logger.debug("Using cached eBay OAuth token")
        return _token_cache["access_token"]

    app_id  = getattr(config, "EBAY_APP_ID",  "")
    cert_id = getattr(config, "EBAY_CERT_ID", "")

    if not app_id or not cert_id:
        missing = []
        if not app_id:  missing.append("EBAY_APP_ID")
        if not cert_id: missing.append("EBAY_CERT_ID")
        logger.error(
            f"Cannot get eBay OAuth token — missing credentials: {missing}. "
            f"Both EBAY_APP_ID and EBAY_CERT_ID are required for the Browse API. "
            f"Get EBAY_CERT_ID from developer.ebay.com → Application Keys → "
            f"Production 'Cert ID' column."
        )
        _stats["token_failed"] = True
        return None

    credentials = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()

    try:
        logger.debug("Requesting new eBay OAuth token")
        resp = requests.post(
            _OAUTH_URL,
            headers={
                "Authorization":  f"Basic {credentials}",
                "Content-Type":   "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope":      _OAUTH_SCOPE,
            },
            timeout=15,
        )

        if resp.status_code == 401:
            logger.error(
                "eBay OAuth 401 — credentials rejected. "
                "Verify EBAY_APP_ID and EBAY_CERT_ID are both from the "
                "PRODUCTION column at developer.ebay.com (not Sandbox)."
            )
            _stats["token_failed"] = True
            return None

        if resp.status_code != 200:
            logger.error(
                f"eBay OAuth failed: HTTP {resp.status_code} — {resp.text[:200]}"
            )
            _stats["token_failed"] = True
            return None

        data        = resp.json()
        token       = data.get("access_token")
        expires_in  = int(data.get("expires_in", 7200))

        _token_cache["access_token"] = token
        _token_cache["expires_at"]   = now + timedelta(seconds=expires_in - 60)
        _stats["token_ok"]           = True

        logger.info(
            f"eBay OAuth token obtained (expires in {expires_in}s, "
            f"cached until {_token_cache['expires_at'].strftime('%H:%M:%S UTC')})"
        )
        return token

    except requests.RequestException as e:
        logger.error(f"eBay OAuth token request failed: {e}")
        _stats["token_failed"] = True
        return None


# ── Browse API search ──────────────────────────────────────────────────────────

def _search_by_isbn(
    token: str,
    isbn: str,
    limit: int = 50,
) -> Tuple[int, list]:
    """
    Call the Browse API search endpoint for one ISBN.

    Returns (total_count, raw_item_list).
    total_count is the full result count from eBay (may exceed limit).
    """
    params = {
        "gtin":          isbn,            # GTIN = ISBN-13 (EAN-13 format)
        "category_ids":  _BOOKS_CATEGORY_ID,
        "filter":        f"buyingOptions:{{FIXED_PRICE}},conditionIds:{{{_USED_CONDITION_IDS}}}",
        "sort":          "price",         # Cheapest first — helps p25/median calc
        "limit":         str(limit),
    }

    headers = {
        "Authorization":            f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID":  "EBAY_US",
        "X-EBAY-C-ENDUSERCTX":      "contextualLocation=country=US,zip=10001",
        "Content-Type":             "application/json",
    }

    _stats["api_calls"] += 1

    try:
        resp = requests.get(
            _BROWSE_URL,
            params=params,
            headers=headers,
            timeout=15,
        )

        if resp.status_code == 401:
            # Token may have expired mid-run — clear cache so next call re-fetches
            _token_cache["access_token"] = None
            _token_cache["expires_at"]   = None
            logger.warning("eBay Browse API 401 — token expired mid-run, will retry")
            _stats["api_failed"] += 1
            return 0, []

        if resp.status_code == 403:
            logger.error(
                "eBay Browse API 403 Forbidden — "
                "App ID may not have Browse API access. "
                "Verify at developer.ebay.com that your production app "
                "has 'Buy APIs' enabled."
            )
            _stats["api_failed"] += 1
            return 0, []

        if resp.status_code == 429:
            logger.warning("eBay Browse API rate limit hit (429) — backing off 5s")
            time.sleep(5)
            _stats["api_failed"] += 1
            return 0, []

        if resp.status_code != 200:
            logger.error(
                f"eBay Browse API HTTP {resp.status_code} "
                f"for ISBN {isbn}: {resp.text[:300]}"
            )
            _stats["api_failed"] += 1
            return 0, []

        data  = resp.json()
        total = int(data.get("total", 0))
        items = data.get("itemSummaries", [])
        _stats["api_ok"] += 1
        return total, items

    except requests.exceptions.Timeout:
        logger.warning(f"eBay Browse API timeout for ISBN {isbn}")
        _stats["api_failed"] += 1
        return 0, []
    except requests.RequestException as e:
        logger.error(f"eBay Browse API request error for ISBN {isbn}: {e}")
        _stats["api_failed"] += 1
        return 0, []


def _parse_item(raw: dict) -> Optional[MarketListing]:
    """
    Parse one item from Browse API response into a MarketListing.
    Returns None if required fields are missing or price is zero.
    """
    try:
        # Price
        price_info = raw.get("price", {})
        price      = float(price_info.get("value", 0) or 0)
        if price <= 0:
            return None

        # Shipping (first available shipping option)
        shipping = 0.0
        shipping_options = raw.get("shippingOptions", [])
        if shipping_options:
            shipping_cost = shipping_options[0].get("shippingCost", {})
            shipping      = float(shipping_cost.get("value", 0) or 0)

        # Seller
        seller_info = raw.get("seller", {})
        fb_pct_str  = seller_info.get("feedbackPercentage", "")
        fb_pct      = float(fb_pct_str) if fb_pct_str else None
        fb_score    = seller_info.get("feedbackScore")

        # Condition
        condition    = raw.get("condition", "Unknown")
        condition_id = raw.get("conditionId", "")

        # Image
        image = raw.get("image", {})
        image_url = image.get("imageUrl") if image else None

        return MarketListing(
            item_id               = raw.get("itemId", ""),
            title                 = raw.get("title", ""),
            price                 = round(price, 2),
            shipping              = round(shipping, 2),
            total_price           = round(price + shipping, 2),
            condition             = condition,
            condition_id          = str(condition_id),
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
        logger.debug(f"Skipping malformed eBay item: {e} — raw: {str(raw)[:120]}")
        return None


def _assign_confidence(count: int) -> str:
    if count >= 10:
        return MCONF_ACTIVE_STRONG
    if count >= 5:
        return MCONF_ACTIVE_MEDIUM
    if count >= 1:
        return MCONF_ACTIVE_WEAK
    return MCONF_NONE


# ── Public interface ───────────────────────────────────────────────────────────

class EbayBrowseSource(AbstractMarketSource):
    """
    eBay market data via Browse API (active listings only).

    WHAT THIS PROVIDES:
        Currently listed used books on eBay.com, priced fixed-price only.
        Sorted by price ascending. Returns up to 50 listings per query.

    WHAT THIS DOES NOT PROVIDE:
        Sold/completed listing history. The Browse API has no endpoint for
        sold data. That requires the Marketplace Insights API (restricted).

    REVENUE ESTIMATE STRATEGY:
        Active listing median = typical asking price.
        profit_analyzer applies config.EBAY_ACTIVE_PRICE_DISCOUNT (default 10%)
        to convert asking price → estimated sell price. This accounts for:
            - Not all listed books sell
            - Price negotiation / Best Offer
            - Seasonality and demand fluctuations

    SEARCH STRATEGY:
        1. Try ISBN-13 via gtin= parameter (structured product lookup)
        2. If 0 results, try ISBN-10 via gtin= (some older listings use ISBN-10)
        3. If still 0, return empty result (profit_analyzer uses Amazon fallback)
    """

    @property
    def source_name(self) -> str:
        return SOURCE_EBAY_BROWSE

    def is_available(self) -> bool:
        return bool(
            getattr(config, "EBAY_APP_ID",  "") and
            getattr(config, "EBAY_CERT_ID", "")
        )

    def get_active_listings(self, isbn13: str, isbn10: str = "") -> MarketResult:
        """
        Fetch active eBay listings for a book by ISBN.
        Returns MarketResult (never raises).
        """
        # Authenticate
        token = _get_oauth_token()
        if not token:
            return MarketResult(
                isbn13       = isbn13,
                isbn10       = isbn10,
                source       = SOURCE_EBAY_BROWSE,
                listing_type = LISTING_ACTIVE,
                error        = "OAuth token unavailable — check EBAY_APP_ID and EBAY_CERT_ID",
                market_confidence = MCONF_NONE,
            )

        time.sleep(getattr(config, "EBAY_REQUEST_DELAY", 0.4))

        # Try ISBN-13 first, fall back to ISBN-10
        fallback_used = False
        isbns_to_try  = [(isbn13, False)]
        if isbn10 and isbn10 not in ("", "nan"):
            isbns_to_try.append((isbn10, True))

        total_count = 0
        raw_items   = []

        for isbn, is_fallback in isbns_to_try:
            if not isbn or isbn == "nan":
                continue
            logger.debug(
                f"eBay Browse search: ISBN {'10' if is_fallback else '13'}={isbn}"
            )
            total_count, raw_items = _search_by_isbn(token, isbn)
            if raw_items:
                fallback_used = is_fallback
                logger.info(
                    f"eBay Browse: {len(raw_items)}/{total_count} listings "
                    f"(ISBN{'10' if is_fallback else '13'}={isbn})"
                )
                break

        if not raw_items:
            _stats["zero_results"] += 1
            logger.info(
                f"eBay Browse: 0 results "
                f"(ISBN-13={isbn13}, ISBN-10={isbn10 or 'n/a'})"
            )
            return MarketResult(
                isbn13            = isbn13,
                isbn10            = isbn10,
                source            = SOURCE_EBAY_BROWSE,
                listing_type      = LISTING_ACTIVE,
                raw_total         = 0,
                market_confidence = MCONF_NONE,
                fallback_used     = fallback_used,
            )

        # Parse all items
        listings = [_parse_item(r) for r in raw_items]
        listings = [l for l in listings if l is not None]

        if is_fallback and raw_items:
            _stats["isbn10_hits"] += 1
        elif raw_items:
            _stats["isbn13_hits"] += 1

        price_summary = PriceSummary.from_listings(listings)
        confidence    = _assign_confidence(len(listings))

        if price_summary:
            logger.info(
                f"eBay Browse: {len(listings)} listings | "
                f"median=${price_summary.median:.2f} | "
                f"range=${price_summary.low:.2f}–${price_summary.high:.2f} | "
                f"spread={price_summary.spread_pct:.0f}% | "
                f"confidence={confidence}"
            )

        return MarketResult(
            isbn13            = isbn13,
            isbn10            = isbn10,
            source            = SOURCE_EBAY_BROWSE,
            listing_type      = LISTING_ACTIVE,
            listings          = listings,
            price_summary     = price_summary,
            raw_total         = total_count,
            market_confidence = confidence,
            fallback_used     = fallback_used,
        )

    def get_sold_listings(self, isbn13: str, isbn10: str = "") -> Optional[MarketResult]:
        """
        Sold listings are not available via Browse API.
        Returns None. To get sold data, apply for Marketplace Insights API access.
        """
        logger.debug(
            "get_sold_listings() called on EbayBrowseSource — "
            "sold data is not available via Browse API. "
            "Apply for Marketplace Insights API for sold comps."
        )
        return None
