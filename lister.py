#!/usr/bin/env python3
"""
lister.py v2 — Lists opportunities from scan_opportunities.json
Uses full_publish logic: PUT inventory item → DELETE stale offer →
POST fresh offer WITH merchantLocationKey → publish.
Updates booksgoat_enhanced.csv with status=active, offer_id, sell_price.

Run: GitHub Actions scanner.yml after scanner.py
"""

import os, csv, json, base64, time, logging, requests, smtplib
from datetime import datetime, timezone
from pathlib import Path
from email.mime.text import MIMEText

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
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


# ── eBay listing ───────────────────────────────────────────────────────────
def ensure_inventory_item(isbn: str, title: str, fmt: str, token: str) -> bool:
    hdrs = {'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US'}
    payload = {
        'sku':       isbn,
        'product':   {'title': title[:80], 'isbn': [isbn],
                      'aspects': {'Format': [fmt or 'Paperback']}},
        'condition': 'NEW',
        'availability': {'shipToLocationAvailability': {'quantity': QUANTITY}},
    }
    r = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers=hdrs, json=payload, timeout=15)
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

def create_offer(isbn: str, price: float, token: str):
    hdrs = {'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US'}
    payload = {
        'sku':               isbn,
        'marketplaceId':     'EBAY_US',
        'format':            'FIXED_PRICE',
        'availableQuantity': QUANTITY,
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
        fmt   = rows.get(isbn, {}).get('format', 'Paperback')

        if isbn in BLOCKLIST:
            log.info(f'  SKIP_BLOCKLIST {isbn}')
            continue

        if (i + 1) % 25 == 0:
            token = get_user_token()
            log.info(f'  {i+1}/{len(opportunities)} — token refreshed')

        # Step 1: ensure inventory item
        if not ensure_inventory_item(isbn, title, fmt, token):
            log.error(f'  INV FAIL {isbn}')
            failed.append(isbn)
            time.sleep(0.5)
            continue

        # Step 2: delete stale offers
        delete_existing_offers(isbn, token)
        time.sleep(0.2)

        # Step 3: create fresh offer with merchantLocationKey
        offer_id = create_offer(isbn, price, token)
        if not offer_id:
            log.error(f'  OFFER FAIL {isbn}')
            failed.append(isbn)
            time.sleep(0.5)
            continue

        # Step 4: publish
        listing_id, err = publish_offer(offer_id, token)
        if listing_id:
            log.info(f'  \u2705 Listed {isbn} | ${price} | Profit: ${opp["profit"]} | ListingID: {listing_id}')
            listed.append(opp)
            if isbn in rows:
                rows[isbn]['status']     = 'active'
                rows[isbn]['offer_id']   = offer_id
                rows[isbn]['sell_price'] = str(price)
                rows[isbn]['listed_at']  = datetime.now(timezone.utc).isoformat()
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
