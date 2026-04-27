#!/usr/bin/env python3
"""
repricer.py v2 — reads from booksgoat_enhanced.csv (not lister_state.json)
Reprices all status=active rows. Updates CSV with new sell_price.
Delists if profit < MIN_PROFIT.
"""

import os, csv, json, base64, time, logging, requests, statistics
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from protection_patch import should_delist

load_dotenv()

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
BOOKSGOAT_CSV_URL  = os.getenv('BOOKSGOAT_CSV_URL')
SMTP_HOST          = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT          = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER          = os.getenv('SMTP_USER')
SMTP_PASSWORD      = os.getenv('SMTP_PASSWORD')
EMAIL_FROM         = os.getenv('EMAIL_FROM')
EMAIL_TO           = os.getenv('EMAIL_TO')

MIN_PROFIT    = 5.00
EBAY_FEE_RATE = 0.153
UNDERCUT_PCT  = 0.12
AMAZON_CAP    = 0.95
MIN_PRICE_MULT = 1.20   # never list below cost × 1.2 regardless of comps
CSV_PATH      = Path('booksgoat_enhanced.csv')
LOG_FILE      = 'repricer_log.txt'

BLOCKLIST = {
    '9781260460445',
    '9780990873853',
    '9781119826798',
    '9781628257830',  # Process Groups: A Practice Guide — min qty 5 on BooksGoat
    '9780415708234',  # Healing the Fragmented Selves of Trauma Survivors — min qty 6 on BooksGoat
    '9781591264507',  # PPI FE Electrical and Computer Practice Problems — min qty 5 on BooksGoat
    '9780091816971',  # Who Moved My Cheese — min qty 50 on BooksGoat
    '9781108724265',  # Trustworthy Online Controlled Experiments — min qty 5 on BooksGoat
    '9780415898058',  # Even if it Costs Me My Life — min qty 5 on BooksGoat
    '9781118115121',  # Art and Science of Technical Analysis — min qty 5 on BooksGoat
    '9780393979503',  # C Programming: A Modern Approach — download only on BooksGoat
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def get_user_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
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
        raise RuntimeError(f'App token error: {data}')
    return data['access_token']

def fetch_booksgoat_prices():
    if not BOOKSGOAT_CSV_URL:
        return {}
    r = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
    r.raise_for_status()
    prices = {}
    for row in csv.DictReader(StringIO(r.text)):
        try:
            isbn       = row.get('ISBN-13', '').strip().replace('-', '')
            cost_raw   = (row.get('10 Qty') or row.get('5 Qty', '')).replace('$','').replace(',','').strip()
            amazon_raw = row.get('Amazon Price', '').replace('$','').replace(',','').strip()
            if isbn and cost_raw:
                prices[isbn] = {
                    'cost':         float(cost_raw),
                    'amazon_price': float(amazon_raw) if amazon_raw and amazon_raw != 'N/A' else None
                }
        except Exception:
            pass
    log.info(f'Fetched {len(prices)} BooksGoat prices')
    return prices

def get_ebay_comps(isbn, app_token):
    headers = {'Authorization': f'Bearer {app_token}', 'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'}
    prices = []
    for isbn_val in [isbn, isbn[3:] if len(isbn) == 13 else None]:
        if not isbn_val or len(isbn_val) < 10:
            continue
        try:
            r = requests.get(
                'https://api.ebay.com/buy/browse/v1/item_summary/search',
                headers=headers,
                params={'gtin': isbn_val, 'filter': 'conditions:{NEW},buyingOptions:{FIXED_PRICE}', 'limit': '50'},
                timeout=15)
            for item in r.json().get('itemSummaries', []):
                try: prices.append(float(item['price']['value']))
                except: pass
            if prices: break
        except Exception as e:
            log.warning(f'  GTIN error {isbn_val}: {e}')
    # No keyword fallback — broad title searches match wrong editions and corrupt pricing
    conf = 'HIGH' if len(prices) >= 3 else 'MEDIUM' if prices else 'NONE'
    return prices, conf

def filter_comps(prices: list, cost: float, multiplier: float = 1.1) -> list:
    """Discard comps below cost * multiplier — eliminates used/wrong edition outliers."""
    if not prices or not cost:
        return prices
    floor = cost * multiplier
    filtered = [p for p in prices if p >= floor]
    return filtered if filtered else prices  # fall back to unfiltered if all removed

def calc_target(isbn, cost, amazon_price, app_token):
    comps, conf = get_ebay_comps(isbn, app_token)
    if comps:
        comps = filter_comps(comps, cost)  # remove cheap outliers before taking min
        target = round(min(comps) * (1 - UNDERCUT_PCT), 2)
        method = f'EBAY_COMP ({conf}, n={len(comps)})'
    elif amazon_price:
        target = round(amazon_price * (1 - UNDERCUT_PCT), 2)
        method = 'AMAZON_FALLBACK'
        conf   = 'FALLBACK'
    else:
        return None, None, 'NO_ANCHOR'
    if amazon_price and target > amazon_price * AMAZON_CAP:
        target = round(amazon_price * AMAZON_CAP, 2)
        method += ' [amazon capped]'
    # Never list below cost × MIN_PRICE_MULT — prevents bad comps from killing margin
    min_price = round(cost * MIN_PRICE_MULT, 2)
    if target < min_price:
        target = min_price
        method += ' [min floor]'
    profit = round(target * (1 - EBAY_FEE_RATE) - cost, 2)
    return target, profit, method

def update_offer_price(token, offer_id, new_price):
    r = requests.get(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
                     headers={'Authorization': f'Bearer {token}'}, timeout=10)
    if r.status_code != 200:
        log.error(f'  Could not fetch offer {offer_id}: {r.text[:200]}')
        return False
    current  = r.json()
    policies = current.get('listingPolicies', {})
    location = current.get('merchantLocationKey', '')
    category = current.get('categoryId', '267')
    r2 = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Content-Language': 'en-US'},
        json={'pricingSummary': {'price': {'currency': 'USD', 'value': f'{new_price:.2f}'}},
              'listingPolicies': policies, 'merchantLocationKey': location, 'categoryId': category},
        timeout=15)
    if r2.status_code not in [200, 204]:
        log.error(f'  PUT failed ({r2.status_code}): {r2.text[:300]}')
        return False
    return True

def delist_offer(token, isbn, offer_id):
    if not offer_id:
        return False
    r = requests.delete(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
                        headers={'Authorization': f'Bearer {token}'}, timeout=10)
    return r.status_code in [200, 204]

def load_csv():
    if not CSV_PATH.exists():
        return {}
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

def reprice():
    log.info('=' * 60)
    log.info(f'REPRICER STARTED — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info(f'fee={EBAY_FEE_RATE*100:.1f}%  min_profit=${MIN_PROFIT:.2f}  undercut={UNDERCUT_PCT*100:.0f}%')
    log.info('=' * 60)

    rows = load_csv()
    active = {isbn: row for isbn, row in rows.items()
              if row.get('status') == 'active' and row.get('offer_id') and isbn not in BLOCKLIST}
    log.info(f'Checking {len(active)} active listings...')

    bg_prices  = fetch_booksgoat_prices()
    user_token = get_user_token()
    app_token  = get_app_token()

    repriced = []
    delisted = []
    unchanged = 0
    errors = 0

    for isbn, row in list(active.items()):
        title         = row.get('title', isbn)
        offer_id      = row.get('offer_id')
        listing_price = float(row.get('sell_price') or 0)
        current_cost  = float(bg_prices.get(isbn, {}).get('cost') or row.get('cost') or 0)
        amazon_price  = bg_prices.get(isbn, {}).get('amazon_price')

        if not listing_price or not current_cost:
            unchanged += 1
            continue

        target, profit, method = calc_target(isbn, current_cost, amazon_price, app_token)
        if target is None:
            unchanged += 1
            continue

        if should_delist(row, profit):
            protected_flag = row.get('protected', 'false') == 'true'
            floor = 0.00 if protected_flag else MIN_PROFIT
            log.info(f'  {title[:50]}: AUTO-DELIST — profit ${profit:.2f} at ${target:.2f} (cost ${current_cost:.2f}) [floor=${floor:.2f}{" PROTECTED" if protected_flag else ""}]')
            if delist_offer(user_token, isbn, offer_id):
                rows[isbn]['status']      = 'delisted'
                rows[isbn]['delisted_at'] = datetime.now(timezone.utc).isoformat()
                rows[isbn]['delist_reason'] = 'unprofitable'
                delisted.append({'title': title, 'profit': profit})
                save_csv(rows)  # write immediately — prevents orphan on canceled run
                log.info(f'  \u2705 Delisted')
            else:
                log.error(f'  \u274c Delist failed')
                errors += 1

        elif abs(target - listing_price) > 0.01:
            log.info(f'  {title[:50]}: ${listing_price:.2f} \u2192 ${target:.2f} profit=${profit:.2f}  method={method}')
            if update_offer_price(user_token, offer_id, target):
                rows[isbn]['sell_price'] = str(target)
                repriced.append({'title': title, 'old': listing_price, 'new': target, 'profit': profit})
                save_csv(rows)  # write immediately — keeps CSV in sync with eBay state
                log.info(f'  \u2705 Repriced')
            else:
                log.error(f'  \u274c Reprice failed ({isbn})')
                errors += 1
        else:
            log.info(f'  {title[:50]}: price OK (${listing_price:.2f}, profit=${profit:.2f})')
            unchanged += 1

        time.sleep(0.5)

    save_csv(rows)

    log.info('=' * 60)
    log.info(f'DONE: {len(repriced)} repriced | {len(delisted)} delisted | {unchanged} unchanged')
    log.info('=' * 60)

if __name__ == '__main__':
    reprice()
