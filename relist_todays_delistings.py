#!/usr/bin/env python3
"""
relist_todays_delistings.py — Relists books incorrectly delisted today by repricer.
Only targets: status=delisted, delist_reason=unprofitable, delisted_at=2026-04-25
"""

import os, csv, base64, time, logging, requests, shutil
from pathlib import Path
from datetime import datetime, timezone

EBAY_APP_ID        = os.environ['EBAY_APP_ID']
EBAY_CERT_ID       = os.environ['EBAY_CERT_ID']
EBAY_REFRESH_TOKEN = os.environ['EBAY_REFRESH_TOKEN']

CSV_PATH    = Path(r'E:\Book\Scanner\booksgoat_enhanced.csv')
LISTER_CSV  = Path(r'E:\Book\Lister\booksgoat_enhanced.csv')
TODAY       = '2026-04-25'
LOG_FILE    = 'relist_todays.log'

FULFILLMENT_POLICY = '391308514023'
PAYMENT_POLICY     = '391308491023'
RETURN_POLICY      = '391308498023'
MERCHANT_LOCATION  = 'home1'
CATEGORY_ID        = '261186'
QUANTITY           = 20

BLOCKLIST = {
    '9781260460445','9780990873853','9781119826798','9781628257830',
    '9780415708234','9781591264507','9780091816971','9781108724265',
    '9780415898058','9781118115121','9780393979503',
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def get_user_token():
    creds = base64.b64encode(f'{EBAY_APP_ID}:{EBAY_CERT_ID}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data={'grant_type': 'refresh_token', 'refresh_token': EBAY_REFRESH_TOKEN,
              'scope': 'https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.inventory'}
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Token error: {data}')
    return data['access_token']


def put_inventory_item(token, isbn, title):
    payload = {
        'product': {'title': title, 'isbn': [isbn]},
        'condition': 'NEW',
        'availability': {'shipToLocationAvailability': {'quantity': QUANTITY}}
    }
    r = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json',
                 'Content-Language': 'en-US'},
        json=payload, timeout=15
    )
    return r.status_code in (200, 204)


def delete_existing_offers(token, isbn):
    r = requests.get(
        'https://api.ebay.com/sell/inventory/v1/offer',
        headers={'Authorization': f'Bearer {token}'},
        params={'sku': isbn}, timeout=10
    )
    if r.status_code == 200:
        for offer in r.json().get('offers', []):
            oid = offer.get('offerId')
            if oid:
                requests.delete(
                    f'https://api.ebay.com/sell/inventory/v1/offer/{oid}',
                    headers={'Authorization': f'Bearer {token}'}, timeout=10
                )


def post_offer(token, isbn, price):
    payload = {
        'sku': isbn,
        'marketplaceId': 'EBAY_US',
        'format': 'FIXED_PRICE',
        'pricingSummary': {'price': {'currency': 'USD', 'value': f'{price:.2f}'}},
        'listingPolicies': {
            'fulfillmentPolicyId': FULFILLMENT_POLICY,
            'paymentPolicyId':     PAYMENT_POLICY,
            'returnPolicyId':      RETURN_POLICY,
        },
        'merchantLocationKey': MERCHANT_LOCATION,
        'categoryId':          CATEGORY_ID,
        'includeCatalogProductDetails': False,
    }
    r = requests.post(
        'https://api.ebay.com/sell/inventory/v1/offer',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json',
                 'Content-Language': 'en-US'},
        json=payload, timeout=15
    )
    return r.json().get('offerId')


def publish_offer(token, offer_id):
    r = requests.post(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        timeout=15
    )
    return r.json().get('listingId')


def load_csv():
    rows = {}
    with CSV_PATH.open(encoding='utf-8') as f:
        for row in csv.DictReader(f):
            isbn = row.get('isbn13', '').strip()
            if isbn:
                rows[isbn] = row
    return rows


def save_csv(rows):
    all_rows   = list(rows.values())
    all_fields = list(dict.fromkeys(k for r in all_rows for k in r))
    tmp = CSV_PATH.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(all_rows)
    tmp.replace(CSV_PATH)
    if LISTER_CSV.exists():
        shutil.copy2(CSV_PATH, LISTER_CSV)


def run():
    log.info('=' * 60)
    log.info("relist_todays_delistings.py — restoring today's bad delistings")
    log.info('=' * 60)

    rows = load_csv()

    candidates = []
    for isbn, row in rows.items():
        if isbn in BLOCKLIST:
            continue
        if row.get('status') != 'delisted':
            continue
        if row.get('delist_reason') != 'unprofitable':
            continue
        if not row.get('delisted_at', '').startswith(TODAY):
            continue
        if not float(row.get('sell_price') or 0):
            continue
        candidates.append((isbn, row))

    log.info(f'Found {len(candidates)} books to relist')
    if not candidates:
        log.info('Nothing to relist — check CSV path and delisted_at dates')
        return

    token = get_user_token()
    log.info('Token acquired')

    listed = failed = token_counter = 0

    for i, (isbn, row) in enumerate(candidates):
        title      = row.get('title', isbn)
        sell_price = float(row.get('sell_price') or 0)

        token_counter += 1
        if token_counter % 50 == 0:
            token = get_user_token()

        log.info(f'[{i+1}/{len(candidates)}] {isbn} | ${sell_price:.2f} | {title[:50]}')

        try:
            put_inventory_item(token, isbn, title)
            delete_existing_offers(token, isbn)
            offer_id = post_offer(token, isbn, sell_price)
            if not offer_id:
                log.error(f'  No offer ID'); failed += 1; time.sleep(0.5); continue
            listing_id = publish_offer(token, offer_id)
            if listing_id:
                rows[isbn]['status']       = 'active'
                rows[isbn]['offer_id']     = offer_id
                rows[isbn]['listed_at']    = datetime.now(timezone.utc).isoformat()
                rows[isbn]['delisted_at']  = ''
                rows[isbn]['delist_reason']= ''
                save_csv(rows)
                log.info(f'  ✅ offer={offer_id} listing={listing_id}')
                listed += 1
            else:
                log.error(f'  Publish failed'); failed += 1
        except Exception as e:
            log.error(f'  Exception: {e}'); failed += 1

        time.sleep(0.5)

    log.info('=' * 60)
    log.info(f'DONE: {listed} relisted | {failed} failed')
    log.info('=' * 60)


if __name__ == '__main__':
    run()
