#!/usr/bin/env python3
"""
test_ebay_api.py — Standalone diagnostic for the eBay Browse API.

Runs completely independently of the scanner. Use this to verify
your credentials before running a full scan.

USAGE:
    python3 test_ebay_api.py

WHAT IT TESTS:
    1. Credential presence (EBAY_APP_ID + EBAY_CERT_ID in .env)
    2. OAuth token request (auth works, credentials are valid)
    3. Live ISBN search  (Browse API returns real data)
    4. Response parsing  (data is usable for profit calculations)

EXPECTED OUTPUT (working):
    ✅ EBAY_APP_ID found
    ✅ EBAY_CERT_ID found
    ✅ OAuth token obtained (expires in 7200s)
    ✅ ISBN search returned 12 listings
       Cheapest: $45.00 | Median: $67.50 | Most expensive: $120.00
    ✅ All checks passed — Browse API is working

COMMON FAILURES:
    "EBAY_CERT_ID not found"
        → Add EBAY_CERT_ID to your .env file.
        → Get it from developer.ebay.com → Application Keys → Production → Cert ID

    "OAuth 401 Forbidden"
        → One or both credentials are wrong.
        → Confirm you're using PRODUCTION keys (labeled PRD), not Sandbox (SBX).

    "OAuth token obtained but 0 listings"
        → Credentials work but this specific ISBN has no listings.
        → Try a different ISBN or check that Books category is enabled.

    "Browse API 403 Forbidden"
        → Your app may not have Browse API access enabled.
        → Check developer.ebay.com → your app → API Access tab.
"""

import sys
import os
import base64
import json
from statistics import median

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("📄 .env file loaded")
except ImportError:
    print("ℹ️  python-dotenv not installed — reading env vars directly")

try:
    import requests
except ImportError:
    print("❌ requests not installed. Run: pip install requests")
    sys.exit(1)


# ── Test ISBN — a common textbook that reliably has eBay listings ─────────────
TEST_ISBN = "9780879396961"   # Fire and Emergency Services Instructor, 9th Ed
TEST_ISBN_NAME = "Fire and Emergency Services Instructor 9th Ed"

# ── eBay endpoints ────────────────────────────────────────────────────────────
OAUTH_URL  = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE      = "https://api.ebay.com/oauth/api_scope"


def header(text: str) -> None:
    print(f"\n{'─' * 55}")
    print(f"  {text}")
    print(f"{'─' * 55}")


def ok(msg: str) -> None:
    print(f"  ✅  {msg}")


def fail(msg: str) -> None:
    print(f"  ❌  {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠️   {msg}")


def info(msg: str) -> None:
    print(f"       {msg}")


# ── Step 1: Credentials ───────────────────────────────────────────────────────

header("Step 1 — Credentials")

app_id  = os.getenv("EBAY_APP_ID",  "")
cert_id = os.getenv("EBAY_CERT_ID", "")

passed = True

if app_id:
    ok(f"EBAY_APP_ID found: {app_id[:12]}...{app_id[-6:]}")
else:
    fail("EBAY_APP_ID not found in environment")
    info("Add to .env:  EBAY_APP_ID=YourClientIdHere")
    passed = False

if cert_id:
    ok(f"EBAY_CERT_ID found: {cert_id[:8]}...{cert_id[-4:]}")
else:
    fail("EBAY_CERT_ID not found in environment")
    info("Add to .env:  EBAY_CERT_ID=YourCertIdHere")
    info("Get it from: developer.ebay.com → Application Keys → Production → Cert ID")
    info("This is NEW — the old Finding API only needed EBAY_APP_ID.")
    info("The Browse API requires BOTH credentials.")
    passed = False

if not passed:
    print("\n❌ Cannot proceed — fix credentials first.\n")
    sys.exit(1)


# ── Step 2: OAuth token ───────────────────────────────────────────────────────

header("Step 2 — OAuth Token")

credentials = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()

try:
    resp = requests.post(
        OAUTH_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope":      SCOPE,
        },
        timeout=15,
    )

    if resp.status_code == 200:
        data       = resp.json()
        token      = data.get("access_token", "")
        expires_in = data.get("expires_in", "?")
        ok(f"Token obtained (expires in {expires_in}s)")
        info(f"Token preview: {token[:20]}...")
    elif resp.status_code == 401:
        fail("OAuth 401 — credentials rejected by eBay")
        info("Check that BOTH values come from the PRODUCTION column")
        info("(look for 'PRD' in the App ID, not 'SBX')")
        info(f"Response: {resp.text[:300]}")
        sys.exit(1)
    else:
        fail(f"OAuth failed: HTTP {resp.status_code}")
        info(f"Response: {resp.text[:300]}")
        sys.exit(1)

except requests.exceptions.ConnectionError:
    fail("Cannot reach api.ebay.com — check internet connection")
    sys.exit(1)
except requests.exceptions.Timeout:
    fail("OAuth request timed out (15s)")
    sys.exit(1)


# ── Step 3: ISBN search ───────────────────────────────────────────────────────

header(f"Step 3 — ISBN Search: {TEST_ISBN}")
info(f"Book: {TEST_ISBN_NAME}")

params = {
    "gtin":          TEST_ISBN,
    "category_ids":  "267",
    "filter":        "buyingOptions:{FIXED_PRICE},conditionIds:{2000|2500|3000|4000|5000}",
    "sort":          "price",
    "limit":         "50",
}

headers = {
    "Authorization":           f"Bearer {token}",
    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    "X-EBAY-C-ENDUSERCTX":    "contextualLocation=country=US,zip=10001",
    "Content-Type":            "application/json",
}

try:
    resp = requests.get(BROWSE_URL, params=params, headers=headers, timeout=15)

    if resp.status_code == 200:
        data         = resp.json()
        total        = data.get("total", 0)
        items        = data.get("itemSummaries", [])
        ok(f"Browse API responded: {len(items)} listings returned ({total} total on eBay)")
    elif resp.status_code == 403:
        fail("Browse API 403 Forbidden")
        info("Your app may not have Browse API access.")
        info("Check developer.ebay.com → your app → API Access tab.")
        info("You may need to request access to 'Buy APIs'.")
        sys.exit(1)
    elif resp.status_code == 401:
        fail("Browse API 401 — token rejected")
        info("Token was obtained but rejected on use — this is unusual.")
        info("Try regenerating credentials at developer.ebay.com.")
        sys.exit(1)
    else:
        fail(f"Browse API HTTP {resp.status_code}")
        info(f"Response: {resp.text[:400]}")
        sys.exit(1)

except requests.exceptions.Timeout:
    fail("Browse API request timed out")
    sys.exit(1)

if not items:
    warn("0 listings returned for this ISBN")
    info("The API is working but this ISBN has no active eBay listings.")
    info("This can happen with very specialised textbooks.")
    info("Try: python3 test_ebay_api.py  (modify TEST_ISBN at top of file)")
    print("\n⚠️  API is working but no results for test ISBN. Check manually.\n")
    sys.exit(0)


# ── Step 4: Parse and display ─────────────────────────────────────────────────

header("Step 4 — Parsed Results")

prices = []
print(f"\n  {'Title':<55}  {'Cond':<12}  {'Price':>7}  {'Ship':>6}  {'Total':>7}")
print(f"  {'─'*55}  {'─'*12}  {'─'*7}  {'─'*6}  {'─'*7}")

for item in items[:10]:
    title     = item.get("title", "")[:54]
    condition = item.get("condition", "Unknown")[:11]
    price_val = float(item.get("price", {}).get("value", 0) or 0)
    ship_opts = item.get("shippingOptions", [])
    ship_val  = 0.0
    if ship_opts:
        ship_raw = ship_opts[0].get("shippingCost", {}).get("value", 0)
        ship_val = float(ship_raw or 0)
    total = price_val + ship_val
    prices.append(total)
    print(f"  {title:<55}  {condition:<12}  ${price_val:>6.2f}  ${ship_val:>5.2f}  ${total:>6.2f}")

# All prices for stats
all_prices = []
for item in items:
    pv = float(item.get("price", {}).get("value", 0) or 0)
    sv_opts = item.get("shippingOptions", [])
    sv = float((sv_opts[0].get("shippingCost", {}).get("value", 0) or 0) if sv_opts else 0)
    t  = pv + sv
    if t > 0:
        all_prices.append(t)

if len(items) > 10:
    info(f"... and {len(items) - 10} more listings")

if all_prices:
    med = median(all_prices)
    discount = 0.10
    estimated_sell = round(med * (1.0 - discount), 2)

    print(f"\n  Price summary ({len(all_prices)} listings):")
    print(f"    Lowest:          ${min(all_prices):.2f}")
    print(f"    Median (asking): ${med:.2f}")
    print(f"    Highest:         ${max(all_prices):.2f}")
    print(f"    Spread:          {(max(all_prices)-min(all_prices))/med*100:.0f}%")
    print(f"\n  eBay sell price estimate (median × {1.0-discount:.2f} discount):")
    print(f"    Estimated sell:  ${estimated_sell:.2f}")

    print(f"\n  Arbitrage example (if this book costs ~$58 from supplier):")
    supplier_cost = 58.00
    ebay_fee      = estimated_sell * 0.1325
    profit        = estimated_sell - supplier_cost - ebay_fee
    margin        = profit / estimated_sell * 100 if estimated_sell else 0
    print(f"    Revenue estimate: ${estimated_sell:.2f}")
    print(f"    Supplier cost:    ${supplier_cost:.2f}")
    print(f"    eBay fee (13.25%): ${ebay_fee:.2f}")
    print(f"    Net profit:       ${profit:.2f}  ({margin:.0f}% margin)")


# ── Summary ───────────────────────────────────────────────────────────────────

header("Summary")
ok("EBAY_APP_ID ✓")
ok("EBAY_CERT_ID ✓")
ok("OAuth token ✓")
ok(f"Browse API ✓ — {len(items)} listings for test ISBN")
print()
print("  ✅ All checks passed — Browse API is working correctly.")
print("     The scanner will now use real eBay active listing data.")
print()
print("  IMPORTANT: Browse API provides ACTIVE listings, not SOLD comps.")
print("  Revenue estimates are: active median × 0.90 (10% discount applied).")
print("  All results are flagged ACTIVE_ONLY in scanner output by design.")
print()
