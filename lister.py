#!/usr/bin/env python3
"""
lister.py v2.4 — Lists opportunities from scan_opportunities.json
Uses full_publish logic: PUT inventory item → DELETE stale offer →
POST fresh offer WITH merchantLocationKey → publish.
Updates booksgoat_enhanced.csv with status=active, offer_id, sell_price.
Includes AI description generation and protection_patch support.

Fixes applied:
  v2.2 — get_cover_image: use GET+stream instead of HEAD+Content-Length
  v2.2 — create_offer: includeCatalogProductDetails=False
  v2.3 — ensure_inventory_item: add Author aspect (required by eBay Books)
  v2.3 — publish_offer: safe JSON parsing, no crash on empty body
  v2.4 — Ported full 5-source image fallback chain from fix_listings.py
  v2.4 — Failure email: detailed report with error cause + corrective action
  v2.4 — Synced BLOCKLIST with scanner.py

Run: GitHub Actions scanner.yml after scanner.py
"""

import os, csv, json, base64, time, logging, requests, smtplib, sys, re
from datetime import datetime, timezone
from pathlib import Path
from email.mime.text import MIMEText

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

try:
    from protection_patch import is_protected
except ImportError:
    def is_protected(row): return str(row.get('protected', '')).lower() == 'true'

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
ANTHROPIC_API_KEY  = os.getenv('ANTHROPIC_API_KEY', '')
SMTP_HOST          = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT          = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER          = os.getenv('SMTP_USER', '')
SMTP_PASSWORD      = os.getenv('SMTP_PASSWORD', '')
EMAIL_FROM         = os.getenv('EMAIL_FROM', SMTP_USER)
EMAIL_TO           = os.getenv('EMAIL_TO', SMTP_USER)

FULFILLMENT_POLICY = '391308514023'
PAYMENT_POLICY     = '391308491023'
RETURN_POLICY      = '391308498023'
QUANTITY           = 20
CATEGORY_ID        = '261186'
CSV_PATH           = Path('booksgoat_enhanced.csv')
LOG_FILE           = 'lister_log.txt'

# Synced with scanner.py BLOCKLIST
BLOCKLIST = {
    '9781119143642',  # Understanding Behaviorism — min qty 5
    '9781260460445',  # Lange Q&A Radiography — min qty 5
    '9780990873853',  # Overcoming Gravity — min qty 5
    '9781119826798',  # Architect's Studio Companion — PDF only
    '9781628257830',  # Process Groups — min qty 5
    '9780415708234',  # Healing the Fragmented Selves — min qty 6
    '9781591264507',  # PPI FE Electrical and Computer — min qty 5
    '9780091816971',  # Who Moved My Cheese — min qty 50
    '9781108724265',  # Trustworthy Online Controlled Experiments — min qty 5
    '9780415898058',  # Even if it Costs Me My Life — min qty 5
    '9781118115121',  # Art and Science of Technical Analysis — min qty 5
    '9780393979503',  # C Programming: A Modern Approach — download only
    '9780973501827',  # Back Mechanic — min qty 10
    '9780357622957',  # Theory and Practice of Group Counseling — min qty 5
}

CLOSING_STATEMENT = (
    "This item is sourced internationally to offer significant savings. "
    "Tracking information may not update until the package reaches the United States. "
    "All books are brand new, in mint condition, and carefully inspected before shipment."
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── Auth ───────────────────────────────────────────────────────────────────
def get_user_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type':    'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': ' '.join([
                'https://api.ebay.com/oauth/api_scope',
                'https://api.ebay.com/oauth/api_scope/sell.inventory',
                'https://api.ebay.com/oauth/api_scope/sell.account',
            ])
        }
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Token error: {data}')
    return data['access_token']


# ── CSV helpers ────────────────────────────────────────────────────────────
def load_csv() -> dict:
    if not CSV_PATH.exists():
        return {}
    rows = {}
    with CSV_PATH.open(encoding='utf-8') as f:
        for row in csv.DictReader(f):
            isbn = row.get('isbn13', '').strip()
            if isbn:
                rows[isbn] = row
    return rows

def save_csv(rows: dict):
    all_rows   = list(rows.values())
    all_fields = list(dict.fromkeys(k for r in all_rows for k in r))
    tmp = CSV_PATH.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(all_rows)
    tmp.replace(CSV_PATH)


# ── AI description ─────────────────────────────────────────────────────────
def generate_description(title: str, isbn: str) -> str:
    if not ANTHROPIC_API_KEY:
        return f"{title}\n\nISBN-13: {isbn}"
    try:
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':      'claude-haiku-4-5-20251001',
                'max_tokens': 300,
                'messages': [{
                    'role':    'user',
                    'content': (
                        f"Write a concise, compelling eBay listing description for this book:\n"
                        f"Title: {title}\nISBN-13: {isbn}\n\n"
                        f"2-3 sentences max. Highlight what the book covers and who it's for. "
                        f"Do not mention price, shipping, or condition. Plain text only."
                    )
                }],
            },
            timeout=15,
        )
        if r.status_code == 200:
            content = r.json().get('content', [])
            if content and content[0].get('type') == 'text':
                return content[0]['text'].strip()
    except Exception as e:
        log.warning(f'  AI description failed: {e}')
    return f"{title}\n\nISBN-13: {isbn}"


# ── ISBN-10 conversion ────────────────────────────────────────────────────
def isbn13_to_isbn10(isbn13: str) -> str:
    """Convert ISBN-13 (978...) to ISBN-10. Returns '' if not convertible."""
    if not isbn13 or not isbn13.startswith('978') or len(isbn13) != 13:
        return ''
    body = isbn13[3:12]
    try:
        total = sum(int(d) * (10 - i) for i, d in enumerate(body))
        check = (11 - (total % 11)) % 11
        return body + ('X' if check == 10 else str(check))
    except (ValueError, IndexError):
        return ''


# ── Cover image — full 5-source fallback chain ────────────────────────────
def is_real_image(url: str, min_bytes: int = 5000) -> bool:
    """Validate that URL returns an actual JPEG/PNG image of sufficient size."""
    try:
        r = requests.get(url, timeout=12, stream=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return False
        chunk = next(r.iter_content(chunk_size=min_bytes + 1), b"")
        r.close()
        is_jpg = chunk[:2] == b"\xff\xd8"
        is_png = chunk[:4] == b"\x89PNG"
        return (is_jpg or is_png) and len(chunk) >= min_bytes
    except Exception:
        return False


def get_book_image(isbn13: str, isbn10: str = "", title: str = "") -> tuple[str | None, str]:
    """
    Full image fallback chain with thumbnail acceptance.
    Returns (url, quality) where quality is 'full', 'thumbnail', or 'none'.
    Sources 1-6 try for high-quality images (3000+ bytes).
    Source 7 retries with thumbnail acceptance (500+ bytes) as last resort.
    """
    # ── HIGH-QUALITY PASS (sources 1-6) ────────────────────────

    # 1. Open Library Covers API — try L, M sizes for both ISBN-13 and ISBN-10
    for isbn in [isbn13, isbn10]:
        if not isbn:
            continue
        for size in ["L", "M"]:
            url = f"https://covers.openlibrary.org/b/isbn/{isbn}-{size}.jpg"
            if is_real_image(url, min_bytes=3000):
                log.info(f"  Image: Open Library ({isbn}-{size})")
                return url, 'full'

    # 2. Open Library Works API — sometimes has covers when ISBN endpoint doesn't
    try:
        r = requests.get(
            "https://openlibrary.org/api/books",
            params={"bibkeys": f"ISBN:{isbn13}", "format": "json", "jscmd": "data"},
            timeout=8,
        )
        data = r.json().get(f"ISBN:{isbn13}", {})
        cover = data.get("cover", {})
        for size_key in ["large", "medium", "small"]:
            cover_url = cover.get(size_key)
            if cover_url and is_real_image(cover_url, min_bytes=3000):
                log.info(f"  Image: Open Library Works API ({size_key})")
                return cover_url, 'full'
    except Exception:
        pass

    # 3. Amazon CDN patterns — 5 URL patterns across ISBN-13 and ISBN-10
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
            return url, 'full'

    # 4. Google Books API — ISBN search with zoom for higher resolution
    try:
        r = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": f"isbn:{isbn13}", "maxResults": 3},
            timeout=8,
        )
        for item in r.json().get("items", []):
            img = item.get("volumeInfo", {}).get("imageLinks", {})
            for size_key, zoom in [("extraLarge", 3), ("large", 3), ("medium", 2)]:
                src = img.get(size_key)
                if not src:
                    continue
                src = src.replace("http://", "https://")
                src = re.sub(r"zoom=\d", f"zoom={zoom}", src)
                if is_real_image(src, min_bytes=3000):
                    log.info(f"  Image: Google Books ({size_key})")
                    return src, 'full'
    except Exception as e:
        log.warning(f"  Google Books failed for {isbn13}: {e}")

    # 5. Open Library search by cover ID
    try:
        r = requests.get(
            "https://openlibrary.org/search.json",
            params={"isbn": isbn13, "fields": "cover_i", "limit": 1},
            timeout=8,
        )
        docs = r.json().get("docs", [])
        if docs and docs[0].get("cover_i"):
            cover_id = docs[0]["cover_i"]
            url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
            if is_real_image(url, min_bytes=3000):
                log.info(f"  Image: Open Library cover ID ({cover_id})")
                return url, 'full'
    except Exception:
        pass

    # 6. Google Books API — title search fallback
    if title:
        try:
            search_title = re.sub(r'\s*[-–]\s*(Hardcover|Paperback|Spiral|Loose)', '', title, flags=re.I)
            search_title = re.sub(r'[^\w\s]', ' ', search_title).strip()[:80]
            if search_title:
                r = requests.get(
                    "https://www.googleapis.com/books/v1/volumes",
                    params={"q": f"intitle:{search_title}", "maxResults": 5},
                    timeout=8,
                )
                for item in r.json().get("items", []):
                    img = item.get("volumeInfo", {}).get("imageLinks", {})
                    for size_key, zoom in [("extraLarge", 3), ("large", 3), ("medium", 2)]:
                        src = img.get(size_key)
                        if not src:
                            continue
                        src = src.replace("http://", "https://")
                        src = re.sub(r"zoom=\d", f"zoom={zoom}", src)
                        if is_real_image(src, min_bytes=3000):
                            log.info(f"  Image: Google Books title search ({size_key})")
                            return src, 'full'
        except Exception as e:
            log.warning(f"  Google Books title search failed: {e}")

    # ── THUMBNAIL PASS (source 7) ──────────────────────────────
    # Accept any image >= 500 bytes as last resort. These get listed but
    # flagged for manual replacement. Better than no listing at all.
    log.info(f"  No full-size image found — trying thumbnail pass...")

    # 7a. Google Books ISBN search — accept thumbnails
    try:
        r = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": f"isbn:{isbn13}", "maxResults": 3},
            timeout=8,
        )
        for item in r.json().get("items", []):
            img = item.get("volumeInfo", {}).get("imageLinks", {})
            for size_key in ["medium", "thumbnail", "smallThumbnail"]:
                src = img.get(size_key)
                if not src:
                    continue
                src = src.replace("http://", "https://").replace("&edge=curl", "")
                if is_real_image(src, min_bytes=500):
                    log.warning(f"  Image: THUMBNAIL from Google Books ISBN ({size_key}) — flag for upgrade")
                    return src, 'thumbnail'
    except Exception:
        pass

    # 7b. Google Books title search — accept thumbnails
    if title:
        try:
            search_title = re.sub(r'[^\w\s]', ' ', title).strip()[:80]
            if search_title:
                r = requests.get(
                    "https://www.googleapis.com/books/v1/volumes",
                    params={"q": f"intitle:{search_title}", "maxResults": 5},
                    timeout=8,
                )
                for item in r.json().get("items", []):
                    img = item.get("volumeInfo", {}).get("imageLinks", {})
                    for size_key in ["medium", "thumbnail", "smallThumbnail"]:
                        src = img.get(size_key)
                        if not src:
                            continue
                        src = src.replace("http://", "https://").replace("&edge=curl", "")
                        if is_real_image(src, min_bytes=500):
                            log.warning(f"  Image: THUMBNAIL from Google Books title ({size_key}) — flag for upgrade")
                            return src, 'thumbnail'
        except Exception:
            pass

    # 7c. Open Library — accept small images
    for isbn in [isbn13, isbn10]:
        if not isbn:
            continue
        for size in ["M", "S"]:
            url = f"https://covers.openlibrary.org/b/isbn/{isbn}-{size}.jpg"
            if is_real_image(url, min_bytes=500):
                log.warning(f"  Image: THUMBNAIL from Open Library ({isbn}-{size}) — flag for upgrade")
                return url, 'thumbnail'

    return None, 'none'


# ── Author extraction ──────────────────────────────────────────────────────
def _extract_author(title: str) -> str:
    """
    Extract author from 'Title by Author Name' pattern.
    Falls back to 'See Description' — satisfies eBay's required Author aspect.
    """
    m = re.search(r'\bby\s+([A-Z][^\(\n,]{2,40})(?:\s*[\(,]|$)', title, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return 'See Description'


# ── Quantity tier ──────────────────────────────────────────────────────────
def get_quantity(row: dict) -> int:
    try:
        sales = int(row.get('sales_count') or 0)
    except (ValueError, TypeError):
        sales = 0
    if sales >= 10:
        return 60
    if sales >= 3:
        return 40
    return 20


# ── eBay listing ───────────────────────────────────────────────────────────
def ensure_inventory_item(isbn: str, title: str, fmt: str,
                           description: str, cover_url, qty: int, token: str) -> bool:
    hdrs = {'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US'}

    author = _extract_author(title)

    payload = {
        'sku': isbn,
        'product': {
            'title':       title[:80],
            'description': f"{description}\n\n{CLOSING_STATEMENT}",
            'isbn':        [isbn],
            'aspects': {
                'Book Title': [title[:65]],
                'Format':     [fmt or 'Paperback'],
                'Language':   ['English'],
                'Author':     [author],
            },
        },
        'availability': {'shipToLocationAvailability': {'quantity': qty}},
    }
    if cover_url:
        payload['product']['imageUrls'] = [cover_url]

    r = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers=hdrs, json=payload, timeout=15)
    if r.status_code not in (200, 204):
        log.warning(f'  INV fail {r.status_code}: {r.text[:100]}')
    return r.status_code in (200, 204)


def delete_existing_offers(isbn: str, token: str):
    hdrs = {'Authorization': f'Bearer {token}'}
    r = requests.get('https://api.ebay.com/sell/inventory/v1/offer',
                     headers=hdrs, params={'sku': isbn}, timeout=10)
    for o in r.json().get('offers', []):
        requests.delete(
            f'https://api.ebay.com/sell/inventory/v1/offer/{o["offerId"]}',
            headers=hdrs, timeout=10)
        time.sleep(0.1)


def create_offer(isbn: str, price: float, qty: int, token: str):
    hdrs = {
        'Authorization':    f'Bearer {token}',
        'Content-Type':     'application/json',
        'Content-Language': 'en-US',
    }
    payload = {
        'sku':               isbn,
        'marketplaceId':     'EBAY_US',
        'format':            'FIXED_PRICE',
        'availableQuantity': qty,
        'categoryId':        CATEGORY_ID,
        'merchantLocationKey': 'home1',
        'listingPolicies': {
            'fulfillmentPolicyId': FULFILLMENT_POLICY,
            'paymentPolicyId':     PAYMENT_POLICY,
            'returnPolicyId':      RETURN_POLICY,
        },
        'pricingSummary': {
            'price': {'value': str(round(price, 2)), 'currency': 'USD'}
        },
        'includeCatalogProductDetails': False,
    }
    r = requests.post('https://api.ebay.com/sell/inventory/v1/offer',
                      headers=hdrs, json=payload, timeout=15)
    if r.status_code in (200, 201):
        return r.json().get('offerId')
    log.warning(f'  POST offer failed {r.status_code}: {r.text[:120]}')
    return None


def publish_offer(offer_id: str, token: str):
    r = requests.post(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type':  'application/json'},
        timeout=15)
    if r.status_code == 200:
        return r.json().get('listingId'), None
    # Safe error extraction — body may be empty on some failure codes
    err = f'HTTP {r.status_code}'
    if r.text:
        try:
            errs = r.json().get('errors', [])
            err  = errs[0].get('message', '')[:200] if errs else r.text[:200]
        except Exception:
            err = r.text[:200]
    return None, err


# ── Failure categorization ─────────────────────────────────────────────────
def categorize_failure(stage: str, error: str, had_image: bool) -> str:
    """Return a human-readable corrective action based on failure context."""
    error_lower = error.lower() if error else ''

    if 'photo' in error_lower or 'picture' in error_lower:
        if 'resolution' in error_lower:
            return (
                "Image found but too low resolution for eBay.\n"
                "    -> Upload a higher-res cover photo manually in Seller Hub.\n"
                "    -> Search Google Images for the ISBN to find a suitable cover."
            )
        else:
            return (
                "No cover image found from any of the 5 automated sources.\n"
                "    -> Upload a cover photo manually in Seller Hub.\n"
                "    -> Search Google Images for the ISBN to find a suitable cover."
            )

    if stage == 'inventory':
        if '25002' in error:
            return (
                "eBay catalog conflict on inventory item creation.\n"
                "    -> Try listing manually via Seller Hub (Sell Similar).\n"
                "    -> If persistent, add to SKIP_ISBNS and list manually."
            )
        return (
            f"Inventory item creation failed: {error[:100]}\n"
            "    -> Check eBay API status. Retry on next run."
        )

    if stage == 'offer':
        if 'already exists' in error_lower:
            return (
                "An offer already exists for this SKU.\n"
                "    -> Delete stale offer in Seller Hub, then retry."
            )
        return (
            f"Offer creation failed: {error[:100]}\n"
            "    -> Check for existing offers/listings for this ISBN."
        )

    if stage == 'publish':
        if 'description' in error_lower:
            return (
                "eBay requires a description but none was generated.\n"
                "    -> Run fix_listings.py locally to generate AI description, then retry."
            )
        return (
            f"Publish failed: {error[:120]}\n"
            "    -> Review error in Seller Hub. May need manual intervention."
        )

    return f"Unknown failure at {stage}: {error[:120]}"


# ── Failure email ──────────────────────────────────────────────────────────
def send_failure_email(failures: list):
    """Send detailed failure report email with corrective actions."""
    if not failures or not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        return

    lines = [
        f"LISTING FAILURES — {len(failures)} book(s) failed",
        f"Run: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        "=" * 60,
        "",
    ]

    for i, f in enumerate(failures, 1):
        lines.append(f"{i}. {f['title'][:60]}")
        lines.append(f"   ISBN: {f['isbn']}")
        lines.append(f"   Price: ${f['price']} | Profit: ${f['profit']}")
        lines.append(f"   Stage: {f['stage']} | Image found: {'Yes' if f['had_image'] else 'No'}")
        lines.append(f"   Error: {f['error'][:150]}")
        lines.append(f"   Action: {f['action']}")
        lines.append("")

    lines.append("-" * 60)
    lines.append("These books were found profitable by the scanner but could not")
    lines.append("be listed automatically. Manual action is needed to capture")
    lines.append("these sales opportunities.")

    msg = MIMEText('\n'.join(lines))
    msg['Subject'] = f'[Lister] {len(failures)} listing failure(s) — action needed'
    msg['From']    = EMAIL_FROM or SMTP_USER
    msg['To']      = EMAIL_TO

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
        log.info(f'Failure email sent ({len(failures)} failures)')
    except Exception as e:
        log.error(f'Failure email send error: {e}')


# ── Main ───────────────────────────────────────────────────────────────────
def list_books():
    log.info('=' * 60)
    log.info(f'LISTER STARTED — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info('=' * 60)

    opps_path = Path('scan_opportunities.json')
    if not opps_path.exists():
        log.warning('No scan_opportunities.json found')
        return
    opportunities = json.loads(opps_path.read_text())
    if not opportunities:
        log.info('No opportunities to list')
        return

    log.info(f'Processing {len(opportunities)} opportunities...')
    rows   = load_csv()
    token  = get_user_token()
    listed = []
    failed = []   # list of dicts with full failure detail

    for i, opp in enumerate(opportunities):
        isbn  = opp['isbn13']
        title = opp.get('title', isbn)
        price = opp['price']
        row   = rows.get(isbn, {})
        fmt   = row.get('format', 'Paperback')

        if isbn in BLOCKLIST:
            log.info(f'  SKIP_BLOCKLIST {isbn}')
            continue

        if (i + 1) % 25 == 0:
            token = get_user_token()
            log.info(f'  {i+1}/{len(opportunities)} — token refreshed')

        qty = get_quantity(row)

        description = row.get('description', '').strip()
        if not description:
            log.info(f'  Generating description for {isbn}...')
            description = generate_description(title, isbn)
            if isbn in rows:
                rows[isbn]['description'] = description

        # Full image lookup with ISBN-10 + title fallback + thumbnail acceptance
        isbn10 = isbn13_to_isbn10(isbn)
        cover_url, img_quality = get_book_image(isbn, isbn10, title)
        if cover_url and img_quality == 'full':
            log.info(f'  Image found (full quality)')
        elif cover_url and img_quality == 'thumbnail':
            log.warning(f'  Image found (THUMBNAIL — needs manual upgrade)')
        else:
            log.warning(f'  No image found')

        if not ensure_inventory_item(isbn, title, fmt, description, cover_url, qty, token):
            err_msg = 'Inventory item creation failed'
            action = categorize_failure('inventory', err_msg, bool(cover_url))
            log.error(f'  FAIL {isbn}: {err_msg}')
            failed.append({
                'isbn': isbn, 'title': title, 'price': price,
                'profit': opp.get('profit', '?'), 'stage': 'inventory',
                'error': err_msg, 'had_image': bool(cover_url),
                'img_quality': img_quality, 'action': action,
            })
            time.sleep(0.5)
            continue

        delete_existing_offers(isbn, token)
        time.sleep(0.2)

        offer_id = create_offer(isbn, price, qty, token)
        if not offer_id:
            err_msg = 'Offer creation failed'
            action = categorize_failure('offer', err_msg, bool(cover_url))
            log.error(f'  FAIL {isbn}: {err_msg}')
            failed.append({
                'isbn': isbn, 'title': title, 'price': price,
                'profit': opp.get('profit', '?'), 'stage': 'offer',
                'error': err_msg, 'had_image': bool(cover_url),
                'img_quality': img_quality, 'action': action,
            })
            time.sleep(0.5)
            continue

        listing_id, err = publish_offer(offer_id, token)
        if listing_id:
            thumb_tag = ' [THUMBNAIL]' if img_quality == 'thumbnail' else ''
            log.info(f'  \u2705 Listed {isbn} | ${price} | Profit: ${opp["profit"]} | qty={qty} | ListingID: {listing_id}{thumb_tag}')
            listed.append({**opp, 'img_quality': img_quality})
            if isbn in rows:
                rows[isbn]['status']     = 'active'
                rows[isbn]['offer_id']   = offer_id
                rows[isbn]['sell_price'] = str(price)
                rows[isbn]['listed_at']  = datetime.now(timezone.utc).isoformat()
                if img_quality == 'thumbnail':
                    rows[isbn]['image_flag'] = 'thumbnail'
            save_csv(rows)
        else:
            action = categorize_failure('publish', err or '', bool(cover_url))
            log.error(f'  FAIL {isbn}: {err}')
            if isbn in rows:
                rows[isbn]['offer_id'] = offer_id
            failed.append({
                'isbn': isbn, 'title': title, 'price': price,
                'profit': opp.get('profit', '?'), 'stage': 'publish',
                'error': err or 'Unknown publish error',
                'had_image': bool(cover_url), 'img_quality': img_quality,
                'action': action,
            })

        time.sleep(0.5)

    save_csv(rows)

    log.info('=' * 60)
    log.info(f'LISTER DONE: {len(listed)} listed | {len(failed)} failed')
    thumb_listed = [b for b in listed if b.get('img_quality') == 'thumbnail']
    if thumb_listed:
        log.warning(f'THUMBNAIL IMAGES ({len(thumb_listed)}) — need manual upgrade:')
        for b in thumb_listed:
            log.warning(f'  THUMBNAIL {b["isbn13"]} | {b.get("title", "")[:40]}')
    if failed:
        log.info(f'FAILED BOOKS:')
        for f in failed:
            log.info(f'  FAILED_DETAIL {f["isbn"]} | {f["title"][:40]} | stage={f["stage"]} | image={f["had_image"]} | img_quality={f.get("img_quality", "none")} | {f["error"][:80]}')
    log.info('=' * 60)

    # Success email — includes thumbnail warnings
    if listed and all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        try:
            lines = ['New listings created:\n']
            for b in listed:
                thumb_tag = ' [THUMBNAIL — needs photo upgrade]' if b.get('img_quality') == 'thumbnail' else ''
                lines.append(f"  {b['title'][:50]} | ${b['price']} | Profit: ${b['profit']}{thumb_tag}")
            if thumb_listed:
                lines.append(f'\n⚠ {len(thumb_listed)} listing(s) used thumbnail images.')
                lines.append('Upload better photos in Seller Hub for these ISBNs:')
                for b in thumb_listed:
                    lines.append(f'  - {b["isbn13"]} ({b.get("title", "")[:40]})')
            msg = MIMEText('\n'.join(lines))
            thumb_subj = f' ({len(thumb_listed)} thumbnails)' if thumb_listed else ''
            msg['Subject'] = f'[Lister] {len(listed)} new listings{thumb_subj}'
            msg['From']    = EMAIL_FROM or SMTP_USER
            msg['To']      = EMAIL_TO
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
            log.info('Success email sent')
        except Exception as e:
            log.error(f'Success email failed: {e}')

    # Failure email — separate, with detailed corrective actions
    if failed:
        send_failure_email(failed)


if __name__ == '__main__':
    list_books()
