#!/usr/bin/env python3
"""
lister.py v2.1 — Lists opportunities from scan_opportunities.json
Uses full_publish logic: PUT inventory item → DELETE stale offer →
POST fresh offer WITH merchantLocationKey → publish.
Updates booksgoat_enhanced.csv with status=active, offer_id, sell_price.
Includes AI description generation and protection_patch support.

Run: GitHub Actions scanner.yml after scanner.py
"""

import os, csv, json, base64, time, logging, requests, smtplib, sys
from datetime import datetime, timezone
from pathlib import Path
from email.mime.text import MIMEText

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Import protection support if available
try:
    from protection_patch import is_protected
except ImportError:
    def is_protected(row): return str(row.get('protected','')).lower() == 'true'

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

BLOCKLIST = {
    '9781260460445',
    '9780990873853',
    '9781119826798',
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


# ── Cover image ────────────────────────────────────────────────────────────
def get_cover_image(isbn: str):
    sources = [
        f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn}.01.LZZZZZZZ.jpg",
    ]
    for url in sources:
        try:
            r = requests.head(url, timeout=5, allow_redirects=True)
            if r.status_code == 200 and int(r.headers.get('Content-Length', 0)) > 1000:
                return url
        except Exception:
            pass
    try:
        r = requests.get(
            f'https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}&maxResults=1',
            timeout=5)
        if r.status_code == 200:
            items = r.json().get('items', [])
            if items:
                links = items[0].get('volumeInfo', {}).get('imageLinks', {})
                img = links.get('extraLarge') or links.get('large') or links.get('thumbnail')
                if img:
                    return img.replace('http://', 'https://').replace('&edge=curl', '')
    except Exception:
        pass
    return None


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
    payload = {
        'sku':     isbn,
        'product': {
            'title':       title[:80],
            'description': f"{description}\n\n{CLOSING_STATEMENT}",
            'isbn':        [isbn],
            'aspects':     {'Format': [fmt or 'Paperback'], 'Language': ['English']},
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
    hdrs = {'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US'}
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
        'pricingSummary': {'price': {'value': str(round(price, 2)), 'currency': 'USD'}},
        'includeCatalogProductDetails': True,
    }
    r = requests.post('https://api.ebay.com/sell/inventory/v1/offer',
                      headers=hdrs, json=payload, timeout=15)
    if r.status_code in (200, 201):
        return r.json().get('offerId')
    log.warning(f'  POST offer failed {r.status_code}: {r.text[:100]}')
    return None

def publish_offer(offer_id: str, token: str):
    r = requests.post(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json'},
        timeout=15)
    if r.status_code == 200:
        return r.json().get('listingId'), None
    err = r.json().get('errors', [{}])[0].get('message', '')[:100]
    return None, err


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
    rows  = load_csv()
    token = get_user_token()
    listed = []
    failed = []

    for i, opp in enumerate(opportunities):
        isbn  = opp['isbn13']
        title = opp.get('title', isbn)
        price = opp['price']
        row   = rows.get(isbn, {})
        fmt   = row.get('format', 'Paperback')

        if isbn in BLOCKLIST:
            log.info(f'  SKIP_BLOCKLIST {isbn}')
            continue

        # Refresh token every 25 books
        if (i + 1) % 25 == 0:
            token = get_user_token()
            log.info(f'  {i+1}/{len(opportunities)} — token refreshed')

        # Get quantity tier
        qty = get_quantity(row)

        # Get or generate description
        description = row.get('description', '').strip()
        if not description:
            log.info(f'  Generating description for {isbn}...')
            description = generate_description(title, isbn)
            if isbn in rows:
                rows[isbn]['description'] = description

        # Get cover image
        cover_url = get_cover_image(isbn)
        if cover_url:
            log.info(f'  Image found')
        else:
            log.warning(f'  No image found')

        # Step 1: ensure inventory item
        if not ensure_inventory_item(isbn, title, fmt, description, cover_url, qty, token):
            log.error(f'  INV FAIL {isbn}')
            failed.append(isbn)
            time.sleep(0.5)
            continue

        # Step 2: delete stale offers
        delete_existing_offers(isbn, token)
        time.sleep(0.2)

        # Step 3: create fresh offer with merchantLocationKey
        offer_id = create_offer(isbn, price, qty, token)
        if not offer_id:
            log.error(f'  OFFER FAIL {isbn}')
            failed.append(isbn)
            time.sleep(0.5)
            continue

        # Step 4: publish
        listing_id, err = publish_offer(offer_id, token)
        if listing_id:
            log.info(f'  \u2705 Listed {isbn} | ${price} | Profit: ${opp["profit"]} | qty={qty} | ListingID: {listing_id}')
            listed.append(opp)
            if isbn in rows:
                rows[isbn]['status']     = 'active'
                rows[isbn]['offer_id']   = offer_id
                rows[isbn]['sell_price'] = str(price)
                rows[isbn]['listed_at']  = datetime.now(timezone.utc).isoformat()
            # Atomic save after each successful listing
            save_csv(rows)
        else:
            log.error(f'  FAIL {isbn}: {err}')
            if isbn in rows:
                rows[isbn]['offer_id'] = offer_id
            failed.append(isbn)

        time.sleep(0.5)

    save_csv(rows)

    log.info('=' * 60)
    log.info(f'LISTER DONE: {len(listed)} listed | {len(failed)} failed')
    log.info('=' * 60)

    if listed and all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        try:
            lines = ['New listings created:\n']
            for b in listed:
                lines.append(f"  {b['title'][:50]} | ${b['price']} | Profit: ${b['profit']}")
            msg = MIMEText('\n'.join(lines))
            msg['Subject'] = f'[Lister] {len(listed)} new listings'
            msg['From'] = EMAIL_FROM or SMTP_USER
            msg['To']   = EMAIL_TO
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
            log.info('Email alert sent')
        except Exception as e:
            log.error(f'Email failed: {e}')


if __name__ == '__main__':
    list_books()
