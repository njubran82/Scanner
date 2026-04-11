"""
scanner.py — BooksGoat → eBay Opportunity Scanner
Runs weekly on GitHub Actions (Mondays)
Finds profitable books and saves to scan_opportunities.json for lister.py
"""

import os, csv, json, base64, time, logging, statistics
from io import StringIO
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
BOOKSGOAT_CSV_URL  = os.getenv('BOOKSGOAT_CSV_URL')
EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')

MIN_PROFIT         = 5.0
EBAY_FEE_RATE      = 0.1325
UNDERCUT_PCT       = 0.125        # 12.5% = midpoint of 10–20% undercut
STATE_FILE         = 'lister_state.json'
OPPORTUNITIES_FILE = 'scan_opportunities.json'
LOG_FILE           = 'scanner_log.txt'

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
        headers={
            'Authorization': f'Basic {creds}',
            'Content-Type': 'application/x-www-form-urlencoded'
        },
        data={
            'grant_type': 'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': (
                'https://api.ebay.com/oauth/api_scope '
                'https://api.ebay.com/oauth/api_scope/sell.inventory '
                'https://api.ebay.com/oauth/api_scope/sell.account'
            )
        }
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f"eBay token error: {data}")
    return data['access_token']


# ── BooksGoat Sheet ──────────────────────────────────────────────────────────
def fetch_booksgoat():
    log.info("Fetching BooksGoat sheet...")
    r = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(StringIO(r.text))
    books = []
    for row in reader:
        try:
            title = row.get('Title', '').strip()
            isbn13 = row.get('ISBN-13', '').strip().replace('-', '').replace(' ', '')
            isbn10 = row.get('ISBN-10', '').strip().replace('-', '').replace(' ', '')
            cost_raw = row.get('5 Qty', '').replace('$', '').replace(',', '').strip()
            amazon_raw = row.get('Amazon Price', '').replace('$', '').replace(',', '').strip()

            if not title or not isbn13 or not cost_raw:
                continue

            books.append({
                'title':         title,
                'isbn13':        isbn13,
                'isbn10':        isbn10,
                'cost':          float(cost_raw),
                'amazon_price':  float(amazon_raw) if amazon_raw else None,
                'booksgoat_url': f'https://www.booksgoat.com/index.php?route=product/search&search={isbn13}',
            })
        except Exception as e:
            log.warning(f"Skipping row: {e} | {row}")

    log.info(f"Fetched {len(books)} books from BooksGoat")
    return books


# ── eBay Pricing ─────────────────────────────────────────────────────────────
def get_ebay_prices(token, isbn13, isbn10, title):
    """
    Returns (median_price, confidence, num_comps)
    Confidence: HIGH (3+ listings), MEDIUM (1-2), NO_DATA (0)
    Strategy: ISBN GTIN search first, title search fallback
    Amazon used only as last resort (flagged as FALLBACK)
    """
    headers = {
        'Authorization': f'Bearer {token}',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
    }

    prices = []

    # 1. GTIN search by ISBN-13 then ISBN-10
    for isbn in [isbn13, isbn10]:
        if not isbn or len(isbn) < 10:
            continue
        try:
            r = requests.get(
                'https://api.ebay.com/buy/browse/v1/item_summary/search',
                headers=headers,
                params={
                    'gtin': isbn,
                    'filter': 'conditions:{NEW},buyingOptions:{FIXED_PRICE}',
                    'limit': 50
                },
                timeout=10
            )
            for item in r.json().get('itemSummaries', []):
                p = item.get('price', {}).get('value')
                if p:
                    prices.append(float(p))
            if prices:
                break
        except Exception as e:
            log.warning(f"GTIN search error ({isbn}): {e}")
        time.sleep(0.25)

    # 2. Title search fallback (Books category only)
    if not prices:
        try:
            r = requests.get(
                'https://api.ebay.com/buy/browse/v1/item_summary/search',
                headers=headers,
                params={
                    'q': title[:80],
                    'category_ids': '267',
                    'filter': 'conditions:{NEW},buyingOptions:{FIXED_PRICE}',
                    'limit': 20
                },
                timeout=10
            )
            for item in r.json().get('itemSummaries', []):
                p = item.get('price', {}).get('value')
                if p:
                    prices.append(float(p))
        except Exception as e:
            log.warning(f"Title search error ({title[:40]}): {e}")
        time.sleep(0.25)

    if len(prices) >= 3:
        return statistics.median(prices), 'HIGH', len(prices)
    elif len(prices) >= 1:
        return statistics.median(prices), 'MEDIUM', len(prices)
    else:
        return None, 'NO_DATA', 0


# ── Profit Formula ───────────────────────────────────────────────────────────
def calculate_listing_price(median_price):
    return round(median_price * (1 - UNDERCUT_PCT), 2)

def calculate_profit(cost, listing_price):
    fee = listing_price * EBAY_FEE_RATE
    return round(listing_price - cost - fee, 2)


# ── State ────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {'listings': {}, 'listed_isbns': []}


# ── Main ─────────────────────────────────────────────────────────────────────
def scan():
    log.info("=" * 60)
    log.info("SCANNER STARTED — " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    log.info("=" * 60)

    state         = load_state()
    already_listed = set(state.get('listed_isbns', []))
    token         = get_ebay_token()
    log.info("eBay token acquired")

    books = fetch_booksgoat()

    opportunities      = []
    skipped_listed     = 0
    skipped_no_data    = 0
    skipped_no_profit  = 0
    amazon_fallbacks   = 0

    for book in books:
        isbn13 = book['isbn13']

        if isbn13 in already_listed:
            skipped_listed += 1
            continue

        median, confidence, num_comps = get_ebay_prices(
            token, isbn13, book['isbn10'], book['title']
        )

        # Amazon fallback — only when zero eBay data at all
        if confidence == 'NO_DATA':
            if book.get('amazon_price'):
                median     = book['amazon_price']
                confidence = 'FALLBACK_AMAZON'
                num_comps  = 0
                amazon_fallbacks += 1
                log.info(f"  Amazon fallback: {book['title'][:50]}")
            else:
                skipped_no_data += 1
                continue

        listing_price = calculate_listing_price(median)

        # Amazon price check — undercut Amazon by 5% if it's cheaper than our eBay price
        amazon_price = book.get('amazon_price')
        if amazon_price and amazon_price < listing_price:
            amazon_beating_price = round(amazon_price * 0.95, 2)
            log.info(f"  Amazon (${amazon_price:.2f}) < eBay (${listing_price:.2f}) — repricing to ${amazon_beating_price:.2f}")
            listing_price = amazon_beating_price

        profit = calculate_profit(book['cost'], listing_price)

        if profit < MIN_PROFIT:
            skipped_no_profit += 1
            continue

        opportunity = {
            **book,
            'median_price':   median,
            'listing_price':  listing_price,
            'profit':         profit,
            'confidence':     confidence,
            'num_comps':      num_comps,
            'scanned_at':     datetime.now().isoformat()
        }
        opportunities.append(opportunity)
        log.info(
            f"✅ {book['title'][:50]:<50} | "
            f"Cost: ${book['cost']:.2f} | "
            f"List: ${listing_price:.2f} | "
            f"Profit: ${profit:.2f} | "
            f"{confidence} ({num_comps})"
        )

    # Save for lister
    with open(OPPORTUNITIES_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'scan_date':     datetime.now().isoformat(),
            'total_books':   len(books),
            'opportunities': opportunities
        }, f, indent=2)

    log.info("=" * 60)
    log.info(f"SCAN COMPLETE: {len(opportunities)} opportunities found")
    log.info(f"  Already listed:   {skipped_listed}")
    log.info(f"  No eBay data:     {skipped_no_data}")
    log.info(f"  Unprofitable:     {skipped_no_profit}")
    log.info(f"  Amazon fallbacks: {amazon_fallbacks}")
    log.info("=" * 60)

    return opportunities


if __name__ == '__main__':
    scan()
