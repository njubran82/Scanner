#!/usr/bin/env python3
"""
scanner.py v3 — Unified discovery + scoring
Reads from booksgoat_enhanced.csv (committed to repo by local scraper).
Also fetches merchant sheet for amazon_price data (used for price cap).
Scores all pending candidates, writes opportunities to scan_opportunities.json.

Run: GitHub Actions scanner.yml (Monday 9AM EST — after local scraper pushes CSV at 6AM)
"""

import os, csv, json, base64, time, logging, requests, re
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
BOOKSGOAT_CSV_URL  = os.getenv('BOOKSGOAT_CSV_URL')

MIN_PROFIT    = 12.00
EBAY_FEE_RATE = 0.153
UNDERCUT_PCT  = 0.12
AMAZON_CAP    = 0.95
COOLDOWN_DAYS = 14

CSV_PATH  = Path('booksgoat_enhanced.csv')
LOG_FILE  = 'scanner_log.txt'

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
    "9780357622957",  # Theory and Practice of Group Counseling — 5-qty mi
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
    all_rows   = list(rows.values())
    all_fields = list(dict.fromkeys(CSV_FIELDS + [k for r in all_rows for k in r]))
    tmp = CSV_PATH.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(all_rows)
    tmp.replace(CSV_PATH)
    log.info(f'CSV saved: {len(all_rows)} rows')


# ── BooksGoat merchant sheet (amazon_price only) ───────────────────────────
def fetch_merchant_sheet() -> dict:
    """
    Fetches merchant sheet for amazon_price data only — used for price cap.
    Cost basis comes from booksgoat_enhanced.csv (set by local scraper at 5-qty).
    """
    if not BOOKSGOAT_CSV_URL:
        log.warning('BOOKSGOAT_CSV_URL not set')
        return {}
    r = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
    r.raise_for_status()
    result  = {}
    skipped = 0
    for row in csv.DictReader(StringIO(r.text)):
        try:
            isbn = row.get('ISBN-13', '').strip().replace('-', '')
            if not isbn or not re.match(r'^97[89]\d{10}$', isbn):
                skipped += 1
                continue
            # 5-qty price as cost basis
            cost_raw   = (row.get('5 Qty') or row.get('10 Qty', '')).replace('$', '').replace(',', '').strip()
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
        method = 'AMAZON_FALLBACK'
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
    log.info(f'SCANNER v3 STARTED — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info('=' * 60)

    # Load CSV (committed by local scraper at 6AM — contains all sources)
    rows = load_csv()
    if not rows:
        log.error('CSV is empty or missing — scraper may not have pushed yet')
        return

    # Fetch merchant sheet for amazon_price data only
    merchant  = fetch_merchant_sheet()
    app_token = get_app_token()
    now       = datetime.now(timezone.utc)

    log.info(f'CSV: {len(rows)} total books')
    log.info(f'Merchant sheet: {len(merchant)} books (for amazon_price cap only)')

    # Score all pending candidates
    opportunities  = []
    already_listed = 0
    unprofitable   = 0
    no_data        = 0
    cooldown_skip  = 0
    blocklist_skip = 0

    for isbn, row in rows.items():
        if isbn in BLOCKLIST:
            blocklist_skip += 1
            continue

        status = row.get('status', '')

        # Active books are handled by repricer — skip here
        if status == 'active':
            already_listed += 1
            continue

        # Cooldown check
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
                        row['status'] = 'pending'
                        log.info(f'  COOLDOWN_EXPIRED {isbn} — reset to pending')
                except Exception:
                    pass

        if status not in ('pending', ''):
            continue

        # Cost from CSV (set by scraper at 5-qty)
        cost_raw = row.get('cost', '')
        if not cost_raw:
            no_data += 1
            continue
        try:
            cost = float(cost_raw)
        except ValueError:
            no_data += 1
            continue

        # Amazon price from merchant sheet (for cap only)
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
        log.info(
            f'  ✅ {row.get("title", isbn)[:50]} | '
            f'Cost: ${cost} | List: ${target} | Profit: ${profit} | {method}'
        )
        time.sleep(0.3)

    log.info('=' * 60)
    log.info(f'SCAN COMPLETE: {len(opportunities)} opportunities found')
    log.info(f'  Already listed: {already_listed}')
    log.info(f'  Unprofitable:   {unprofitable}')
    log.info(f'  No eBay data:   {no_data}')
    log.info(f'  Cooldown skip:  {cooldown_skip}')
    log.info(f'  Blocklist skip: {blocklist_skip}')
    log.info('=' * 60)

    # Write opportunities for lister
    with open('scan_opportunities.json', 'w') as f:
        json.dump(opportunities, f, indent=2)

    # Save CSV (cooldown resets)
    save_csv(rows)
    log.info(f'scan_opportunities.json written: {len(opportunities)} entries')


if __name__ == '__main__':
    scan()
