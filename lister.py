"""
lister.py — eBay Auto-Lister
Reads scan_opportunities.json, lists each book on eBay via Inventory API,
updates lister_state.json so the tracker knows what to monitor.
"""

import os, json, base64, time, logging, requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
ANTHROPIC_API_KEY  = os.getenv('ANTHROPIC_API_KEY')
SENDGRID_API_KEY   = os.getenv('SENDGRID_API_KEY')
ALERT_EMAIL_TO     = os.getenv('ALERT_EMAIL_TO')
ALERT_EMAIL_FROM   = os.getenv('ALERT_EMAIL_FROM')
TWILIO_SID         = os.getenv('TWILIO_SID')
TWILIO_AUTH        = os.getenv('TWILIO_AUTH')
TWILIO_FROM        = os.getenv('TWILIO_FROM')
TWILIO_TO          = os.getenv('TWILIO_TO')

PAYMENT_POLICY_ID  = '391308491023'
SHIPPING_POLICY_ID = '391308514023'
RETURN_POLICY_ID   = '391308498023'

QUANTITY           = 100
STATE_FILE         = 'lister_state.json'
OPPORTUNITIES_FILE = 'scan_opportunities.json'
LOG_FILE           = 'lister_log.txt'

DISCLAIMER = (
    "\n\nThis item is sourced internationally to offer significant savings. "
    "Tracking information may not update until the package reaches the United States. "
    "All books are brand new, in mint condition, and carefully inspected before shipment."
)

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


# ── AI Description ───────────────────────────────────────────────────────────
def generate_description(title, isbn13):
    """Generate listing description via Claude API, append mandatory disclaimer."""
    try:
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json'
            },
            json={
                'model': 'claude-haiku-4-5-20251001',
                'max_tokens': 500,
                'messages': [{
                    'role': 'user',
                    'content': (
                        f'Write a 5-6 sentence product description for an eBay listing for the '
                        f'textbook titled "{title}" (ISBN: {isbn13}). Cover: what subject it addresses, '
                        f'who benefits from it, what key concepts or skills it develops, and why it is '
                        f'a valuable addition to a student\'s library. Write in plain text only — '
                        f'no bullet points, no markdown, no headers. Do not mention the price.'
                    )
                }]
            },
            timeout=20
        )
        text = r.json()['content'][0]['text'].strip()
        return text + DISCLAIMER
    except Exception as e:
        log.warning(f"AI description failed ({isbn13}): {e} — using fallback")
        return (
            f'"{title}" is a comprehensive academic textbook designed for students and educators '
            f'seeking a thorough understanding of the subject matter. This edition covers core '
            f'concepts, theories, and practical applications that are essential for academic success. '
            f'The text is written by leading authorities in the field and is widely used in university '
            f'and college courses worldwide. This brand new copy (ISBN: {isbn13}) is in perfect '
            f'condition and ready to support your studies.'
            + DISCLAIMER
        )


# ── Book Image ───────────────────────────────────────────────────────────────
def get_book_image(isbn13, isbn10):
    """Try Open Library (ISBN-13, ISBN-10), then Google Books API."""
    for isbn in [isbn13, isbn10]:
        if not isbn:
            continue
        url = f'https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg'
        try:
            r = requests.head(url, timeout=8, allow_redirects=True)
            # Open Library returns a 1x1 placeholder for missing covers
            content_len = int(r.headers.get('content-length', 0))
            if r.status_code == 200 and content_len > 5000:
                log.info(f"  Image: Open Library ({isbn})")
                return url
        except Exception:
            pass

    # Google Books fallback
    try:
        r = requests.get(
            f'https://www.googleapis.com/books/v1/volumes',
            params={'q': f'isbn:{isbn13}'},
            timeout=8
        )
        items = r.json().get('items', [])
        if items:
            img = items[0].get('volumeInfo', {}).get('imageLinks', {})
            src = img.get('extraLarge') or img.get('large') or img.get('thumbnail')
            if src:
                src = src.replace('http://', 'https://')
                log.info(f"  Image: Google Books")
                return src
    except Exception as e:
        log.warning(f"  Google Books image failed: {e}")

    return None


# ── eBay Inventory API ───────────────────────────────────────────────────────
def create_inventory_item(token, book, description, image_url):
    """PUT /sell/inventory/v1/inventory_item/{sku}"""
    payload = {
        'availability': {
            'shipToLocationAvailability': {'quantity': QUANTITY}
        },
        'condition': 'NEW',
        'product': {
            'title':       book['title'][:80],
            'description': description,
            'isbn':        book['isbn13'],
            'imageUrls':   [image_url],
            'aspects': {
                'Type':     ['Textbook'],
                'Language': ['English'],
                'ISBN':     [book['isbn13']]
            }
        }
    }

    r = requests.put(
        f"https://api.ebay.com/sell/inventory/v1/inventory_item/{book['isbn13']}",
        headers={
            'Authorization':    f'Bearer {token}',
            'Content-Type':     'application/json',
            'Content-Language': 'en-US'
        },
        json=payload,
        timeout=15
    )

    if r.status_code not in [200, 204]:
        log.error(f"  create_inventory_item failed ({r.status_code}): {r.text[:300]}")
        return False
    return True


def create_and_publish_offer(token, book, description):
    """
    POST /sell/inventory/v1/offer  →  POST .../publish
    Returns (offer_id, listing_id) or (None, None)
    """
    offer_payload = {
        'sku':                book['isbn13'],
        'marketplaceId':      'EBAY_US',
        'format':             'FIXED_PRICE',
        'availableQuantity':  QUANTITY,
        'categoryId':         '267',
        'listingDescription': description,
        'listingPolicies': {
            'fulfillmentPolicyId': SHIPPING_POLICY_ID,
            'paymentPolicyId':     PAYMENT_POLICY_ID,
            'returnPolicyId':      RETURN_POLICY_ID
        },
        'pricingSummary': {
            'price': {
                'currency': 'USD',
                'value':    f"{book['listing_price']:.2f}"
            }
        },
        'listingDuration': 'GTC',
        'countryOfOrigin': 'IN',
        'tax': {'applyTax': False}
    }

    r = requests.post(
        'https://api.ebay.com/sell/inventory/v1/offer',
        headers={
            'Authorization':    f'Bearer {token}',
            'Content-Type':     'application/json',
            'Content-Language': 'en-US'
        },
        json=offer_payload,
        timeout=15
    )

    if r.status_code not in [200, 201]:
        log.error(f"  create_offer failed ({r.status_code}): {r.text[:300]}")
        return None, None

    offer_id = r.json().get('offerId')
    if not offer_id:
        log.error(f"  No offerId returned: {r.text[:200]}")
        return None, None

    # Publish
    pub = requests.post(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json'
        },
        timeout=15
    )

    if pub.status_code not in [200, 201]:
        log.error(f"  publish_offer failed ({pub.status_code}): {pub.text[:300]}")
        return offer_id, None

    listing_id = pub.json().get('listingId')
    return offer_id, listing_id


# ── Alerts ───────────────────────────────────────────────────────────────────
def send_alerts(listed_books):
    subject = f"[Lister] {len(listed_books)} new books listed on eBay"
    lines = [
        f"- {b['title']} | ${b['listing_price']:.2f} | Profit: ${b['profit']:.2f} | {b['confidence']}"
        for b in listed_books
    ]
    body = f"New listings added:\n\n" + "\n".join(lines)

    # Email
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

    # SMS
    if TWILIO_SID and TWILIO_AUTH:
        try:
            requests.post(
                f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json',
                auth=(TWILIO_SID, TWILIO_AUTH),
                data={
                    'From': TWILIO_FROM,
                    'To':   TWILIO_TO,
                    'Body': f"[eBay Lister] {len(listed_books)} new books listed. Check email for details."
                },
                timeout=10
            )
            log.info("SMS alert sent")
        except Exception as e:
            log.error(f"SMS alert failed: {e}")


# ── State ────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {'listings': {}, 'listed_isbns': []}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


# ── Main ─────────────────────────────────────────────────────────────────────
def list_books():
    log.info("=" * 60)
    log.info("LISTER STARTED — " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    log.info("=" * 60)

    if not os.path.exists(OPPORTUNITIES_FILE):
        log.warning("scan_opportunities.json not found. Run scanner.py first.")
        return

    with open(OPPORTUNITIES_FILE, encoding='utf-8') as f:
        data = json.load(f)

    opportunities = data.get('opportunities', [])
    if not opportunities:
        log.info("No opportunities to list.")
        return

    log.info(f"Processing {len(opportunities)} opportunities...")

    token         = get_ebay_token()
    state         = load_state()
    already_listed = set(state.get('listed_isbns', []))

    newly_listed = []
    failed       = 0

    for book in opportunities:
        isbn13 = book['isbn13']

        if isbn13 in already_listed:
            log.info(f"Skip (already listed): {isbn13}")
            continue

        log.info(f"Listing: {book['title'][:60]}")

        # 1. Get image — skip if none
        image_url = get_book_image(isbn13, book.get('isbn10', ''))
        if not image_url:
            log.warning(f"  No image found for {isbn13} — skipping")
            failed += 1
            continue

        # 2. Generate description
        description = generate_description(book['title'], isbn13)

        # 3. Create inventory item
        if not create_inventory_item(token, book, description, image_url):
            failed += 1
            continue
        time.sleep(0.5)

        # 4. Create + publish offer
        offer_id, listing_id = create_and_publish_offer(token, book, description)
        if not listing_id:
            log.error(f"  Listing failed for {isbn13}")
            failed += 1
            continue

        # 5. Update state
        state.setdefault('listings', {})[isbn13] = {
            'title':         book['title'],
            'isbn13':        isbn13,
            'isbn10':        book.get('isbn10', ''),
            'offer_id':      offer_id,
            'listing_id':    listing_id,
            'cost':          book['cost'],
            'listing_price': book['listing_price'],
            'profit':        book['profit'],
            'confidence':    book['confidence'],
            'listed_date':   datetime.now().isoformat(),
        }
        if isbn13 not in state.get('listed_isbns', []):
            state.setdefault('listed_isbns', []).append(isbn13)

        save_state(state)
        newly_listed.append(book)

        log.info(
            f"  ✅ Listed | ${book['listing_price']:.2f} | "
            f"Profit: ${book['profit']:.2f} | ListingID: {listing_id}"
        )
        time.sleep(1.0)

    state['last_run'] = datetime.now().isoformat()
    save_state(state)

    log.info("=" * 60)
    log.info(f"LISTER DONE: {len(newly_listed)} listed, {failed} failed")
    log.info("=" * 60)

    if newly_listed:
        send_alerts(newly_listed)


if __name__ == '__main__':
    list_books()
