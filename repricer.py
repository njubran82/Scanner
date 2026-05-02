#!/usr/bin/env python3
"""
repricer.py v4 — decoupled reprice + delist
Reads from booksgoat_enhanced.csv. Reprices all status=active rows.

Mode flag (env REPRICER_MODE):
  - live         → reprice + delist (current behavior)
  - report_only  → reprice runs, delist candidates emailed (DEFAULT — safe)
  - dry_run      → no API changes at all, full report emailed (for testing)

Fixes from v3:
  - 5-qty cost basis (was 10-qty)
  - MIN_PROFIT = $12 (was $5, matching spec)
  - Back Mechanic added to blocklist
"""

import os, csv, base64, time, logging, requests, smtplib
from io import StringIO
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from dotenv import load_dotenv
from protection_patch import should_delist

load_dotenv()

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
BOOKSGOAT_CSV_URL  = os.getenv('BOOKSGOAT_CSV_URL')

SMTP_HOST     = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT     = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER     = os.getenv('SMTP_USER')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
EMAIL_FROM    = os.getenv('EMAIL_FROM', SMTP_USER)
EMAIL_TO      = os.getenv('EMAIL_TO', SMTP_USER)

# ── MODE FLAG ─────────────────────────────────────────────────────────────
MODE = os.getenv('REPRICER_MODE', 'report_only').lower()
if MODE not in ('live', 'report_only', 'dry_run'):
    raise ValueError(f"REPRICER_MODE must be live|report_only|dry_run, got: {MODE}")

MIN_PROFIT      = 5.00
EBAY_FEE_RATE  = 0.153
UNDERCUT_PCT   = 0.12
AMAZON_CAP     = 0.95
MIN_PRICE_MULT = 1.20

CSV_PATH = Path('booksgoat_enhanced.csv')
LOG_FILE = 'repricer_log.txt'

BLOCKLIST = {
    '9781119143642', '9781260460445', '9780990873853', '9781119826798',
    '9781628257830', '9780415708234', '9781591264507', '9780091816971',
    '9781108724265', '9780415898058', '9781118115121', '9780393979503',
    '9781119141983',
    '9780973501827',  # Back Mechanic — min qty 10
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
    """Uses 5-qty as cost basis (corrected from 10-qty)."""
    if not BOOKSGOAT_CSV_URL:
        return {}
    r = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
    r.raise_for_status()
    prices = {}
    for row in csv.DictReader(StringIO(r.text)):
        try:
            isbn = row.get('ISBN-13', '').strip().replace('-', '')
            cost_raw = (row.get('5 Qty') or row.get('10 Qty', '')).replace('$', '').replace(',', '').strip()
            amazon_raw = row.get('Amazon Price', '').replace('$', '').replace(',', '').strip()
            if isbn and cost_raw:
                prices[isbn] = {
                    'cost':         float(cost_raw),
                    'amazon_price': float(amazon_raw) if amazon_raw and amazon_raw != 'N/A' else None
                }
        except Exception:
            pass
    log.info(f'Fetched {len(prices)} BooksGoat prices (5-qty basis)')
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
                try:
                    prices.append(float(item['price']['value']))
                except Exception:
                    pass
            if prices: break
        except Exception as e:
            log.warning(f'  GTIN error {isbn_val}: {e}')
    conf = 'HIGH' if len(prices) >= 3 else 'MEDIUM' if prices else 'NONE'
    return prices, conf


def filter_comps(prices, cost, multiplier=1.1):
    if not prices or not cost:
        return prices
    floor = cost * multiplier
    filtered = [p for p in prices if p >= floor]
    return filtered if filtered else prices


def calc_target(isbn, cost, amazon_price, app_token):
    comps, conf = get_ebay_comps(isbn, app_token)
    if comps:
        comps = filter_comps(comps, cost)
        target = round(min(comps) * (1 - UNDERCUT_PCT), 2)
        method = f'EBAY_COMP ({conf}, n={len(comps)})'
    elif amazon_price:
        target = round(amazon_price * (1 - UNDERCUT_PCT), 2)
        method = 'AMAZON_FALLBACK'
        conf = 'FALLBACK'
    else:
        return None, None, 'NO_ANCHOR'
    if amazon_price and target > amazon_price * AMAZON_CAP:
        target = round(amazon_price * AMAZON_CAP, 2)
        method += ' [amazon capped]'
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
    current = r.json()
    policies = current.get('listingPolicies', {})
    location = current.get('merchantLocationKey', '')
    category = current.get('categoryId', '267')
    r2 = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                 'Content-Language': 'en-US'},
        json={'pricingSummary': {'price': {'currency': 'USD', 'value': f'{new_price:.2f}'}},
              'listingPolicies': policies, 'merchantLocationKey': location, 'categoryId': category},
        timeout=15)
    return r2.status_code in [200, 204]


def delist_offer(token, offer_id):
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
    all_rows = list(rows.values())
    all_fields = list(dict.fromkeys(k for r in all_rows for k in r))
    tmp = CSV_PATH.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(all_rows)
    tmp.replace(CSV_PATH)


def send_email(subject, body):
    if not SMTP_USER or not SMTP_PASSWORD:
        log.warning('SMTP not configured — email skipped')
        return
    try:
        msg = MIMEText(body, 'plain')
        msg['Subject'] = subject
        msg['From']    = EMAIL_FROM
        msg['To']      = EMAIL_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info(f'Email sent: {subject}')
    except Exception as e:
        log.warning(f'Email failed: {e}')


def reprice():
    log.info('=' * 70)
    log.info(f'REPRICER v4 STARTED — MODE={MODE.upper()} — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info(f'fee={EBAY_FEE_RATE*100:.1f}% min_profit=${MIN_PROFIT:.2f} undercut={UNDERCUT_PCT*100:.0f}%')
    log.info('=' * 70)

    rows = load_csv()

    active_count = sum(1 for r in rows.values() if r.get('status') == 'active')
    if active_count < 50:
        log.error(f'SAFETY ABORT: only {active_count} active rows — exiting.')
        return

    active = {isbn: row for isbn, row in rows.items()
              if row.get('status') == 'active' and row.get('offer_id') and isbn not in BLOCKLIST}
    log.info(f'Checking {len(active)} active listings...')

    bg_prices  = fetch_booksgoat_prices()
    user_token = get_user_token() if MODE != 'dry_run' else None
    app_token  = get_app_token()

    repriced          = []
    delist_candidates = []   # books that should be delisted (per should_delist)
    delisted          = []   # books actually delisted (live mode only)
    unchanged         = 0
    errors            = 0
    csv_dirty         = False

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

        # ── DELIST DECISION ────────────────────────────────────────────────
        if should_delist(row, profit):
            candidate = {
                'isbn':         isbn,
                'title':        title,
                'offer_id':     offer_id,
                'cost':         current_cost,
                'listing_price': listing_price,
                'target':       target,
                'profit':       profit,
                'method':       method,
            }
            delist_candidates.append(candidate)

            if MODE == 'live':
                log.info(f'  {title[:50]}: DELIST — profit ${profit:.2f}')
                if delist_offer(user_token, offer_id):
                    rows[isbn]['status']        = 'delisted'
                    rows[isbn]['delisted_at']   = datetime.now(timezone.utc).isoformat()
                    rows[isbn]['delist_reason'] = 'unprofitable'
                    delisted.append(candidate)
                    csv_dirty = True
                    save_csv(rows)
                    log.info(f'  ✅ Delisted')
                else:
                    log.error(f'  ❌ Delist failed')
                    errors += 1
            else:
                log.info(f'  {title[:50]}: DELIST CANDIDATE — profit ${profit:.2f} [{MODE}: not delisted]')

        # ── REPRICE ─────────────────────────────────────────────────────────
        elif abs(target - listing_price) > 0.01:
            log.info(f'  {title[:50]}: ${listing_price:.2f} → ${target:.2f} profit=${profit:.2f}')

            if MODE == 'dry_run':
                log.info(f'  [dry_run: would reprice]')
                repriced.append({'title': title, 'old': listing_price, 'new': target, 'profit': profit})
            else:
                if update_offer_price(user_token, offer_id, target):
                    rows[isbn]['sell_price'] = str(target)
                    repriced.append({'title': title, 'old': listing_price, 'new': target, 'profit': profit})
                    csv_dirty = True
                    save_csv(rows)
                    log.info(f'  ✅ Repriced')
                else:
                    log.error(f'  ❌ Reprice failed ({isbn})')
                    errors += 1
        else:
            unchanged += 1

        time.sleep(0.5)

    if csv_dirty:
        save_csv(rows)

    log.info('=' * 70)
    log.info(f'DONE [{MODE}]: {len(repriced)} repriced | '
             f'{len(delist_candidates)} delist candidates | '
             f'{len(delisted)} actually delisted | {unchanged} unchanged')
    log.info('=' * 70)

    # ── EMAIL REPORT ───────────────────────────────────────────────────────
    if delist_candidates or MODE != 'live':
        body = [
            f"REPRICER REPORT — Mode: {MODE.upper()}",
            f"Run: {datetime.now():%Y-%m-%d %H:%M}",
            "",
            f"Repriced:          {len(repriced)}",
            f"Delist candidates: {len(delist_candidates)}",
            f"Actually delisted: {len(delisted)} (live mode only)",
            f"Unchanged:         {unchanged}",
            f"Errors:            {errors}",
            "",
        ]

        if delist_candidates:
            mode_note = "ACTION REQUIRED — Delist these manually in Seller Hub:" if MODE != 'live' \
                        else "DELISTED automatically:"
            body.append(f"━━━ {mode_note} ━━━")
            body.append("")
            for c in delist_candidates:
                body.append(f"• {c['title'][:55]}")
                body.append(f"  ISBN: {c['isbn']} | Listed: ${c['listing_price']:.2f} | "
                           f"Cost: ${c['cost']:.2f} | Computed profit: ${c['profit']:.2f}")
                body.append(f"  https://www.ebay.com/sh/lst/active?keyword={c['isbn']}")
                body.append("")

        if repriced and len(repriced) <= 30:
            body.append("━━━ Repriced ━━━")
            for r in repriced:
                body.append(f"  {r['title'][:50]} | ${r['old']:.2f} → ${r['new']:.2f} | profit ${r['profit']:.2f}")

        subject_prefix = {
            'live':        '🔧 Repricer (LIVE)',
            'report_only': '⚠️ Repricer — DELIST CANDIDATES' if delist_candidates else '🔧 Repricer (report)',
            'dry_run':     '🧪 Repricer (DRY RUN)',
        }[MODE]

        send_email(
            subject=f"{subject_prefix}: {len(delist_candidates)} delist, {len(repriced)} repriced",
            body='\n'.join(body),
        )


if __name__ == '__main__':
    reprice()
