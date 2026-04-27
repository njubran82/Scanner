#!/usr/bin/env python3
"""
update_quantities.py — Bulk update listing quantities based on sales_count.

Tiers:
  sales_count >= 30 → qty 100
  sales_count >= 10 → qty 60
  sales_count >= 5  → qty 40
  sales_count >= 1  → qty 30
  default (0)       → qty 20
"""

import os, csv, sqlite3, base64, time, logging, requests
from pathlib import Path

EBAY_APP_ID        = os.environ['EBAY_APP_ID']
EBAY_CERT_ID       = os.environ['EBAY_CERT_ID']
EBAY_REFRESH_TOKEN = os.environ['EBAY_REFRESH_TOKEN']

CSV_PATH = Path(r'E:\Book\Lister\booksgoat_enhanced.csv')
DB_PATH  = Path(r'E:\Book\Scanner\protection.db')
LOG_FILE = 'update_quantities.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def qty_for_sales(sales_count: int) -> int:
    if sales_count >= 30:
        return 100
    if sales_count >= 10:
        return 60
    if sales_count >= 5:
        return 40
    if sales_count >= 1:
        return 30
    return 20


def get_user_token() -> str:
    creds = base64.b64encode(f'{EBAY_APP_ID}:{EBAY_CERT_ID}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data={'grant_type': 'refresh_token', 'refresh_token': EBAY_REFRESH_TOKEN,
              'scope': 'https://api.ebay.com/oauth/api_scope/sell.inventory'}
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Token error: {data}')
    return data['access_token']


def get_inventory_item(isbn: str, token: str) -> dict | None:
    r = requests.get(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers={'Authorization': f'Bearer {token}'},
        timeout=10
    )
    if r.status_code == 200:
        return r.json()
    return None


def update_quantity(isbn: str, qty: int, token: str, item: dict) -> bool:
    payload = {
        'product': item.get('product', {}),
        'availability': {
            'shipToLocationAvailability': {'quantity': qty}
        }
    }
    r = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json',
                 'Content-Language': 'en-US'},
        json=payload,
        timeout=15
    )
    return r.status_code in (200, 204)


def load_sales_counts() -> dict:
    counts = {}
    if not DB_PATH.exists():
        log.warning(f'Protection DB not found at {DB_PATH} — all counts = 0')
        return counts
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute('SELECT isbn, sales_count FROM book_protection').fetchall()
        conn.close()
        for isbn, count in rows:
            counts[isbn] = count
        log.info(f'Loaded {len(counts)} sales_count records from DB')
    except Exception as e:
        log.warning(f'DB error: {e}')
    return counts


def run():
    log.info('=' * 60)
    log.info('update_quantities.py started')

    token = get_user_token()
    log.info('Token acquired')

    rows = list(csv.DictReader(CSV_PATH.open(encoding='utf-8')))
    active = [r for r in rows if r.get('status') == 'active']
    log.info(f'{len(active)} active listings')

    sales_counts = load_sales_counts()

    updated = skipped = errors = token_counter = 0

    for i, row in enumerate(active):
        isbn  = row.get('isbn13', '')
        title = row.get('title', '')[:50]

        token_counter += 1
        if token_counter % 50 == 0:
            token = get_user_token()

        sales = sales_counts.get(isbn, 0)
        target_qty = qty_for_sales(sales)

        log.info(f'  [{i+1}/{len(active)}] {isbn} | sales={sales} → qty={target_qty} | {title}')

        item = get_inventory_item(isbn, token)
        if not item:
            log.warning(f'    No inventory item — skipping')
            errors += 1
            time.sleep(0.3)
            continue

        current_qty = item.get('availability', {}).get(
            'shipToLocationAvailability', {}).get('quantity', 0)

        if current_qty == target_qty:
            log.info(f'    Already at qty={target_qty} — skipping')
            skipped += 1
            time.sleep(0.2)
            continue

        ok = update_quantity(isbn, target_qty, token, item)
        if ok:
            log.info(f'    ✅ Updated {current_qty} → {target_qty}')
            updated += 1
        else:
            log.error(f'    ❌ Update failed')
            errors += 1

        time.sleep(0.4)

    log.info('=' * 60)
    log.info(f'DONE: {updated} updated | {skipped} already correct | {errors} errors')
    log.info('=' * 60)


if __name__ == '__main__':
    run()
