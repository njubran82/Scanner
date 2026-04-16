"""
repricer.py — Weekly price updater + delist engine
Reads all active listings from lister_state.json, checks live eBay comps,
and either:
  - Reprices to 12% below cheapest eBay competitor
  - Falls back to 12% below Amazon list price if no eBay comps
  - Delists if profit < $1.00 at target price
  - Leaves alone if already correctly priced

Run: automatically via scanner.yml after lister.py
     or manually: python repricer.py

Updated constants vs original:
  EBAY_FEE_RATE   0.1325  → 0.153   (15.3%)
  MIN_PROFIT      5.00    → 1.00
  Undercut        5% off Amazon → 12% off eBay comps (Amazon fallback)
"""

import os, csv, json, base64, time, logging, requests
from io import StringIO
from datetime import datetime
from dotenv import load_dotenv

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

MIN_PROFIT     = 1.00        # was 5.00
EBAY_FEE_RATE  = 0.153       # was 0.1325
UNDERCUT_PCT   = 0.12        # 12% — was 0.95 (5% off Amazon only)
STATE_FILE     = 'lister_state.json'
LOG_FILE       = 'repricer_log.txt'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── eBay Auth ─────────────────────────────────────────────────────────────────
def get_user_token():
    """Refresh token → short-lived user access token (sell.inventory scope)."""
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type':    'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': (
                'https://api.ebay.com/oauth/api_scope '
                'https://api.ebay.com/oauth/api_scope/sell.inventory'
            )
        }
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Token error: {data}')
    return data['access_token']

def get_app_token():
    """Client credentials token for Browse API (comp lookup, no user auth needed)."""
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


# ── BooksGoat CSV (Amazon price fallback) ─────────────────────────────────────
def fetch_booksgoat_prices():
    """Returns dict isbn → {'cost': float, 'amazon_price': float|None}"""
    if not BOOKSGOAT_CSV_URL:
        log.warning('BOOKSGOAT_CSV_URL not set — Amazon fallback unavailable')
        return {}
    r = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
    r.raise_for_status()
    prices = {}
    for row in csv.DictReader(StringIO(r.text)):
        try:
            isbn       = row.get('ISBN-13', '').strip().replace('-', '')
            cost_raw   = row.get('5 Qty', '').replace('$', '').replace(',', '').strip()
            amazon_raw = row.get('Amazon Price', '').replace('$', '').replace(',', '').strip()
            if isbn and cost_raw:
                prices[isbn] = {
                    'cost':         float(cost_raw),
                    'amazon_price': float(amazon_raw) if amazon_raw and amazon_raw != 'N/A' else None
                }
        except Exception:
            pass
    log.info(f'Fetched {len(prices)} BooksGoat prices')
    return prices


# ── eBay Comps (primary pricing source) ──────────────────────────────────────
def get_ebay_comps(isbn, app_token):
    """
    Returns (prices: list[float], confidence: str).
    Uses Browse API — Books category, fixed price only.
    """
    headers = {
        'Authorization':          f'Bearer {app_token}',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
    }
    params = {
        'q':            isbn,
        'category_ids': '267',
        'filter':       'buyingOptions:{FIXED_PRICE}',
        'sort':         'price',
        'limit':        '20',
    }
    try:
        r = requests.get(
            'https://api.ebay.com/buy/browse/v1/item_summary/search',
            headers=headers, params=params, timeout=15
        )
        r.raise_for_status()
    except Exception as e:
        log.warning(f'  Browse API error for {isbn}: {e}')
        return [], 'ERROR'

    prices = []
    for item in r.json().get('itemSummaries', []):
        try:
            prices.append(float(item['price']['value']))
        except (KeyError, ValueError):
            pass

    if   len(prices) >= 3: conf = 'HIGH'
    elif len(prices) >= 1: conf = 'MEDIUM'
    else:                  conf = 'NONE'
    return prices, conf


# ── Target Price ──────────────────────────────────────────────────────────────
def calc_target_price(isbn, current_cost, current_listing_price,
                      bg_prices, app_token):
    """
    Returns (target_price, method, confidence).
    Priority: eBay comps → Amazon fallback → keep current.
    """
    comps, conf = get_ebay_comps(isbn, app_token)

    if comps:
        floor        = min(comps)
        target_price = round(floor * (1 - UNDERCUT_PCT), 2)
        method       = f'EBAY_COMP ({conf}, floor=${floor:.2f})'
        return target_price, method, conf

    # No eBay comps — try Amazon fallback
    bg = bg_prices.get(isbn, {})
    amazon_price = bg.get('amazon_price')
    if amazon_price:
        target_price = round(amazon_price * (1 - UNDERCUT_PCT), 2)
        method       = f'AMAZON_FALLBACK (${amazon_price:.2f})'
        return target_price, method, 'FALLBACK'

    # No anchor price at all — keep current price, just check profitability
    return current_listing_price, 'NO_ANCHOR_KEEP_CURRENT', 'NONE'


# ── eBay Offer Update ─────────────────────────────────────────────────────────
def update_offer_price(token, offer_id, new_price):
    r = requests.get(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={'Authorization': f'Bearer {token}'}, timeout=10
    )
    if r.status_code != 200:
        log.error(f'  Could not fetch offer {offer_id}: {r.text[:200]}')
        return False

    offer_data = r.json()
    for field in ['offerId', 'status', 'listing', 'marketplaceId',
                  'auditInfo', 'availableQuantity', 'soldQuantity',
                  'warnings', 'errors']:
        offer_data.pop(field, None)

    offer_data['pricingSummary'] = {
        'price': {'currency': 'USD', 'value': f'{new_price:.2f}'}
    }

    r2 = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={
            'Authorization':    f'Bearer {token}',
            'Content-Type':     'application/json',
            'Content-Language': 'en-US'
        },
        json=offer_data, timeout=15
    )
    if r2.status_code not in [200, 204]:
        log.error(f'  PUT failed ({r2.status_code}): {r2.text[:300]}')
        return False
    return True


def delist_offer(token, isbn13, offer_id=None):
    if not offer_id:
        r = requests.get(
            'https://api.ebay.com/sell/inventory/v1/offer',
            headers={'Authorization': f'Bearer {token}',
                     'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'},
            params={'sku': isbn13}, timeout=10
        )
        offers = r.json().get('offers', [])
        if offers:
            offer_id = offers[0].get('offerId')
    if not offer_id:
        log.error(f'  No offer_id found for {isbn13}')
        return False
    r = requests.delete(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={'Authorization': f'Bearer {token}'}, timeout=10
    )
    return r.status_code in [200, 204]


# ── Alert ─────────────────────────────────────────────────────────────────────
def send_alert(repriced, delisted):
    import smtplib
    from email.mime.text import MIMEText

    lines = []
    if repriced:
        lines.append('=== REPRICED ===')
        for r in repriced:
            lines.append(
                f"  {r['title'][:50]} | ${r['old_price']:.2f} → ${r['new_price']:.2f} "
                f"| profit: ${r['profit']:.2f} | {r['method']}"
            )
    if delisted:
        lines.append('\n=== DELISTED (unprofitable) ===')
        for d in delisted:
            lines.append(f"  {d['title'][:50]} | {d['reason']}")

    if not lines:
        return

    body    = '\n'.join(lines)
    subject = f'[Repricer] {len(repriced)} repriced, {len(delisted)} delisted'

    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.warning('Email not configured — skipping alert')
        return
    try:
        from email.mime.text import MIMEText
        import smtplib
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From']    = EMAIL_FROM or SMTP_USER
        msg['To']      = EMAIL_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
        log.info('Email alert sent')
    except Exception as e:
        log.error(f'Email alert failed: {e}')


# ── Main ──────────────────────────────────────────────────────────────────────
def reprice():
    log.info('=' * 60)
    log.info(f'REPRICER STARTED — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info(f'fee={EBAY_FEE_RATE*100:.1f}%  min_profit=${MIN_PROFIT:.2f}  undercut={UNDERCUT_PCT*100:.0f}%')
    log.info('=' * 60)

    if not os.path.exists(STATE_FILE):
        log.warning('No lister_state.json found')
        return

    with open(STATE_FILE, encoding='utf-8') as f:
        state = json.load(f)

    listings = state.get('listings', {})
    if not listings:
        log.info('No active listings to reprice')
        return

    log.info(f'Checking {len(listings)} listings...')

    bg_prices  = fetch_booksgoat_prices()
    user_token = get_user_token()
    app_token  = get_app_token()

    repriced  = []
    delisted  = []
    unchanged = 0

    for isbn13, listing in list(listings.items()):
        title         = listing.get('title', isbn13)
        listing_price = listing.get('listing_price', 0)
        offer_id      = listing.get('offer_id')
        current_cost  = listing.get('cost') or bg_prices.get(isbn13, {}).get('cost', 0)

        if not listing_price:
            log.info(f'  {title[:50]}: no listing price recorded — skipping')
            unchanged += 1
            continue

        if not offer_id:
            log.info(f'  {title[:50]}: MANUAL listing — alerting only if unprofitable')

        # Get target price via eBay comps → Amazon fallback
        target_price, method, conf = calc_target_price(
            isbn13, current_cost, listing_price, bg_prices, app_token
        )

        profit = round(target_price * (1 - EBAY_FEE_RATE) - current_cost, 2)

        if profit < MIN_PROFIT:
            reason = (
                f'Unprofitable — profit ${profit:.2f} at ${target_price:.2f} '
                f'(cost ${current_cost:.2f}, method: {method})'
            )
            if offer_id:
                log.info(f'  {title[:50]}: AUTO-DELIST — {reason}')
                if delist_offer(user_token, isbn13, offer_id=offer_id):
                    delisted.append({**listing, 'reason': reason})
                    del state['listings'][isbn13]
                    if isbn13 in state.get('listed_isbns', []):
                        state['listed_isbns'].remove(isbn13)
                    log.info(f'  ✅ Delisted')
                else:
                    log.error(f'  ❌ Delist failed')
            else:
                log.info(f'  {title[:50]}: MANUAL — alerting: {reason}')
                delisted.append({**listing, 'reason': f'MANUAL — delist in Seller Hub. {reason}'})

        elif abs(target_price - listing_price) > 0.01:
            log.info(f'  {title[:50]}: ${listing_price:.2f} → ${target_price:.2f} '
                     f'profit=${profit:.2f}  method={method}')
            if offer_id:
                if update_offer_price(user_token, offer_id, target_price):
                    repriced.append({
                        'title':     title,
                        'isbn13':    isbn13,
                        'old_price': listing_price,
                        'new_price': target_price,
                        'profit':    profit,
                        'method':    method,
                    })
                    state['listings'][isbn13]['listing_price'] = target_price
                    state['listings'][isbn13]['profit']        = profit
                    log.info(f'  ✅ Repriced')
                else:
                    log.error(f'  ❌ Reprice failed ({isbn13})')
            else:
                log.info(f'  MANUAL listing — cannot reprice via API')
                unchanged += 1
        else:
            log.info(f'  {title[:50]}: price OK (${listing_price:.2f}, profit=${profit:.2f})')
            unchanged += 1

        time.sleep(0.5)

    state['last_repricer_run'] = datetime.now().isoformat()
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)

    log.info('=' * 60)
    log.info(f'DONE: {len(repriced)} repriced | {len(delisted)} delisted | {unchanged} unchanged')
    log.info('=' * 60)

    if repriced or delisted:
        send_alert(repriced, delisted)


if __name__ == '__main__':
    reprice()
