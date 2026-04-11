"""
tracker.py — Daily BooksGoat Availability & Price Tracker
Runs LOCALLY via Windows Task Scheduler (BooksGoat blocks GitHub IP ranges).

For each active listing it:
  1. Scrapes the BooksGoat product URL directly
  2. Checks if the item is still in stock
  3. Compares the current BooksGoat price against the cost stored in lister_state.json
  4. Auto-delists from eBay if OOS or if current cost would make profit < $5
  5. Commits updated lister_state.json back to GitHub
"""

import os, json, base64, time, re, logging, subprocess, requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
SENDGRID_API_KEY   = os.getenv('SENDGRID_API_KEY')
ALERT_EMAIL_TO     = os.getenv('ALERT_EMAIL_TO')
ALERT_EMAIL_FROM   = os.getenv('ALERT_EMAIL_FROM')
TWILIO_SID         = os.getenv('TWILIO_SID')
TWILIO_AUTH        = os.getenv('TWILIO_AUTH')
TWILIO_FROM        = os.getenv('TWILIO_FROM')
TWILIO_TO          = os.getenv('TWILIO_TO')

MIN_PROFIT    = 5.0
EBAY_FEE_RATE = 0.1325
UNDERCUT_PCT  = 0.125          # Same as lister — 12.5% below median
STATE_FILE    = 'lister_state.json'
LOG_FILE      = 'tracker_log.txt'

# Rotate user agents to reduce block risk
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


# ── Git Sync ─────────────────────────────────────────────────────────────────
def git_pull():
    try:
        result = subprocess.run(
            ['git', 'pull', '--rebase'],
            capture_output=True, text=True, timeout=30
        )
        log.info(f"git pull: {result.stdout.strip() or result.stderr.strip()}")
    except Exception as e:
        log.warning(f"git pull failed (continuing anyway): {e}")

def git_push(message):
    try:
        subprocess.run(['git', 'add', STATE_FILE, LOG_FILE], timeout=10)
        result = subprocess.run(
            ['git', 'commit', '-m', message],
            capture_output=True, text=True, timeout=10
        )
        if 'nothing to commit' in result.stdout:
            log.info("No state changes to commit.")
            return
        subprocess.run(['git', 'push'], timeout=30)
        log.info(f"State pushed to GitHub: {message}")
    except Exception as e:
        log.warning(f"git push failed: {e}")


# ── BooksGoat Scraper ─────────────────────────────────────────────────────────
def scrape_booksgoat(url):
    """
    Scrapes a BooksGoat product page and returns:
      { 'in_stock': bool, 'price': float or None }

    Returns None if the page could not be fetched.
    """
    try:
        headers = {
            'User-Agent': next_ua(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.booksgoat.com/',
        }
        r = requests.get(url, headers=headers, timeout=15)

        if r.status_code == 404:
            return {'in_stock': False, 'price': None, 'reason': 'Page 404'}

        if r.status_code != 200:
            log.warning(f"  BooksGoat returned {r.status_code} for {url}")
            return None

        html = r.text

        # ── Out of stock signals ──────────────────────────────────────────────
        oos_patterns = [
            r'out[- ]of[- ]stock',
            r'unavailable',
            r'sold\s+out',
            r'not\s+available',
            r'product[- ]not[- ]found',
        ]
        for pat in oos_patterns:
            if re.search(pat, html, re.IGNORECASE):
                return {'in_stock': False, 'price': None, 'reason': f'OOS pattern: {pat}'}

        # ── Extract price ─────────────────────────────────────────────────────
        # BooksGoat typically shows price in patterns like:
        #   $49.99   or   "price":"49.99"   or  <span ...>$49.99</span>
        price = None

        # JSON price in page source
        m = re.search(r'"price"\s*:\s*"?(\d+\.\d{2})"?', html)
        if m:
            price = float(m.group(1))

        # Fallback: dollar amount near "price" keyword
        if not price:
            m = re.search(r'(?:price|Price)[^$]{0,80}\$\s*(\d+\.\d{2})', html)
            if m:
                price = float(m.group(1))

        # Fallback: any prominent dollar amount
        if not price:
            amounts = re.findall(r'\$\s*(\d{1,4}\.\d{2})', html)
            # Filter to reasonable book prices ($5 - $500)
            amounts = [float(a) for a in amounts if 5 <= float(a) <= 500]
            if amounts:
                price = min(amounts)   # Take lowest (most likely the book price)

        if price is None:
            log.warning(f"  Could not extract price from {url}")
            return {'in_stock': True, 'price': None, 'reason': 'Price parse failed'}

        return {'in_stock': True, 'price': price, 'reason': 'OK'}

    except requests.exceptions.ConnectionError:
        log.error(f"  Connection error scraping {url}")
        return None
    except Exception as e:
        log.error(f"  Scrape error for {url}: {e}")
        return None


# ── eBay Auth ────────────────────────────────────────────────────────────────
def get_ebay_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={
            'Authorization': f'Basic {creds}',
            'Content-Type': 'application/x-www-form-urlencoded'
        },
        data={
            'grant_type': 'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': (
                'https://api.ebay.com/oauth/api_scope '
                'https://api.ebay.com/oauth/api_scope/sell.inventory'
            )
        }
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f"eBay token error: {data}")
    return data['access_token']


# ── eBay Delist ───────────────────────────────────────────────────────────────
def delist_book(token, isbn13, offer_id=None):
    """
    Withdraw the eBay offer for this ISBN.
    Uses stored offer_id if available; otherwise looks it up by SKU.
    """
    if not offer_id:
        try:
            r = requests.get(
                'https://api.ebay.com/sell/inventory/v1/offer',
                headers={
                    'Authorization': f'Bearer {token}',
                    'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'
                },
                params={'sku': isbn13},
                timeout=10
            )
            offers = r.json().get('offers', [])
            if offers:
                offer_id = offers[0].get('offerId')
        except Exception as e:
            log.error(f"  Offer lookup failed for {isbn13}: {e}")

    if not offer_id:
        log.error(f"  Cannot delist {isbn13}: no offer_id found on eBay")
        return False

    r = requests.delete(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={'Authorization': f'Bearer {token}'},
        timeout=10
    )
    return r.status_code in [200, 204]


# ── Profit Check ─────────────────────────────────────────────────────────────
def is_still_profitable(listing, current_cost):
    """
    For auto-listed books: use stored listing_price vs current_cost.
    For manual listings (listing_price is None): use current_cost * markup
    to estimate what eBay price would be, and check against $5 profit floor.
    """
    listing_price = listing.get('listing_price')

    if listing_price is None:
        # Manual listing — estimate eBay price as current_cost / (1 - UNDERCUT_PCT) * some multiplier
        # Conservative: assume they listed at ~2x cost. If current cost > listing_price * 0.75, flag it.
        # Since we don't know their eBay price, delist only if cost went UP significantly (>10%)
        original_cost = listing.get('cost')
        if original_cost and current_cost > original_cost * 1.10:
            return False, f"Cost rose from ${original_cost:.2f} to ${current_cost:.2f} (+{((current_cost/original_cost)-1)*100:.0f}%)"
        return True, None

    # Auto-listed: exact profit calculation
    profit = listing_price - current_cost - (listing_price * EBAY_FEE_RATE)
    if profit < MIN_PROFIT:
        return False, f"Profit dropped to ${profit:.2f} (cost now ${current_cost:.2f})"
    return True, None


# ── Alerts ────────────────────────────────────────────────────────────────────
def send_alerts(delisted, price_changes):
    lines = []
    if delisted:
        lines.append("=== DELISTED ===")
        lines += [f"  - {d['title']} ({d['isbn13']}): {d['reason']}" for d in delisted]
    if price_changes:
        lines.append("\n=== PRICE CHANGES (still listed) ===")
        lines += [f"  - {p['title']} ({p['isbn13']}): {p['note']}" for p in price_changes]

    if not lines:
        return

    body    = "\n".join(lines)
    subject = f"[Tracker] {len(delisted)} delisted, {len(price_changes)} price changes"

    if SENDGRID_API_KEY and ALERT_EMAIL_TO:
        try:
            requests.post(
                'https://api.sendgrid.com/v3/mail/send',
                headers={
                    'Authorization': f'Bearer {SENDGRID_API_KEY}',
                    'Content-Type':  'application/json'
                },
                json={
                    'personalizations': [{'to': [{'email': ALERT_EMAIL_TO}]}],
                    'from':    {'email': ALERT_EMAIL_FROM},
                    'subject': subject,
                    'content': [{'type': 'text/plain', 'value': body}]
                },
                timeout=10
            )
            log.info("Email alert sent")
        except Exception as e:
            log.error(f"Email alert failed: {e}")

    if TWILIO_SID and TWILIO_AUTH:
        try:
            requests.post(
                f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json',
                auth=(TWILIO_SID, TWILIO_AUTH),
                data={
                    'From': TWILIO_FROM,
                    'To':   TWILIO_TO,
                    'Body': f"[Tracker] {len(delisted)} delisted, {len(price_changes)} price changes. Check email."
                },
                timeout=10
            )
            log.info("SMS alert sent")
        except Exception as e:
            log.error(f"SMS alert failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def track():
    log.info("=" * 60)
    log.info("TRACKER STARTED — " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    log.info("=" * 60)

    git_pull()

    if not os.path.exists(STATE_FILE):
        log.warning("lister_state.json not found — nothing to track.")
        return

    with open(STATE_FILE, encoding='utf-8') as f:
        state = json.load(f)

    listings = state.get('listings', {})
    if not listings:
        log.info("No active listings to track.")
        return

    log.info(f"Tracking {len(listings)} listings...")

    token         = get_ebay_token()
    delisted      = []
    price_changes = []
    scrape_errors = []

    for isbn13, listing in list(listings.items()):
        title = listing.get('title', isbn13)[:55]
        url   = listing.get('booksgoat_url', '')

        if not url:
            log.warning(f"  No BooksGoat URL for {isbn13} — skipping")
            continue

        log.info(f"Checking: {title}")
        result = scrape_booksgoat(url)

        # If scrape completely failed (network error etc), skip — don't delist on uncertainty
        if result is None:
            log.warning(f"  Scrape failed — skipping {isbn13} to avoid false delist")
            scrape_errors.append(isbn13)
            time.sleep(2)
            continue

        delist_reason = None

        # ── Out of stock ──────────────────────────────────────────────────────
        if not result['in_stock']:
            delist_reason = f"Out of stock on BooksGoat ({result.get('reason','')})"

        # ── Price increased beyond threshold ──────────────────────────────────
        elif result['price'] is not None:
            current_cost  = result['price']
            original_cost = listing.get('cost')

            profitable, profit_reason = is_still_profitable(listing, current_cost)

            if not profitable:
                delist_reason = profit_reason
            elif original_cost and current_cost != original_cost:
                # Price changed but still profitable — log it
                direction = "↑" if current_cost > original_cost else "↓"
                note = f"Price {direction} ${original_cost:.2f} → ${current_cost:.2f}"
                price_changes.append({**listing, 'note': note})
                log.info(f"  ⚠️  {note} (still profitable)")
                # Update stored cost to current
                state['listings'][isbn13]['cost'] = current_cost

        # ── Delist if needed ──────────────────────────────────────────────────
        if delist_reason:
            log.info(f"  Delisting: {delist_reason}")
            success = delist_book(token, isbn13, offer_id=listing.get('offer_id'))

            if success:
                delisted.append({**listing, 'reason': delist_reason})
                del state['listings'][isbn13]
                if isbn13 in state.get('listed_isbns', []):
                    state['listed_isbns'].remove(isbn13)
                log.info(f"  ✅ Delisted: {title}")
            else:
                log.error(f"  ❌ Delist failed for {isbn13}")

        time.sleep(1.5)   # Polite delay between BooksGoat requests

    state['last_tracker_run'] = datetime.now().isoformat()

    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)

    log.info("=" * 60)
    log.info(f"TRACKER DONE: {len(delisted)} delisted | {len(price_changes)} price changes | {len(scrape_errors)} scrape errors")
    log.info("=" * 60)

    git_push(
        f"Tracker {datetime.now().strftime('%Y-%m-%d')}: "
        f"{len(delisted)} delisted, {len(price_changes)} price changes"
    )

    if delisted or price_changes:
        send_alerts(delisted, price_changes)


if __name__ == '__main__':
    track()
