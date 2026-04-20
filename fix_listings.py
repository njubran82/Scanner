#!/usr/bin/env python3
"""
fix_listings.py — Scored, Capped eBay Lister
Location : E:\\Book\\Lister\\fix_listings.py

Pipeline:
  1. Score all books in booksgoat_enhanced.csv using profit, ROI,
     demand, confidence, spread stability, seasonality, days-listed decay
  2. Skip conf=NONE and profit < MIN_PROFIT ($12)
  3. Hard cap at MAX_LISTINGS (250) active listings
  4. Continuous replacement: if a new book scores above the lowest
     active listing and we are at cap, delist the lowest and list the new
  5. Cooldown: delisted books cannot be relisted for COOLDOWN_DAYS (14)
  6. Upsert eBay inventory item + create/update offer for selected books
  7. Write status, listed_at, score, sell_price back to CSV
"""

import os, json, time, logging, base64, re, csv, statistics
import requests
import anthropic
from math import log as mlog
from pathlib import Path
from datetime import datetime, timedelta

# ════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════
EBAY_APP_ID        = os.environ["EBAY_APP_ID"]
EBAY_CERT_ID       = os.environ["EBAY_CERT_ID"]
EBAY_REFRESH_TOKEN = os.environ["EBAY_REFRESH_TOKEN"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

EBAY_FEE_RATE   = 0.153
UNDERCUT_PCT    = 0.12
MIN_PROFIT      = 12.00       # minimum net profit to list

# Minimum-quantity books - cannot be purchased as single units on BooksGoat
MIN_QTY_BLOCKLIST = {
    "9781260460445",  # Lange Q&A Radiography Examination
    "9780990873853",  # Overcoming Gravity: Gymnastics — min qty 5
    "9781119826798",  # Architect's Studio Companion — PDF only on BooksGoat
}
MAX_LISTINGS  = 1000  # upgraded plan         # hard cap on active listings
COOLDOWN_DAYS   = 14          # days before a delisted book can be relisted
HANDLING_DAYS   = 7
MAX_ROI         = 5.0         # ROI cap to prevent outlier dominance

FULFILLMENT_POLICY = "391308514023"
PAYMENT_POLICY     = "391308491023"
RETURN_POLICY      = "391308498023"

BASE_DIR   = Path(__file__).parent
CSV_PATH   = BASE_DIR / "booksgoat_enhanced.csv"
STATE_PATH = BASE_DIR / "lister_state.json"
LOG_PATH   = BASE_DIR / "fix_listings.log"


# ISBNs that permanently fail eBay upsert
SKIP_ISBNS = {
    '9781609836184',
    '9780763781873',
    '9781951058067',
    '9781951058098',
    '9781597381000',
}

DISCLAIMER = (
    "This item is sourced internationally to offer significant savings. "
    "Tracking information may not update until the package reaches the United States. "
    "All books are brand new, in mint condition, and carefully inspected before shipment."
)

# ════════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# SCORING
# ════════════════════════════════════════════════════════════════
def score_book(
    profit: float,
    cost: float,
    comps: list[float],
    conf: str,
    listed_at: str | None,
) -> float:
    """
    Returns a score >= 0. Returns 0 if book should be skipped entirely.
    Higher score = higher priority for a listing slot.
    """
    if conf == "NONE" or profit < MIN_PROFIT:
        return 0.0

    # Profit (log scaled)
    profit_s = mlog(1 + profit)

    # ROI capped at MAX_ROI
    roi = min(profit / cost, MAX_ROI) if cost > 0 else 1.0

    # Demand (log scaled comps count)
    demand = mlog(1 + len(comps))

    # Confidence multiplier
    conf_w = {"HIGH": 1.0, "MEDIUM": 0.7}.get(conf, 0.0)

    # Spread penalty (wide spread = unstable market)
    if len(comps) >= 2:
        mean = statistics.mean(comps)
        std  = statistics.stdev(comps)
        spread_pen = max(0.7, 1.0 - (std / mean)) if mean > 0 else 0.7
    else:
        spread_pen = 0.85  # single comp — mild penalty

    # Seasonal multiplier
    month = datetime.now().month
    seasonal = 1.15 if month in [1, 2, 8, 9] else 1.0

    # Days-listed decay (tiered)
    decay = 1.0
    if listed_at:
        try:
            days = (datetime.now() - datetime.fromisoformat(listed_at)).days
            if days <= 14:   decay = 1.0
            elif days <= 30: decay = 0.9
            elif days <= 45: decay = 0.75
            elif days <= 60: decay = 0.6
            else:            decay = 0.4
        except Exception:
            decay = 1.0

    return profit_s * roi * demand * conf_w * spread_pen * seasonal * decay


# ════════════════════════════════════════════════════════════════
# AUTH
# ════════════════════════════════════════════════════════════════
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


# ════════════════════════════════════════════════════════════════
# FULFILLMENT POLICY
# ════════════════════════════════════════════════════════════════
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
        log.info(f"Handling time already {HANDLING_DAYS}d — no change needed")
        return
    policy["handlingTime"] = {"unit": "DAY", "value": HANDLING_DAYS}
    r2 = requests.put(
        f"https://api.ebay.com/sell/account/v1/fulfillment_policy/{FULFILLMENT_POLICY}",
        headers=headers, json=policy, timeout=15,
    )
    if r2.status_code in (200, 204):
        log.info(f"Fulfillment policy updated: {HANDLING_DAYS}-day handling")
    else:
        log.warning(f"PUT fulfillment policy failed: {r2.status_code} {r2.text[:150]}")


# ════════════════════════════════════════════════════════════════
# IMAGE
# ════════════════════════════════════════════════════════════════
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
    # 1. Open Library Covers API — try L, M sizes for both ISBN-13 and ISBN-10
    for isbn in [isbn13, isbn10]:
        if not isbn:
            continue
        for size in ["L", "M"]:
            url = f"https://covers.openlibrary.org/b/isbn/{isbn}-{size}.jpg"
            if is_real_image(url, min_bytes=3000):
                log.info(f"  Image: Open Library ({isbn}-{size})")
                return url

    # 2. Open Library Works API — sometimes has covers when ISBN endpoint doesn't
    try:
        r = requests.get(
            f"https://openlibrary.org/api/books",
            params={"bibkeys": f"ISBN:{isbn13}", "format": "json", "jscmd": "data"},
            timeout=8,
        )
        data = r.json().get(f"ISBN:{isbn13}", {})
        cover = data.get("cover", {})
        for size_key in ["large", "medium", "small"]:
            cover_url = cover.get(size_key)
            if cover_url and is_real_image(cover_url, min_bytes=3000):
                log.info(f"  Image: Open Library Works API ({size_key})")
                return cover_url
    except Exception:
        pass

    # 3. Amazon CDN patterns
    amazon_patterns = [
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn13}.01.LZZZZZZZ.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn13}.01._SX500_.jpg",
        f"https://m.media-amazon.com/images/I/{isbn13}.jpg",
    ]
    if isbn10:
        amazon_patterns += [
            f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.LZZZZZZZ.jpg",
            f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01._SX500_.jpg",
        ]
    for url in amazon_patterns:
        if is_real_image(url, min_bytes=5000):
            log.info(f"  Image: Amazon CDN")
            return url

    # 4. Google Books API
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
                if is_real_image(src, min_bytes=3000):
                    log.info(f"  Image: Google Books ({size_key})")
                    return src
    except Exception as e:
        log.warning(f"  Google Books failed for {isbn13}: {e}")

    # 5. Open Library by OCLC/OLID as last resort
    try:
        r = requests.get(
            f"https://openlibrary.org/search.json",
            params={"isbn": isbn13, "fields": "cover_i", "limit": 1},
            timeout=8,
        )
        docs = r.json().get("docs", [])
        if docs and docs[0].get("cover_i"):
            cover_id = docs[0]["cover_i"]
            url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
            if is_real_image(url, min_bytes=3000):
                log.info(f"  Image: Open Library cover ID ({cover_id})")
                return url
    except Exception:
        pass

    return None


# ════════════════════════════════════════════════════════════════
# COMPETITOR PRICING
# ════════════════════════════════════════════════════════════════
def get_comps(sku: str, app_token: str) -> tuple[list[float], str]:
    headers = {"Authorization": f"Bearer {app_token}"}
    try:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers,
            params={
                "q": sku,
                "filter": "buyingOptions:{FIXED_PRICE},conditions:{NEW}",
                "limit": 10,
            },
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Browse API error {sku}: {e}")
        return [], "NONE"
    prices = []
    for item in r.json().get("itemSummaries", []):
        try:
            prices.append(float(item["price"]["value"]))
        except (KeyError, ValueError):
            pass
    conf = "HIGH" if len(prices) >= 3 else "MEDIUM" if len(prices) >= 1 else "NONE"
    return sorted(prices), conf


# ════════════════════════════════════════════════════════════════
# AI DESCRIPTION
# ════════════════════════════════════════════════════════════════
def clean_title(title: str) -> str:
    title = re.sub(r"\s*[-–]\s*(Hardcover|Paperback|Spiral Bound|Loose Leaf)\s*$", "", title, flags=re.I)
    title = re.sub(r"\s*[\(\[{]?\s*ISBN[:\s]*[\d\-X]+\s*[\)\]}]?", "", title, flags=re.I)
    return title.strip(" –-")


def extract_format(title: str) -> str:
    if re.search(r"Hardcover", title, re.I):    return "Hardcover"
    if re.search(r"Spiral Bound", title, re.I): return "Spiral Bound"
    if re.search(r"Loose Leaf", title, re.I):   return "Loose Leaf"
    return "Paperback"


def extract_author(title: str) -> str:
    """
    Attempts to extract author from title strings like:
    'Book Title by Author Name (ISBN ...)'
    'Book Title by Author Name'
    Falls back to 'See Description' if not found.
    """
    m = re.search(r'\bby\s+([A-Z][^(\n,]{2,50})', title)
    if m:
        author = m.group(1).strip().rstrip(".,;")
        # Stop at ISBN or bracket
        author = re.split(r'\s*[\(\[{]', author)[0].strip()
        if author:
            return author
    return "See Description"


def extract_aspects(title: str, fmt: str, isbn: str) -> dict:
    """
    Builds the aspects dict required by eBay's Inventory API
    for the Books & Magazines category (267).
    Required fields vary by subcategory but including all of these
    prevents the most common 25002 errors.
    """
    clean = clean_title(title)[:80]
    author = extract_author(title)

    # Subject/Type — infer from title keywords
    subject = "Nonfiction"
    if re.search(r"novel|fiction|story|stories", title, re.I):
        subject = "Fiction"

    aspects = {
        "Book Title":        [clean],
        "Author":            [author],
        "Format":            [fmt],
        "Language":          ["English"],
        "Publication Name":  [clean],
        "Type":              [subject],
        "ISBN":              [isbn],
    }
    return aspects


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
- 3-4 sentences ONLY
- Be specific about the subject and who benefits
- Mention it is a brand-new {fmt}
- Reference edition/year if obvious from title
- Do NOT start with the book title verbatim
- Do NOT use vague filler like "comprehensive", "essential", "thorough"
- Do NOT add any disclaimer or shipping note
- Plain text only, no markdown

Output ONLY the description text, nothing else."""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        body = resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Claude error for {isbn}: {e}")
        body = f"Brand new {fmt} copy of {clean} (ISBN {isbn})."

    return f"{body}\n\n{DISCLAIMER}"


# ════════════════════════════════════════════════════════════════
# INVENTORY API
# ════════════════════════════════════════════════════════════════
def upsert_inventory_item(
    sku: str, title: str, description: str, fmt: str,
    user_token: str, image_url: str | None = None
) -> bool:
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }
    clean = clean_title(title)[:80]
    product = {
        "title": clean,
        "description": description,
        "aspects": extract_aspects(title, fmt, sku),
    }
    if image_url:
        product["imageUrls"] = [image_url]

    payload = {
        "product": product,
        "condition": "NEW",
        "conditionDescription": "Brand new, unread copy.",
        "availability": {
            "shipToLocationAvailability": {"quantity": 20}
        },
    }
    r = requests.put(
        f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}",
        headers=headers, json=payload, timeout=15,
    )
    if r.status_code not in (200, 204):
        log.warning(f"  upsert error {r.status_code}: {r.text[:300]}")
    return r.status_code in (200, 204)


def get_or_create_offer(sku: str, price: float, user_token: str) -> tuple[str | None, bool]:
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }
    r = requests.get(
        "https://api.ebay.com/sell/inventory/v1/offer",
        headers=headers,
        params={"sku": sku},
        timeout=15,
    )
    if False:  # Force new offer creation — merchantLocationKey only persists on POST
        offers = []
        if offers:
            offer_id = offers[0]["offerId"]
            r2 = requests.put(
                f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}",
                headers=headers,
                json={
                    "sku": sku,
                    "marketplaceId": "EBAY_US",
                    "format": "FIXED_PRICE",
                    "availableQuantity": 20,
                    "categoryId": "261186",
                    "listingPolicies": {
                        "fulfillmentPolicyId": FULFILLMENT_POLICY,
                        "paymentPolicyId":     PAYMENT_POLICY,
                        "returnPolicyId":      RETURN_POLICY,
                        "merchantLocationKey": "home1",
                    },
                    "pricingSummary": {
                        "price": {"value": str(round(price, 2)), "currency": "USD"}
                    },
                    "includeCatalogProductDetails": False,
                },
                timeout=15,
            )
            if r2.status_code not in (200, 204):
                log.warning(f"  update offer error {r2.status_code}: {r2.text[:200]}")
            return offer_id, False

    payload = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": 20,
        "categoryId": "261186",
        "listingPolicies": {
            "fulfillmentPolicyId": FULFILLMENT_POLICY,
            "paymentPolicyId":     PAYMENT_POLICY,
            "returnPolicyId":      RETURN_POLICY,
            "merchantLocationKey": "home1",
        },
        "pricingSummary": {
            "price": {"value": str(round(price, 2)), "currency": "USD"}
        },
    }
    r2 = requests.post(
        "https://api.ebay.com/sell/inventory/v1/offer",
        headers=headers, json=payload, timeout=15,
    )
    if r2.status_code in (200, 201):
        return r2.json().get("offerId"), True
    log.warning(f"  create offer error {r2.status_code}: {r2.text[:200]}")
    return None, False


def publish_offer(offer_id: str, user_token: str) -> bool:
    headers = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}
    r = requests.post(
        f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish",
        headers=headers, timeout=15,
    )
    if r.status_code not in (200, 201):
        try:
            log.warning(f"  publish_offer FAILED {r.status_code}: {r.json()}")
        except Exception:
            log.warning(f"  publish_offer FAILED {r.status_code}: {r.text[:300]}")
    return r.status_code in (200, 201)


def end_listing(offer_id: str, user_token: str) -> bool:
    headers = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}
    r = requests.delete(
        f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}",
        headers=headers, timeout=15,
    )
    return r.status_code in (200, 204)


# ════════════════════════════════════════════════════════════════
# CSV HELPERS
# ════════════════════════════════════════════════════════════════
CSV_FIELDS = [
    "isbn13", "title", "format", "cost", "product_url", "category_path",
    "sell_price", "status", "score", "listed_at", "sold_at",
    "delisted_at", "delist_reason", "checked_at", "offer_id",
]


def load_csv() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    # Ensure all fields exist
    for row in rows:
        for f in CSV_FIELDS:
            if f not in row:
                row[f] = ""
    return rows


def save_csv(rows: list[dict]):
    # Collect all field names present across all rows
    all_fields = list(dict.fromkeys(CSV_FIELDS + [k for r in rows for k in r]))
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

AMAZON_CAP_PCT = 0.95         # never list above 95% of Amazon list price

def build_amazon_lookup() -> dict:
    """Fetch BooksGoat feed and return isbn13 -> amazon_price mapping."""
    url = os.environ.get("BOOKSGOAT_CSV_URL", "")
    if not url:
        log.warning("BOOKSGOAT_CSV_URL not set — Amazon cap disabled")
        return {}
    try:
        import io
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        lookup = {}
        for row in reader:
            isbn = row.get("ISBN-13", "").strip().replace("-", "")
            price_raw = row.get("Amazon Price", "").strip().replace("$", "").replace(",", "")
            try:
                price = float(price_raw)
                if price > 0 and isbn:
                    lookup[isbn] = price
            except (ValueError, TypeError):
                pass
        log.info(f"Amazon lookup built: {len(lookup)} prices loaded")
        return lookup
    except Exception as ex:
        log.warning(f"Amazon lookup failed: {ex}")
        return {}

def run():
    log.info("=" * 70)
    log.info(f"fix_listings started | {datetime.now().isoformat()}")

    user_token = get_user_token()
    app_token  = get_app_token()

    update_handling_time(user_token)

    rows = load_csv()
    amazon_lookup = build_amazon_lookup()
    if not rows:
        log.error(f"CSV not found or empty: {CSV_PATH}")
        return

    log.info(f"Loaded {len(rows)} books from CSV")

    # Index rows by ISBN for fast lookup
    row_index = {r["isbn13"]: r for r in rows if r.get("isbn13")}
    now = datetime.now()
    now_iso = now.isoformat()

    # ── PHASE 1: Score all candidates ──────────────────────────
    log.info("Phase 1: Scoring all candidates...")
    scored = []  # (score, sku, comps, conf, new_price, net_profit)

    for row in rows:
        sku   = row.get("isbn13", "").strip()
        title = row.get("title", "").strip()
        cost  = float(row.get("cost", 0) or 0)
        status = row.get("status", "").strip()

        if not sku or len(sku) < 10:
            continue

        # Skip cooldown
        if status == "delisted":
            delisted_at = row.get("delisted_at", "")
            if delisted_at:
                try:
                    days_since = (now - datetime.fromisoformat(delisted_at)).days
                    if days_since < COOLDOWN_DAYS:
                        log.info(f"  COOLDOWN {sku} ({days_since}d / {COOLDOWN_DAYS}d)")
                        continue
                except Exception:
                    pass

        comps, conf = get_comps(sku, app_token)

        if not comps:
            new_price  = float(row.get("sell_price", 0) or 0) or round(cost * 1.3, 2)
            net_profit = round(new_price * (1 - EBAY_FEE_RATE) - cost, 2)
        else:
            floor      = min(comps)
            new_price  = round(floor * (1 - UNDERCUT_PCT), 2)
            net_profit = round(new_price * (1 - EBAY_FEE_RATE) - cost, 2)

        # Apply Amazon price cap
        amazon_ref = amazon_lookup.get(sku, 0)
        if amazon_ref > 0:
            amazon_cap = round(amazon_ref * AMAZON_CAP_PCT, 2)
            if new_price > amazon_cap:
                log.info(f"  Amazon cap applied: ${new_price} -> ${amazon_cap} (Amazon=${amazon_ref})")
                new_price  = amazon_cap
                net_profit = round(new_price * (1 - EBAY_FEE_RATE) - cost, 2)

        if new_price <= 0:
            new_price = round(cost * 1.3, 2)

        listed_at = row.get("listed_at", "") if status == "active" else None
        s = score_book(net_profit, cost, comps, conf, listed_at)

        row["_score"]      = s
        row["_comps"]      = comps
        row["_conf"]       = conf
        row["_new_price"]  = new_price
        row["_net_profit"] = net_profit

        if s > 0:
            scored.append((s, sku))

    # Sort all candidates by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # ── PHASE 2: Determine active set ──────────────────────────
    currently_active = [r for r in rows if r.get("status") == "active"]
    active_count = len(currently_active)
    log.info(f"Currently active: {active_count} / {MAX_LISTINGS}")

    # Entry threshold: median score of active listings (or 0 if none)
    active_scores = [float(r.get("score", 0) or 0) for r in currently_active]
    entry_threshold = statistics.median(active_scores) if active_scores else 0.0
    log.info(f"Entry threshold (median active score): {entry_threshold:.4f}")

    # ── PHASE 3: Replacement logic ──────────────────────────────
    # Find lowest-scoring active listing for replacement comparisons
    if currently_active:
        lowest_active = min(currently_active, key=lambda r: float(r.get("score", 0) or 0))
        lowest_score  = float(lowest_active.get("score", 0) or 0)
        lowest_sku    = lowest_active.get("isbn13", "")
    else:
        lowest_active = None
        lowest_score  = 0.0
        lowest_sku    = ""

    to_list   = []  # ISBNs to list this run
    to_delist = []  # ISBNs to delist this run (replaced by better candidates)

    for s, sku in scored:
        row = row_index.get(sku, {})
        status = row.get("status", "")

        if status == "active":
            # Already active — will be repriced/refreshed
            to_list.append(sku)
            continue

        # New candidate — check if it qualifies
        if False:  # SCORING DISABLED — 1000-listing plan, list all profitable
            log.info(f"  SKIP {sku} score={s:.4f} <= threshold={entry_threshold:.4f}")
            continue

        if True:  # SCORING DISABLED
            # Slot available
            to_list.append(sku)
            active_count += 1
        elif lowest_active and s > lowest_score:
            # Replace lowest active with this better candidate
            log.info(f"  REPLACE {lowest_sku} (score={lowest_score:.4f}) with {sku} (score={s:.4f})")
            to_delist.append(lowest_sku)
            to_list.append(sku)
            # Update lowest for next iteration
            remaining_active = [r for r in currently_active
                                 if r.get("isbn13") not in to_delist]
            if remaining_active:
                lowest_active = min(remaining_active, key=lambda r: float(r.get("score", 0) or 0))
                lowest_score  = float(lowest_active.get("score", 0) or 0)
                lowest_sku    = lowest_active.get("isbn13", "")

    log.info(f"To list: {len(to_list)} | To delist: {len(to_delist)}")

    # ── PHASE 4: Delist replaced books ─────────────────────────
    for sku in to_delist:
        row = row_index.get(sku, {})
        offer_id = row.get("offer_id", "")
        if offer_id:
            ok = end_listing(offer_id, user_token)
            log.info(f"  Delisted {sku}: {ok}")
        row["status"]       = "delisted"
        row["delisted_at"]  = now_iso
        row["delist_reason"] = "replaced_by_higher_score"
        time.sleep(0.3)

    # ── PHASE 5: List/update selected books ────────────────────
    listed  = 0
    updated = 0
    errors  = 0

    for sku in to_list:
        if sku in SKIP_ISBNS:
            log.info(f"  SKIP_PERMANENT {sku}")
            continue
        row = row_index.get(sku, {})
        title      = row.get("title", "").strip()
        cost       = float(row.get("cost", 0) or 0)
        new_price  = float(row.get("_new_price", 0) or 0)
        # Re-apply Amazon cap at upsert time
        _amz = amazon_lookup.get(sku, 0)
        if _amz > 0 and new_price > round(_amz * AMAZON_CAP_PCT, 2):
            new_price = round(_amz * AMAZON_CAP_PCT, 2)
        net_profit = float(row.get("_net_profit", 0) or 0)
        conf       = row.get("_conf", "NONE")
        s          = float(row.get("_score", 0) or 0)
        status     = row.get("status", "")

        log.info(f"Processing {sku} | profit=${net_profit:.2f} | score={s:.4f} | {title[:45]}")

        fmt       = extract_format(title)
        cached_desc = row.get("description", "").strip()
        if cached_desc:
            desc = cached_desc
            log.info(f"  Description: cached")
        else:
            desc = generate_description(title, sku)
            row["description"] = desc
        image_url = get_book_image(sku)
        if not image_url:
            log.warning(f"  No image found")

        ok_item = upsert_inventory_item(sku, title, desc, fmt, user_token, image_url)
        if not ok_item:
            errors += 1
            time.sleep(0.5)
            continue

        # Refresh token every 50 books to prevent expiry
        if listed + updated + errors > 0 and (listed + updated + errors) % 50 == 0:
            log.info("  Refreshing OAuth token...")
            user_token = get_user_token()

        offer_id, is_new = get_or_create_offer(sku, new_price, user_token)
        if not offer_id:
            errors += 1
            time.sleep(0.5)
            continue

        ok_pub = publish_offer(offer_id, user_token)
        log.info(f"  Published: {ok_pub}")
        if is_new:
            listed += 1
        else:
            updated += 1

        log.info(f"  OK — ${new_price:.2f} | profit=${net_profit:.2f} | conf={conf}")

        # Update row
        row["status"]     = "active"
        row["sell_price"] = str(round(new_price, 2))
        row["score"]      = str(round(s, 6))
        row["offer_id"]   = offer_id or ""
        row["checked_at"] = now_iso
        if not row.get("listed_at") or status != "active":
            row["listed_at"] = now_iso

        time.sleep(0.8)

    # ── PHASE 6: Save CSV ───────────────────────────────────────
    # Clean up temp keys
    for row in rows:
        for k in ["_score", "_comps", "_conf", "_new_price", "_net_profit"]:
            row.pop(k, None)

    save_csv(rows)

    log.info("-" * 70)
    log.info(f"Done: {listed} new listed | {updated} repriced | {len(to_delist)} delisted | {errors} errors")
    log.info(f"Active slots used: {len([r for r in rows if r.get('status')=='active'])} / {MAX_LISTINGS}")
    log.info("=" * 70)


if __name__ == "__main__":
    run()









