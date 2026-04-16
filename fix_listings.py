#!/usr/bin/env python3
"""
Fix Existing Listings
Location : E:\\Book\\Lister\\fix_listings.py
Run once : python fix_listings.py

Does three things to every active atlas_commerce listing:
  1. Regenerates description with Claude AI  (+ keeps disclaimer)
  2. Updates handling time → 7 days via fulfillment policy
  3. Reprices to 12% undercut of current cheapest eBay competitor
"""

import os, json, time, logging, base64, re
from pathlib import Path
from datetime import datetime

import requests
import anthropic

# ── CONFIG ─────────────────────────────────────────────────────────────────────
EBAY_APP_ID        = os.environ["EBAY_APP_ID"]
EBAY_CERT_ID       = os.environ["EBAY_CERT_ID"]
EBAY_REFRESH_TOKEN = os.environ["EBAY_REFRESH_TOKEN"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

EBAY_FEE_RATE  = 0.153
UNDERCUT_PCT   = 0.12
MIN_PROFIT     = 1.00
HANDLING_DAYS  = 7

FULFILLMENT_POLICY = "391308514023"
PAYMENT_POLICY     = "391308491023"
RETURN_POLICY      = "391308498023"

BASE_DIR   = Path(__file__).parent
STATE_PATH = BASE_DIR / "lister_state.json"     # existing pipeline state
LOG_PATH   = BASE_DIR / "fix_listings.log"

DISCLAIMER = (
    "This item is sourced internationally to offer significant savings. "
    "Tracking information may not update until the package reaches the United States. "
    "All books are brand new, in mint condition, and carefully inspected before shipment."
)

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── AUTH ──────────────────────────────────────────────────────────────────────
def get_user_token() -> str:
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=(
            "grant_type=refresh_token"
            f"&refresh_token={EBAY_REFRESH_TOKEN}"
            "&scope=https://api.ebay.com/oauth/api_scope/sell.inventory"
            " https://api.ebay.com/oauth/api_scope/sell.account"
        ),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]

def get_app_token() -> str:
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]

# ── FULFILLMENT POLICY ────────────────────────────────────────────────────────
def update_handling_time(user_token: str):
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
    }
    r = requests.get(
        f"https://api.ebay.com/sell/account/v1/fulfillment_policy/{FULFILLMENT_POLICY}",
        headers=headers, timeout=15,
    )
    if r.status_code != 200:
        log.warning(f"GET fulfillment policy failed: {r.status_code}")
        return
    policy = r.json()
    current_ht = policy.get("handlingTime", {}).get("value")
    if current_ht == HANDLING_DAYS:
        log.info(f"Handling time already {HANDLING_DAYS}d – no change needed")
        return

    policy["handlingTime"] = {"unit": "DAY", "value": HANDLING_DAYS}
    r2 = requests.put(
        f"https://api.ebay.com/sell/account/v1/fulfillment_policy/{FULFILLMENT_POLICY}",
        headers=headers, json=policy, timeout=15,
    )
    if r2.status_code in (200, 204):
        log.info(f"✓ Fulfillment policy updated → {HANDLING_DAYS}-day handling")
    else:
        log.warning(f"PUT fulfillment policy failed: {r2.status_code} {r2.text[:150]}")

# ── FETCH ALL ACTIVE OFFERS ───────────────────────────────────────────────────
def fetch_all_offers(user_token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {user_token}"}
    offers, offset = [], 0
    while True:
        r = requests.get(
            "https://api.ebay.com/sell/inventory/v1/offer",
            headers=headers,
            params={"limit": 100, "offset": offset},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning(f"GET offers failed: {r.status_code}")
            break
        data   = r.json()
        batch  = data.get("offers", [])
        offers.extend(batch)
        total  = data.get("total", 0)
        offset += len(batch)
        if offset >= total or not batch:
            break
    log.info(f"Fetched {len(offers)} active offers from eBay")
    return offers

# ── IMAGE FETCHING ────────────────────────────────────────────────────────────
def is_real_image(url: str, min_bytes: int = 5000) -> bool:
    try:
        r = requests.get(url, timeout=12, stream=True)
        if r.status_code != 200:
            return False
        chunk = next(r.iter_content(chunk_size=min_bytes + 1), b"")
        r.close()
        is_jpg = chunk[:2] == b"\xff\xd8"
        is_png = chunk[:4] == b"\x89PNG"
        return (is_jpg or is_png) and len(chunk) >= min_bytes
    except Exception:
        return False

def get_book_image(isbn13: str, isbn10: str = "") -> str | None:
    """
    4-source image fetcher — same logic as lister.py.
    1. Open Library  2. Amazon CDN  3. Google Books  4. Library of Congress
    """
    # 1. Open Library
    for isbn in [isbn13, isbn10]:
        if not isbn:
            continue
        url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
        if is_real_image(url, min_bytes=5000):
            log.info(f"  Image: Open Library ({isbn})")
            return url

    # 2. Amazon CDN
    amazon_patterns = [
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn13}.01.LZZZZZZZ.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn13}.01._SX500_.jpg",
    ]
    if isbn10:
        amazon_patterns += [
            f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.LZZZZZZZ.jpg",
            f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01._SX500_.jpg",
        ]
    for url in amazon_patterns:
        if is_real_image(url, min_bytes=8000):
            log.info(f"  Image: Amazon CDN")
            return url

    # 3. Google Books
    try:
        r = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": f"isbn:{isbn13}", "maxResults": 3},
            timeout=8,
        )
        for item in r.json().get("items", []):
            img = item.get("volumeInfo", {}).get("imageLinks", {})
            for size_key, zoom in [("extraLarge", 3), ("large", 3), ("medium", 2), ("thumbnail", 1)]:
                src = img.get(size_key)
                if not src:
                    continue
                src = src.replace("http://", "https://")
                src = re.sub(r"zoom=\d", f"zoom={zoom}", src)
                if is_real_image(src, min_bytes=5000):
                    log.info(f"  Image: Google Books ({size_key})")
                    return src
    except Exception as e:
        log.warning(f"  Google Books failed for {isbn13}: {e}")

    # 4. Library of Congress
    try:
        r = requests.get(f"https://www.loc.gov/books/?q={isbn13}&fo=json", timeout=8)
        for result in r.json().get("results", [])[:3]:
            img_url = (result.get("image_url") or [None])[0]
            if img_url and is_real_image(img_url, min_bytes=5000):
                log.info(f"  Image: Library of Congress")
                return img_url
    except Exception as e:
        log.warning(f"  Library of Congress failed for {isbn13}: {e}")

    return None


# ── COMPS ─────────────────────────────────────────────────────────────────────
def get_comps(isbn: str, app_token: str) -> tuple[list[float], str]:
    headers = {
        "Authorization": f"Bearer {app_token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {
        "q": isbn, "category_ids": "267",
        "filter": "buyingOptions:{FIXED_PRICE},conditions:{NEW}",
        "sort": "price", "limit": "20",
    }
    try:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers, params=params, timeout=15
        )
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Browse API error {isbn}: {e}")
        return [], "NONE"
    prices = []
    for item in r.json().get("itemSummaries", []):
        try:
            prices.append(float(item["price"]["value"]))
        except (KeyError, ValueError):
            pass
    conf = "HIGH" if len(prices) >= 3 else "MEDIUM" if len(prices) >= 1 else "NONE"
    return prices, conf

# ── AI DESCRIPTION ────────────────────────────────────────────────────────────
import re

def clean_title(title: str) -> str:
    title = re.sub(r"\s*[-–]\s*(Hardcover|Paperback|Spiral Bound|Loose Leaf)\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*[\(\[{]?\s*ISBN[:\s]*[\d\-X]+\s*[\)\]}]?", "", title, flags=re.I)
    return title.strip(" –-")

def extract_format(title: str) -> str:
    if re.search(r"Hardcover", title, re.I):    return "Hardcover"
    if re.search(r"Spiral Bound", title, re.I): return "Spiral Bound"
    if re.search(r"Loose Leaf", title, re.I):   return "Loose Leaf"
    return "Paperback"

def generate_description(title: str, isbn: str) -> str:
    fmt   = extract_format(title)
    clean = clean_title(title)

    if not ANTHROPIC_API_KEY:
        return f"Brand new {fmt} copy of {clean} (ISBN {isbn}).\n\n{DISCLAIMER}"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Write a compelling, specific eBay book listing description.

Book: {clean}
ISBN: {isbn}
Format: {fmt}

Rules:
- 3–4 sentences ONLY
- Be specific about the subject and who benefits (students, practitioners, engineers, etc.)
- Mention it is a brand-new {fmt}
- Reference the edition/year if obvious from the title
- Do NOT start with the book title verbatim
- Do NOT use vague filler like "comprehensive", "essential", "thorough"
- Do NOT add any disclaimer or shipping note
- Plain text only – no markdown

Output ONLY the description text, nothing else."""

    try:
        resp = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        body = resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Claude error for {isbn}: {e}")
        body = f"Brand new {fmt} copy of {clean} (ISBN {isbn})."

    return f"{body}\n\n{DISCLAIMER}"

# ── UPDATE OFFER ──────────────────────────────────────────────────────────────
def update_offer_price(offer_id: str, new_price: float, user_token: str) -> bool:
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }
    payload = {
        "pricingSummary": {
            "price": {"value": f"{new_price:.2f}", "currency": "USD"}
        },
        "listingPolicies": {
            "fulfillmentPolicyId": FULFILLMENT_POLICY,
            "paymentPolicyId":     PAYMENT_POLICY,
            "returnPolicyId":      RETURN_POLICY,
        },
    }
    r = requests.put(
        f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}",
        headers=headers, json=payload, timeout=15,
    )
    return r.status_code in (200, 204)

def update_inventory_description(sku: str, title: str,
                                  description: str, fmt: str,
                                  user_token: str,
                                  image_url: str | None = None) -> bool:
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }
    clean = clean_title(title)[:80]
    product = {
        "title": clean,
        "description": description,
        "isbn": [sku],
        "aspects": {
            "Book Title": [clean],
            "ISBN":       [sku],
            "Format":     [fmt],
            "Language":   ["English"],
        },
    }
    if image_url:
        product["imageUrls"] = [image_url]

    payload = {
        "product": product,
        "condition": "NEW",
        "conditionDescription": "Brand new, unread copy.",
        "availability": {
            "shipToLocationAvailability": {"quantity": 10}
        },
    }
    r = requests.put(
        f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}",
        headers=headers, json=payload, timeout=15,
    )
    return r.status_code in (200, 204)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    log.info("=" * 70)
    log.info(f"fix_listings started | {datetime.now().isoformat()}")

    user_token = get_user_token()
    app_token  = get_app_token()

    # Step 1: Update fulfillment policy to 7-day handling
    update_handling_time(user_token)

    # Step 2: Load existing state (lister_state.json)
    lister_state = {}
    if STATE_PATH.exists():
        lister_state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    # Step 3: Fetch all offers from eBay
    offers = fetch_all_offers(user_token)

    updated = 0
    errors  = 0

    for offer in offers:
        offer_id = offer.get("offerId")
        sku      = offer.get("sku", "")           # ISBN is the SKU
        status   = offer.get("status", "")

        if status != "PUBLISHED":
            continue
        if not sku or len(sku) < 10:
            continue

        # Get title from lister_state if available, else use eBay title
        known   = lister_state.get(sku, {})
        title   = known.get("title", offer.get("listing", {}).get("title", sku))
        cost    = known.get("cost", 0.0)
        current_price = float(
            offer.get("pricingSummary", {}).get("price", {}).get("value", 0)
        )

        log.info(f"Processing {sku} | current_price=${current_price:.2f} | {title[:50]}")

        # Fetch fresh comps
        comps, conf = get_comps(sku, app_token)

        if comps:
            floor     = min(comps)
            new_price = round(floor * (1 - UNDERCUT_PCT), 2)
            net_profit = round(new_price * (1 - EBAY_FEE_RATE) - cost, 2)

            if net_profit < MIN_PROFIT:
                log.info(f"  → SKIP reprice: profit=${net_profit:.2f} too low")
                new_price = current_price   # keep existing price
            else:
                log.info(f"  → Reprice: ${current_price:.2f} → ${new_price:.2f}  "
                         f"profit=${net_profit:.2f}  conf={conf}")
        else:
            new_price = current_price
            log.info(f"  → No comps; keeping price ${current_price:.2f}")

        # Generate AI description
        fmt  = extract_format(title)
        desc = generate_description(title, sku)

        # Fetch fresh image using 4-source fetcher
        image_url = get_book_image(sku)
        if image_url:
            log.info(f"  → Image found")
        else:
            log.warning(f"  → No image found — inventory item will be updated without image")

        # Apply updates
        ok_desc  = update_inventory_description(sku, title, desc, fmt, user_token, image_url)
        ok_price = update_offer_price(offer_id, new_price, user_token)

        if ok_desc and ok_price:
            log.info(f"  ✓ Updated description + price")
            updated += 1
            # Patch lister_state
            if sku in lister_state:
                lister_state[sku]["sell_price"] = new_price
                lister_state[sku]["updated_at"] = datetime.now().isoformat()
        else:
            log.warning(f"  ✗ Partial failure: desc={ok_desc} price={ok_price}")
            errors += 1

        time.sleep(0.8)

    # Save updated state
    STATE_PATH.write_text(json.dumps(lister_state, indent=2), encoding="utf-8")

    log.info("-" * 70)
    log.info(f"Done: {updated} updated | {errors} errors")
    log.info("=" * 70)

if __name__ == "__main__":
    run()
