"""
tracker.py — Daily BooksGoat Availability & Price Tracker
Runs LOCALLY via Windows Task Scheduler (BooksGoat blocks GitHub IP ranges).

For each active listing it:
  1. Checks BooksGoat CSV for current price and availability
  2. Scrapes the product URL as a secondary OOS check
  3a. AUTO-LISTED books (have offer_id): auto-delists if OOS or unprofitable
  3b. MANUAL listings (offer_id is null): sends email alert only — you delist manually

State is synced from/to GitHub before and after each run.
"""

import os, csv, json, base64, time, re, logging, subprocess, requests, smtplib
from io import StringIO
from email.mime.text import MIMEText
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

MIN_PROFIT    = 5.0
EBAY_FEE_RATE = 0.1325
STATE_FILE    = 'lister_state.json'
LOG_FILE      = 'tracker_log.txt'

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
]
_ua_index = 0

def next_ua():
    global _ua_index
    ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
    _ua_index += 1
    return ua

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# -- Git Sync ------------------------------------------------------------------
def git_pull():
    try:
        result = subprocess.run(['git', 'pull', '--rebase'], capture_output=True, text=True, timeout=30)
        log.info(f"git pull: {result.stdout.strip() or result.stderr.strip()}")
    except Exception as e:
        log.warning(f"git pull failed: {e}")

def git_push(message):
    try:
        subprocess.run(['git', 'add', STATE_FILE, LOG_FILE], timeout=10)
        result = subprocess.run(['git', 'commit', '-m', message], capture_output=True, text=True, timeout=10)
        if 'nothing to commit' in result.stdout:
            log.info("No changes to commit")
            return
        subprocess.run(['git', 'push'], timeout=30)
        log.info(f"Pushed: {message}")
    except Exception as e:
        log.warning(f"git push failed: {e}")


# -- BooksGoat CSV Check -------------------------------------------------------
def fetch_booksgoat_prices():
    try:
        r = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
        r.raise_for_status()
        reader = csv.DictReader(StringIO(r.text))
        prices = {}
        for row in reader:
            try:
                isbn = row.get('ISBN-13', '').strip().replace('-', '').replace(' ', '')
                cost_raw = row.get('5 Qty', '').replace('$', '').replace(',', '').strip()
                amazon_raw = row.get('Amazon Price', '').replace('$', '').replace(',', '').strip()
                if isbn and cost_raw:
                    prices[isbn] = {
                        'cost': float(cost_raw),
                        'amazon_price': float(amazon_raw) if amazon_raw and amazon_raw != 'N/A' else None
                    }
            except Exception:
                pass
        log.info(f"Fetched {len(prices)} BooksGoat prices from CSV")
        return prices
    except Exception as e:
        log.error(f"BooksGoat CSV fetch failed: {e}")
        return None


# -- BooksGoat URL Scrape (secondary OOS check) --------------------------------
def scrape_booksgoat_url(url):
    if not url:
        return None
    try:
        r = requests.get(url, headers={'User-Agent': next_ua()}, timeout=15)
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            return None
        html = r.text
        oos_patterns = [
            r'out[- ]of[- ]stock', r'unavailable', r'sold\s+out',
            r'not\s+available', r'product[- ]not[- ]found',
        ]
        for pat in oos_patterns:
            if re.search(pat, html, re.IGNORECASE):
                return False
        return True
    except Exception:
        return None


# -- eBay Auth -----------------------------------------------------------------
def get_ebay_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type': 'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': 'https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.inventory'
        }
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f"Token error: {data}")
    return data['access_token']


# -- eBay Delist ---------------------------------------------------------------
def delist_book(token, isbn13, offer_id=None):
    if not offer_id:
        try:
            r = requests.get(
                'https://api.ebay.com/sell/inventory/v1/offer',
                headers={'Authorization': f'Bearer {token}', 'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'},
                params={'sku': isbn13}, timeout=10
            )
            offers = r.json().get('offers', [])
            if offers:
                offer_id = offers[0].get('offerId')
        except Exception as e:
            log.error(f"  Offer lookup failed: {e}")
    if not offer_id:
        return False
    r = requests.delete(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={'Authorization': f'Bearer {token}'}, timeout=10
    )
    return r.status_code in [200, 204]


# -- Email Alert ---------------------------------------------------------------
def send_alerts(auto_delisted, manual_alerts, price_changes):
    lines = []

    if auto_delisted:
        lines.append("=== AUTO-DELISTED FROM EBAY ===")
        for d in auto_delisted:
            lines.append(f"  {d['title'][:55]} ({d['isbn13']})")
            lines.append(f"  Reason: {d['reason']}\n")

    if manual_alerts:
        lines.append("=== ACTION REQUIRED - MANUAL LISTINGS ===")
        lines.append("Please delist these in eBay Seller Hub:")
        for m in manual_alerts:
            lines.append(f"  {m['title'][:55]} ({m['isbn13']})")
            lines.append(f"  Reason: {m['reason']}")
            lines.append(f"  eBay Listing ID: {m.get('listing_id', 'unknown')}\n")

    if price_changes:
        lines.append("=== PRICE CHANGES (still profitable - no action needed) ===")
        for p in price_changes:
            lines.append(f"  {p['title'][:55]} ({p['isbn13']})")
            lines.append(f"  {p['note']}\n")

    if not lines:
        return

    body    = "\n".join(lines)
    subject = (
        f"[Tracker] {len(auto_delisted)} auto-delisted | "
        f"{len(manual_alerts)} manual action needed | "
        f"{len(price_changes)} price changes"
    )

    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.warning("Email not configured")
        return
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From']    = EMAIL_FROM or SMTP_USER
        msg['To']      = EMAIL_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
        log.info("Email alert sent")
    except Exception as e:
        log.error(f"Email alert failed: {e}")


# -- Main ----------------------------------------------------------------------
def track():
    log.info("=" * 60)
    log.info("TRACKER STARTED - " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    log.info("=" * 60)

    git_pull()

    if not os.path.exists(STATE_FILE):
        log.warning("lister_state.json not found")
        return

    with open(STATE_FILE, encoding='utf-8') as f:
        state = json.load(f)

    listings = state.get('listings', {})
    if not listings:
        log.info("No active listings to track")
        return

    log.info(f"Tracking {len(listings)} listings...")

    bg_prices = fetch_booksgoat_prices()
    if bg_prices is None:
        log.error("BooksGoat CSV unavailable - aborting to avoid false delists")
        return

    token         = get_ebay_token()
    auto_delisted = []
    manual_alerts = []
    price_changes = []

    for isbn13, listing in list(listings.items()):
        title         = listing.get('title', isbn13)[:55]
        offer_id      = listing.get('offer_id')
        listing_price = listing.get('listing_price', 0)
        booksgoat_url = listing.get('booksgoat_url', '')
        is_auto       = bool(offer_id)

        log.info(f"  {'[AUTO]' if is_auto else '[MANUAL]'} {title}")

        delist_reason = None
        price_note    = None

        if isbn13 not in bg_prices:
            # Not in CSV - primary OOS signal, confirm with URL scrape
            url_check = scrape_booksgoat_url(booksgoat_url)
            if url_check is False:
                delist_reason = "Out of stock (not in BooksGoat sheet + URL confirms OOS)"
            elif url_check is None:
                delist_reason = "Out of stock (removed from BooksGoat sheet - URL inconclusive)"
            else:
                log.warning(f"  Not in CSV but URL shows in stock - skipping for safety")
                time.sleep(1)
                continue
        else:
            bg            = bg_prices[isbn13]
            current_cost  = bg['cost']
            amazon_price  = bg.get('amazon_price')
            original_cost = listing.get('cost')

            # Use Amazon-adjusted price for profit check
            effective_price = listing_price
            if amazon_price and amazon_price < listing_price:
                effective_price = round(amazon_price * 0.95, 2)

            profit = effective_price - current_cost - (effective_price * EBAY_FEE_RATE)

            if profit < MIN_PROFIT:
                delist_reason = (
                    f"Unprofitable - profit ${profit:.2f} "
                    f"(cost ${current_cost:.2f}, effective price ${effective_price:.2f})"
                )
            elif original_cost and abs(current_cost - original_cost) > 0.50:
                direction = "up" if current_cost > original_cost else "down"
                price_note = f"BooksGoat cost went {direction}: ${original_cost:.2f} -> ${current_cost:.2f} | profit now ${profit:.2f}"
                state['listings'][isbn13]['cost'] = current_cost

        if delist_reason:
            if is_auto:
                log.info(f"  Auto-delisting: {delist_reason}")
                success = delist_book(token, isbn13, offer_id=offer_id)
                if success:
                    auto_delisted.append({**listing, 'reason': delist_reason})
                    del state['listings'][isbn13]
                    if isbn13 in state.get('listed_isbns', []):
                        state['listed_isbns'].remove(isbn13)
                    log.info(f"  Delisted successfully")
                else:
                    log.error(f"  Auto-delist failed - flagging for manual action")
                    manual_alerts.append({**listing, 'reason': f"Auto-delist FAILED - {delist_reason}"})
            else:
                log.info(f"  Manual listing - alerting: {delist_reason}")
                manual_alerts.append({**listing, 'reason': delist_reason})

        elif price_note:
            log.info(f"  {price_note}")
            price_changes.append({**listing, 'note': price_note})

        time.sleep(1.5)

    state['last_tracker_run'] = datetime.now().isoformat()
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)

    log.info("=" * 60)
    log.info(
        f"TRACKER DONE: {len(auto_delisted)} auto-delisted | "
        f"{len(manual_alerts)} manual alerts | "
        f"{len(price_changes)} price changes"
    )
    log.info("=" * 60)

    git_push(
        f"Tracker {datetime.now().strftime('%Y-%m-%d')}: "
        f"{len(auto_delisted)} delisted, {len(manual_alerts)} alerts"
    )

    if auto_delisted or manual_alerts or price_changes:
        send_alerts(auto_delisted, manual_alerts, price_changes)


if __name__ == '__main__':
    track()
