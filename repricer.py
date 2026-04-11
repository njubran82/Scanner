"""
repricer.py — One-time and recurring price updater
Reads all active listings from lister_state.json, compares each against
the BooksGoat Amazon price, and either:
  - Reprices the eBay offer to undercut Amazon by 5%
  - Delists if repriced profit falls below $5
  - Leaves alone if already cheaper than Amazon

Run manually: python repricer.py
Also added to scanner.yml to run weekly after the lister.
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

MIN_PROFIT         = 5.0
EBAY_FEE_RATE      = 0.1325
AMAZON_UNDERCUT    = 0.95      # List at 5% below Amazon
STATE_FILE         = 'lister_state.json'
LOG_FILE           = 'repricer_log.txt'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── eBay Auth ────────────────────────────────────────────────────────────────
def get_ebay_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'},
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
        raise RuntimeError(f"Token error: {data}")
    return data['access_token']


# ── BooksGoat Prices ──────────────────────────────────────────────────────────
def fetch_amazon_prices():
    """Returns dict of isbn13 -> {'cost': float, 'amazon_price': float or None}"""
    r = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(StringIO(r.text))
    prices = {}
    for row in reader:
        try:
            isbn = row.get('ISBN-13', '').strip().replace('-', '')
            cost_raw = row.get('5 Qty', '').replace('$', '').replace(',', '').strip()
            amazon_raw = row.get('Amazon Price', '').replace('$', '').replace(',', '').strip()
            if not isbn or not cost_raw:
                continue
            prices[isbn] = {
                'cost': float(cost_raw),
                'amazon_price': float(amazon_raw) if amazon_raw and amazon_raw != 'N/A' else None
            }
        except Exception:
            pass
    log.info(f"Fetched {len(prices)} BooksGoat prices")
    return prices


# ── eBay Offer Update ────────────────────────────────────────────────────────
def update_offer_price(token, offer_id, new_price):
    """Update the price on an existing eBay offer."""
    r = requests.get(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={'Authorization': f'Bearer {token}'},
        timeout=10
    )
    if r.status_code != 200:
        log.error(f"  Could not fetch offer {offer_id}: {r.text[:200]}")
        return False

    offer_data = r.json()
    offer_data['pricingSummary'] = {
        'price': {'currency': 'USD', 'value': f'{new_price:.2f}'}
    }
    # Remove read-only fields
    for field in ['offerId', 'status', 'listing', 'marketplaceId']:
        offer_data.pop(field, None)

    r2 = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US'
        },
        json=offer_data,
        timeout=15
    )
    return r2.status_code in [200, 204]


def delist_offer(token, isbn13, offer_id=None):
    """Withdraw an eBay offer by offer_id or SKU lookup."""
    if not offer_id:
        r = requests.get(
            'https://api.ebay.com/sell/inventory/v1/offer',
            headers={'Authorization': f'Bearer {token}', 'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'},
            params={'sku': isbn13}, timeout=10
        )
        offers = r.json().get('offers', [])
        if offers:
            offer_id = offers[0].get('offerId')
    if not offer_id:
        log.error(f"  No offer_id found for {isbn13}")
        return False
    r = requests.delete(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={'Authorization': f'Bearer {token}'}, timeout=10
    )
    return r.status_code in [200, 204]


# ── Alerts ───────────────────────────────────────────────────────────────────
def send_alert(repriced, delisted):
    import smtplib
    from email.mime.text import MIMEText

    lines = []
    if repriced:
        lines.append("=== REPRICED ===")
        for r in repriced:
            lines.append(f"  {r['title'][:50]} | ${r['old_price']:.2f} → ${r['new_price']:.2f} | Profit: ${r['profit']:.2f}")
    if delisted:
        lines.append("\n=== DELISTED (unprofitable vs Amazon) ===")
        for d in delisted:
            lines.append(f"  {d['title'][:50]} | {d['reason']}")

    if not lines:
        return

    body = "\n".join(lines)
    subject = f"[Repricer] {len(repriced)} repriced, {len(delisted)} delisted"

    if not all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.warning("Email not configured — skipping alert")
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


# ── Main ─────────────────────────────────────────────────────────────────────
def reprice():
    log.info("=" * 60)
    log.info("REPRICER STARTED — " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    log.info("=" * 60)

    if not os.path.exists(STATE_FILE):
        log.warning("No lister_state.json found")
        return

    with open(STATE_FILE, encoding='utf-8') as f:
        state = json.load(f)

    listings = state.get('listings', {})
    if not listings:
        log.info("No active listings to reprice")
        return

    log.info(f"Checking {len(listings)} listings...")

    bg_prices = fetch_amazon_prices()
    token     = get_ebay_token()

    repriced  = []
    delisted  = []
    unchanged = 0

    for isbn13, listing in list(listings.items()):
        title         = listing.get('title', isbn13)
        listing_price = listing.get('listing_price', 0)
        offer_id      = listing.get('offer_id')

        bg = bg_prices.get(isbn13)
        if not bg:
            log.info(f"  {title[:50]}: not in BooksGoat sheet — skipping")
            unchanged += 1
            continue

        current_cost  = bg['cost']
        amazon_price  = bg.get('amazon_price')

        # Skip if no listing price recorded (some manual listings)
        if not listing_price:
            log.info(f"  {title[:50]}: no listing price recorded — skipping")
            unchanged += 1
            continue

        # Determine target price
        target_price = listing_price  # default: keep current

        if amazon_price and amazon_price < listing_price:
            # Amazon is cheaper — reprice to undercut by 5%
            target_price = round(amazon_price * AMAZON_UNDERCUT, 2)

        # Check profitability at target price
        profit = target_price - current_cost - (target_price * EBAY_FEE_RATE)

        if profit < MIN_PROFIT:
            log.info(f"  {title[:50]}: profit ${profit:.2f} < $5 at ${target_price:.2f} — delisting")
            success = delist_offer(token, isbn13, offer_id=offer_id)
            if success:
                delisted.append({**listing, 'reason': f'Profit ${profit:.2f} after Amazon reprice'})
                del state['listings'][isbn13]
                if isbn13 in state.get('listed_isbns', []):
                    state['listed_isbns'].remove(isbn13)
                log.info(f"  ✅ Delisted: {title[:50]}")
            else:
                log.error(f"  ❌ Delist failed: {isbn13}")

        elif abs(target_price - listing_price) > 0.01:
            # Price needs updating
            log.info(f"  {title[:50]}: ${listing_price:.2f} → ${target_price:.2f} (profit: ${profit:.2f})")
            success = update_offer_price(token, offer_id, target_price) if offer_id else False
            if success:
                repriced.append({
                    'title':     title,
                    'isbn13':    isbn13,
                    'old_price': listing_price,
                    'new_price': target_price,
                    'profit':    profit
                })
                state['listings'][isbn13]['listing_price'] = target_price
                state['listings'][isbn13]['profit']        = round(profit, 2)
                log.info(f"  ✅ Repriced: {title[:50]}")
            else:
                log.error(f"  ❌ Reprice failed for {isbn13} (offer_id: {offer_id})")
        else:
            unchanged += 1

        time.sleep(0.5)

    state['last_repricer_run'] = datetime.now().isoformat()
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)

    log.info("=" * 60)
    log.info(f"REPRICER DONE: {len(repriced)} repriced | {len(delisted)} delisted | {unchanged} unchanged")
    log.info("=" * 60)

    if repriced or delisted:
        send_alert(repriced, delisted)


if __name__ == '__main__':
    reprice()
