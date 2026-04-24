#!/usr/bin/env python3
"""
scanner.py v2 — Unified discovery + scoring
Reads from booksgoat_enhanced.csv (all 408 books) + BooksGoat merchant sheet.
Merges both sources, scores all candidates, writes opportunities back to CSV.

Run: GitHub Actions scanner.yml (Monday 9AM EST)
"""

import os, csv, json, base64, time, logging, requests, re
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
BOOKSGOAT_CSV_URL  = os.getenv('BOOKSGOAT_CSV_URL')

MIN_PROFIT    = 5.00
EBAY_FEE_RATE = 0.153
UNDERCUT_PCT  = 0.12
AMAZON_CAP    = 0.95
COOLDOWN_DAYS = 14

CSV_PATH      = Path('booksgoat_enhanced.csv')
LOG_FILE      = 'scanner_log.txt'

BLOCKLIST = {
    '9781260460445',  # Lange Q&A Radiography — min qty 5
    '9780990873853',  # Overcoming Gravity — min qty 5
    '9781119826798',  # Architect's Studio Companion — PDF only
    '9781628257830',  # Process Groups: A Practice Guide — min qty 5 on BooksGoat
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── Auth ───────────────────────────────────────────────────────────────────
def get_app_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data='grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope'
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Token error: {data}')
    log.info('eBay app token acquired')
    return data['access_token']


# ── CSV ────────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    'isbn13', 'title', 'format', 'cost', 'product_url', 'category_path',
    'sell_price', 'status', 'score', 'listed_at', 'sold_at',
    'delisted_at', 'delist_reason', 'checked_at', 'offer_id', 'description',
]

def load_csv() -> dict:
    """Load CSV into {isbn13: row_dict}. Returns empty dict if not found."""
    if not CSV_PATH.exists():
        log.warning(f'CSV not found: {CSV_PATH}')
        return {}
    rows = {}
    with CSV_PATH.open(encoding='utf-8') as f:
        for row in csv.DictReader(f):
            isbn = row.get('isbn13', '').strip()
            if isbn:
                for field in CSV_FIELDS:
                    row.setdefault(field, '')
                rows[isbn] = row
    log.info(f'Loaded {len(rows)} books from CSV')
    return rows

def save_csv(rows: dict):
    all_rows = list(rows.values())
    all_fields = list(dict.fromkeys(CSV_FIELDS + [k for r in all_rows for k in r]))
    tmp = CSV_PATH.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(all_rows)
    tmp.replace(CSV_PATH)
    log.info(f'CSV saved: {len(all_rows)} rows')


# ── BooksGoat merchant sheet ───────────────────────────────────────────────
def fetch_merchant_sheet() -> dict:
    """Returns {isbn13: {cost, amazon_price, title}} from merchant sheet."""
    if not BOOKSGOAT_CSV_URL:
        log.warning('BOOKSGOAT_CSV_URL not set')
        return {}
    r = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
    r.raise_for_status()
    result = {}
    skipped = 0
    for row in csv.DictReader(StringIO(r.text)):
        try:
            isbn = row.get('ISBN-13', '').strip().replace('-', '')
            if not isbn or not re.match(r'^97[89]\d{10}$', isbn):
                skipped += 1
                continue
            cost_raw   = (row.get('10 Qty') or row.get('5 Qty', '')).replace('$', '').replace(',', '').strip()
            amazon_raw = row.get('Amazon Price', '').replace('$', '').replace(',', '').strip()
            if not cost_raw:
                skipped += 1
                continue
            result[isbn] = {
                'cost':         float(cost_raw),
                'amazon_price': float(amazon_raw) if amazon_raw and amazon_raw != 'N/A' else None,
                'title':        row.get('Title', f'Book {isbn}'),
            }
        except Exception:
            skipped += 1
    log.info(f'Merchant sheet: {len(result)} books ({skipped} skipped)')
    return result


# ── eBay comps ─────────────────────────────────────────────────────────────
def get_ebay_comps(isbn: str, app_token: str) -> tuple:
    headers = {
        'Authorization': f'Bearer {app_token}',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
    }
    prices = []
    for isbn_val in [isbn, isbn[3:] if len(isbn) == 13 else None]:
        if not isbn_val or len(isbn_val) < 10:
            continue
        try:
            r = requests.get(
                'https://api.ebay.com/buy/browse/v1/item_summary/search',
                headers=headers,
                params={'gtin': isbn_val,
                        'filter': 'conditions:{NEW},buyingOptions:{FIXED_PRICE}',
                        'limit': '50'},
                timeout=15
            )
            for item in r.json().get('itemSummaries', []):
                try:
                    prices.append(float(item['price']['value']))
                except (KeyError, ValueError):
                    pass
            if prices:
                break
        except Exception as e:
            log.warning(f'  GTIN error {isbn_val}: {e}')

    if not prices:
        try:
            r = requests.get(
                'https://api.ebay.com/buy/browse/v1/item_summary/search',
                headers=headers,
                params={'q': isbn, 'category_ids': '267',
                        'filter': 'conditions:{NEW},buyingOptions:{FIXED_PRICE}',
                        'sort': 'price', 'limit': '20'},
                timeout=15
            )
            for item in r.json().get('itemSummaries', []):
                try:
                    prices.append(float(item['price']['value']))
                except (KeyError, ValueError):
                    pass
        except Exception as e:
            log.warning(f'  Keyword error {isbn}: {e}')

    if   len(prices) >= 3: conf = 'HIGH'
    elif len(prices) >= 1: conf = 'MEDIUM'
    else:                  conf = 'NONE'
    return prices, conf


# ── Pricing ────────────────────────────────────────────────────────────────
def calc_price(isbn: str, cost: float, amazon_price, app_token: str) -> tuple:
    comps, conf = get_ebay_comps(isbn, app_token)

    if comps:
        target = round(min(comps) * (1 - UNDERCUT_PCT), 2)
        method = f'EBAY_COMP ({conf}, n={len(comps)})'
    elif amazon_price:
        target = round(amazon_price * (1 - UNDERCUT_PCT), 2)
        method = f'AMAZON_FALLBACK'
        conf   = 'FALLBACK'
    else:
        return None, None, 'NONE'

    if amazon_price and target > amazon_price * AMAZON_CAP:
        target = round(amazon_price * AMAZON_CAP, 2)
        method += ' [amazon capped]'

    profit = round(target * (1 - EBAY_FEE_RATE) - cost, 2)
    return target, profit, f'{conf} | {method}'


# ── Main ───────────────────────────────────────────────────────────────────
def scan():
    log.info('=' * 60)
    log.info(f'SCANNER STARTED — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info('=' * 60)

    rows      = load_csv()
    merchant  = fetch_merchant_sheet()
    app_token = get_app_token()
    now       = datetime.now(timezone.utc)

    # Merge merchant sheet into CSV — add new ISBNs, update cost on pending
    new_from_sheet = 0
    for isbn, data in merchant.items():
        if isbn in BLOCKLIST:
            continue
        if isbn not in rows:
            new_row = {f: '' for f in CSV_FIELDS}
            new_row.update({
                'isbn13':        isbn,
                'title':         data['title'],
                'format':        'Paperback',
                'cost':          str(round(data['cost'], 2)),
                'category_path': 'merchant_sheet',
                'status':        'pending',
            })
            rows[isbn] = new_row
            new_from_sheet += 1
        elif rows[isbn].get('status') == 'pending':
            rows[isbn]['cost'] = str(round(data['cost'], 2))

    if new_from_sheet:
        log.info(f'Added {new_from_sheet} new books from merchant sheet')

    # Score all candidates
    opportunities  = []
    already_listed = 0
    unprofitable   = 0
    no_data        = 0
    cooldown_skip  = 0

    for isbn, row in rows.items():
        if isbn in BLOCKLIST:
            continue

        status = row.get('status', '')

        # Skip active — repricer handles these
        if status == 'active':
            already_listed += 1
            continue

        # Skip cooldown delisted
        if status == 'delisted':
            delisted_at = row.get('delisted_at', '')
            if delisted_at:
                try:
                    dt = datetime.fromisoformat(delisted_at.replace('Z', '+00:00'))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    days = (now - dt).days
                    if days < COOLDOWN_DAYS:
                        cooldown_skip += 1
                        continue
                    else:
                        # Cooldown expired — reset to pending
                        row['status'] = 'pending'
                        log.info(f'  COOLDOWN_EXPIRED {isbn} — reset to pending')
                except Exception:
                    pass

        if status not in ('pending', ''):
            continue

        # Get cost and amazon price
        cost_raw = row.get('cost', '')
        if not cost_raw:
            # Try merchant sheet
            if isbn in merchant:
                cost_raw = str(merchant[isbn]['cost'])
                row['cost'] = cost_raw
            else:
                no_data += 1
                continue
        try:
            cost = float(cost_raw)
        except ValueError:
            no_data += 1
            continue

        amazon_price = merchant.get(isbn, {}).get('amazon_price')

        target, profit, method = calc_price(isbn, cost, amazon_price, app_token)
        if target is None:
            no_data += 1
            continue

        if profit < MIN_PROFIT:
            unprofitable += 1
            continue

        opportunities.append({
            'isbn13':  isbn,
            'title':   row.get('title', isbn),
            'cost':    cost,
            'price':   target,
            'profit':  profit,
            'method':  method,
        })
        log.info(f'  \u2705 {row.get("title", isbn)[:50]} | Cost: ${cost} | List: ${target} | Profit: ${profit} | {method}')
        time.sleep(0.3)

    log.info('=' * 60)
    log.info(f'SCAN COMPLETE: {len(opportunities)} opportunities found')
    log.info(f'  Already listed:   {already_listed}')
    log.info(f'  Unprofitable:     {unprofitable}')
    log.info(f'  No eBay data:     {no_data}')
    log.info(f'  Cooldown skip:    {cooldown_skip}')
    log.info('=' * 60)

    # Save opportunities for lister
    with open('scan_opportunities.json', 'w') as f:
        json.dump(opportunities, f, indent=2)

    # Save updated CSV (new books from sheet, cooldown resets)
    save_csv(rows)
    log.info(f'scan_opportunities.json written: {len(opportunities)} entries')


if __name__ == '__main__':
    scan()
