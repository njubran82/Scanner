"""
lister.py — eBay Auto-Lister
Reads scan_opportunities.json, lists each book on eBay via Inventory API,
updates lister_state.json so the tracker knows what to monitor.
"""

import os, json, base64, time, logging, requests, smtplib
from email.mime.text import MIMEText
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
SMTP_HOST          = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT          = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER          = os.getenv('SMTP_USER')
SMTP_PASSWORD      = os.getenv('SMTP_PASSWORD')
EMAIL_FROM         = os.getenv('EMAIL_FROM')
EMAIL_TO           = os.getenv('EMAIL_TO')

PAYMENT_POLICY_ID  = '391308491023'
SHIPPING_POLICY_ID = '391308514023'
RETURN_POLICY_ID   = '391308498023'

QUANTITY           = 20
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
    """Generate a professional listing description with mandatory disclaimer."""
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
def is_real_image(url, min_bytes=5000):
    """
    Download first chunk and verify it's a real JPEG/PNG large enough to use.
    min_bytes=15000 (~15KB) filters out thumbnails and placeholder images.
    """
    try:
        r = requests.get(url, timeout=12, stream=True)
        if r.status_code != 200:
            return False
        chunk = next(r.iter_content(chunk_size=min_bytes + 1), b'')
        r.close()
        is_jpg = chunk[:2] == b'\xff\xd8'
        is_png = chunk[:4] == b'\x89PNG'
        return (is_jpg or is_png) and len(chunk) >= min_bytes
    except Exception:
        return False

def get_book_image(isbn13, isbn10):
    """
    Try image sources in priority order until a valid image is found:
      1. Open Library     — free, high quality, good academic coverage
      2. Amazon CDN       — predictable URL pattern, excellent coverage
      3. Google Books     — API-based, good fallback
      4. Library of Congress — good for US-published academic titles
    Returns None only if all four sources fail.
    """
    import re as _re

    # ── 1. Open Library ───────────────────────────────────────────────────────
    for isbn in [isbn13, isbn10]:
        if not isbn:
            continue
        url = f'https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg'
        if is_real_image(url, min_bytes=5000):
            log.info(f"  Image: Open Library ({isbn})")
            return url

    # ── 2. Amazon CDN (no API key needed) ────────────────────────────────────
    # Amazon cover images are publicly accessible at predictable ISBN-keyed URLs.
    amazon_patterns = [
        f'https://images-na.ssl-images-amazon.com/images/P/{isbn13}.01.LZZZZZZZ.jpg',
        f'https://images-na.ssl-images-amazon.com/images/P/{isbn13}.01._SX500_.jpg',
    ]
    if isbn10:
        amazon_patterns += [
            f'https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.LZZZZZZZ.jpg',
            f'https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01._SX500_.jpg',
        ]
    for url in amazon_patterns:
        if is_real_image(url, min_bytes=8000):
            log.info(f"  Image: Amazon CDN")
            return url

    # ── 3. Google Books API ───────────────────────────────────────────────────
    try:
        r = requests.get(
            'https://www.googleapis.com/books/v1/volumes',
            params={'q': f'isbn:{isbn13}', 'maxResults': 3},
            timeout=8
        )
        items = r.json().get('items', [])
        for item in items:
            img = item.get('volumeInfo', {}).get('imageLinks', {})
            for size_key, zoom in [('extraLarge', 3), ('large', 3), ('medium', 2), ('thumbnail', 1)]:
                src = img.get(size_key)
                if not src:
                    continue
                src = src.replace('http://', 'https://')
                src = _re.sub(r'zoom=\d', f'zoom={zoom}', src)
                if is_real_image(src, min_bytes=5000):
                    log.info(f"  Image: Google Books ({size_key})")
                    return src
    except Exception as e:
        log.warning(f"  Google Books image failed: {e}")

    # ── 4. Library of Congress ────────────────────────────────────────────────
    try:
        r = requests.get(
            f'https://www.loc.gov/books/?q={isbn13}&fo=json',
            timeout=8
        )
        for result in r.json().get('results', [])[:3]:
            img_url = (result.get('image_url') or [None])[0]
            if img_url and is_real_image(img_url, min_bytes=5000):
                log.info(f"  Image: Library of Congress")
                return img_url
    except Exception as e:
        log.warning(f"  Library of Congress image failed: {e}")

    return None


# ── Item Specific Helpers ────────────────────────────────────────────────────
def extract_author(title):
    """
    Extract author name from BooksGoat title strings like:
    'The Art of Electronics, 3rd edition by Paul Horowitz and Winfield Hill'
    'DSM-5 Handbook by John Smith (ISBN ...) - Hardcover'
    Returns author string or 'See Description' as fallback.
    """
    import re
    # Match ' by Author Name' before common suffixes
    m = re.search(r'\bby\s+([A-Z][^(\-\n]{3,60}?)(?:\s*[-—(]|\s*\(ISBN|\s*$)', title)
    if m:
        author = m.group(1).strip().rstrip(',').strip()
        if 3 < len(author) < 80:
            return author
    return 'See Description'

def clean_book_title(title):
    """
    Strip author bylines, edition notes, ISBN refs, and format tags from title.
    Returns a clean title string for eBay's Book Title item specific.
    """
    import re
    t = re.sub(r'\s+by\s+.+$', '', title, flags=re.IGNORECASE)
    t = re.sub(r'\s*[-—]\s*(Hardcover|Paperback|Spiral.*?)$', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*\(ISBN[^)]*\)', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*\*US [A-Z]+\*\s*', ' ', t)
    t = re.sub(r'\s*\{[^}]*\}', '', t)
    return t.strip(' -—,')[:65]


# ── eBay Inventory API ───────────────────────────────────────────────────────
def create_inventory_item(token, book, description, image_url):
    """PUT /sell/inventory/v1/inventory_item/{sku}"""
    clean_title = clean_book_title(book['title'])
    author      = extract_author(book['title'])

    payload = {
        'availability': {
            'shipToLocationAvailability': {'quantity': QUANTITY}
        },
        'condition': 'NEW',
        'product': {
            'title':       book['title'][:80],
            'description': description,
            **({'imageUrls': [image_url]} if image_url else {}),
            'ean':         [book['isbn13']],
            'isbn':        [book['isbn10']] if book.get('isbn10') else [],
            'aspects': {
                'Book Title': [clean_title],
                'Author':     [author],
                'Type':       ['Textbook'],
                'Language':   ['English'],
                'ISBN':       [book['isbn13']]
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
        'categoryId':         '261186',
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
        'listingDuration':      'GTC',
        'merchantLocationKey':  'home1'
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
        # Handle "offer already exists" — update it with correct payload then publish
        resp_text = r.text
        if 'already exists' in resp_text:
            import re
            m = re.search(r'"offerId","value":"(\d+)"', resp_text)
            if m:
                offer_id = m.group(1)
                log.info(f"  Offer already exists ({offer_id}) — updating and publishing")
                # Update the existing offer with correct merchantLocationKey
                requests.put(
                    f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
                    headers={
                        'Authorization':    f'Bearer {token}',
                        'Content-Type':     'application/json',
                        'Content-Language': 'en-US'
                    },
                    json=offer_payload,
                    timeout=15
                )
            else:
                log.error(f"  create_offer failed ({r.status_code}): {resp_text[:300]}")
                return None, None
        else:
            log.error(f"  create_offer failed ({r.status_code}): {resp_text[:300]}")
            return None, None
    else:
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
        pub_text = pub.text
        # Error 25604: orphaned inventory item — product catalog entry is gone.
        # Fix: delete the inventory item and offer, recreate from scratch.
        if '25604' in pub_text:
            log.warning(f"  Error 25604 (orphaned item) for {book['isbn13']} — deleting and recreating")
            # Delete offer
            requests.delete(
                f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
                headers={'Authorization': f'Bearer {token}'}, timeout=10
            )
            # Delete inventory item
            requests.delete(
                f"https://api.ebay.com/sell/inventory/v1/inventory_item/{book['isbn13']}",
                headers={'Authorization': f'Bearer {token}'}, timeout=10
            )
            # Signal caller to retry by returning a special sentinel
            return 'RETRY', None
        log.error(f"  publish_offer failed ({pub.status_code}): {pub_text[:300]}")
        return offer_id, None

    listing_id = pub.json().get('listingId')
    return offer_id, listing_id


# ── Alerts ───────────────────────────────────────────────────────────────────
def send_alerts(listed_books, needs_image_drafts=None):
    lines = []

    if listed_books:
        lines.append(f"✅ {len(listed_books)} NEW LISTINGS")
        for b in listed_books:
            lines.append(f"  - {b['title'][:55]} | ${b['listing_price']:.2f} | Profit: ${b['profit']:.2f} | {b['confidence']}")

    if needs_image_drafts:
        lines.append(f"\n📋 {len(needs_image_drafts)} DRAFTS — IMAGE NEEDED")
        lines.append("These are saved in Seller Hub as drafts. Add a photo to each and publish manually.")
        lines.append("")
        for b in needs_image_drafts:
            lines.append(f"  - {b['title'][:55]}")
            lines.append(f"    ISBN: {b['isbn13']}  |  Price: ${b['listing_price']:.2f}  |  Offer ID: {b.get('offer_id', 'unknown')}")
            lines.append(f"    Seller Hub: https://www.ebay.com/sh/lst/active")

    if not lines:
        return

    body    = "\n".join(lines)
    subject = (
        f"[Lister] {len(listed_books)} listed"
        + (f", {len(needs_image_drafts)} drafts need images" if needs_image_drafts else "")
    )

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

    newly_listed       = []
    needs_image_drafts = []
    failed             = 0

    for book in opportunities:
        isbn13 = book['isbn13']

        if isbn13 in already_listed:
            log.info(f"Skip (already listed): {isbn13}")
            continue

        log.info(f"Listing: {book['title'][:60]}")

        # 1. Get image
        image_url = get_book_image(isbn13, book.get('isbn10', ''))
        if not image_url:
            log.warning(f"  No image found for {isbn13} — saving as draft for manual image upload")

        # 2. Generate description
        description = generate_description(book['title'], isbn13)

        # 3. Create inventory item
        if not create_inventory_item(token, book, description, image_url):
            failed += 1
            continue
        time.sleep(0.5)

        # 4a. No image — create offer but do NOT publish (saves as draft in Seller Hub)
        if not image_url:
            offer_payload = {
                'sku':                isbn13,
                'marketplaceId':      'EBAY_US',
                'format':             'FIXED_PRICE',
                'availableQuantity':  QUANTITY,
                'categoryId':         '261186',
                'listingDescription': description,
                'listingPolicies': {
                    'fulfillmentPolicyId': SHIPPING_POLICY_ID,
                    'paymentPolicyId':     PAYMENT_POLICY_ID,
                    'returnPolicyId':      RETURN_POLICY_ID
                },
                'pricingSummary': {
                    'price': {'currency': 'USD', 'value': f"{book['listing_price']:.2f}"}
                },
                'listingDuration':     'GTC',
                'merchantLocationKey': 'home1'
            }
            r = requests.post(
                'https://api.ebay.com/sell/inventory/v1/offer',
                headers={
                    'Authorization':    f'Bearer {token}',
                    'Content-Type':     'application/json',
                    'Content-Language': 'en-US'
                },
                json=offer_payload, timeout=15
            )
            offer_id = r.json().get('offerId') if r.status_code in [200, 201] else None

            # Offer already exists from a previous run — extract the existing offer_id
            # and update it with the current price, but still don't publish
            if not offer_id and 'already exists' in r.text:
                import re as _re
                m = _re.search(r'"offerId","value":"(\d+)"', r.text)
                if m:
                    offer_id = m.group(1)
                    log.info(f"  Draft offer already exists ({offer_id}) — updating price, keeping unpublished")
                    requests.put(
                        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
                        headers={
                            'Authorization':    f'Bearer {token}',
                            'Content-Type':     'application/json',
                            'Content-Language': 'en-US'
                        },
                        json=offer_payload, timeout=15
                    )

            if offer_id:
                log.info(f"  📋 Draft saved (offer {offer_id}) — add image in Seller Hub to publish")
                state.setdefault('listings', {})[isbn13] = {
                    'title':         book['title'],
                    'isbn13':        isbn13,
                    'isbn10':        book.get('isbn10', ''),
                    'offer_id':      offer_id,
                    'listing_id':    None,
                    'cost':          book['cost'],
                    'listing_price': book['listing_price'],
                    'profit':        book['profit'],
                    'confidence':    book['confidence'],
                    'listed_date':   datetime.now().isoformat(),
                    'booksgoat_url': book.get('booksgoat_url', ''),
                    'needs_image':   True,
                    'status':        'DRAFT',
                }
                if isbn13 not in state.get('listed_isbns', []):
                    state.setdefault('listed_isbns', []).append(isbn13)
                save_state(state)
                needs_image_drafts.append({**book, 'offer_id': offer_id})
            else:
                log.error(f"  Draft offer creation failed for {isbn13}: {r.text[:200]}")
                failed += 1
            continue

        # 4. Create + publish offer
        offer_id, listing_id = create_and_publish_offer(token, book, description)

        # If error 25604 was detected, inventory item was deleted — recreate and retry once
        if offer_id == 'RETRY':
            log.info(f"  Retrying {isbn13} after orphaned item cleanup...")
            if not create_inventory_item(token, book, description, image_url):
                log.error(f"  Retry: inventory item recreate failed for {isbn13}")
                failed += 1
                continue
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
            'booksgoat_url': book.get('booksgoat_url', f'https://www.booksgoat.com/index.php?route=product/search&search={isbn13}'),
            'needs_image':   not bool(image_url),
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
    log.info(f"LISTER DONE: {len(newly_listed)} listed, {len(needs_image_drafts)} drafts, {failed} failed")
    log.info("=" * 60)

    if newly_listed or needs_image_drafts:
        send_alerts(newly_listed, needs_image_drafts)


if __name__ == '__main__':
    list_books()
