#!/usr/bin/env python3
"""
shipping_tracker.py — Automated BooksGoat shipping email → eBay tracking update.

Two-pass flow:
  Pass 1: BooksGoat confirms shipment → mark eBay order as shipped (no tracking yet)
  Pass 2: Tracking number appears in email → update eBay order with tracking

- Polls Gmail via IMAP for BooksGoat shipping emails
- Parses order ID, tracking number, carrier, ISBN, buyer name
- Finds matching eBay order via Orders API
- POSTs shipment tracking to eBay Fulfillment API
- Retries on subsequent runs if tracking not yet available
- Tracks state in shipping_state.json to avoid duplicates
- Runs every 2 hours via GitHub Actions
"""

import os, json, imaplib, email, re, base64, logging, time, requests, unicodedata
from email.header import decode_header as _dh
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────
EBAY_APP_ID        = os.environ['EBAY_APP_ID']
EBAY_CERT_ID       = os.environ['EBAY_CERT_ID']
EBAY_REFRESH_TOKEN = os.environ['EBAY_REFRESH_TOKEN']
SMTP_USER          = os.environ['SMTP_USER']
SMTP_PASSWORD      = os.environ['SMTP_PASSWORD']

IMAP_HOST          = 'imap.gmail.com'
IMAP_PORT          = 993
LOOKBACK_DAYS      = 14

STATE_FILE         = Path('shipping_state.json')
LOG_FILE           = 'shipping_tracker.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict):
    tmp = str(STATE_FILE) + '.tmp'
    Path(tmp).write_text(json.dumps(state, indent=2))
    Path(tmp).replace(STATE_FILE)


# ── Email helpers ─────────────────────────────────────────────────────────────
def decode_header(val: str) -> str:
    parts = _dh(val or '')
    result = ''
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or 'utf-8', errors='ignore')
        else:
            result += part
    return result

def extract_raw_and_text(msg) -> tuple[str, str]:
    raw = b''
    text = ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_payload(decode=True) or b''
            raw += payload
            if ct == 'text/plain':
                text += payload.decode('utf-8', errors='ignore')
            elif ct == 'text/html':
                html = payload.decode('utf-8', errors='ignore')
                text += re.sub(r'<[^>]+>', ' ', html)
    else:
        payload = msg.get_payload(decode=True) or b''
        raw = payload
        text = payload.decode('utf-8', errors='ignore')
    return raw.decode('utf-8', errors='ignore'), text

def _extract_isbn(text: str) -> str | None:
    """
    Extract ISBN-13 from email text. Handles:
      - Bare: 9781492592006
      - Hyphenated: 978-1-4925-9200-6
      - Spaced: 978 1 4925 9200 6
      - With label: ISBN: 978-1492592006, ISBN-13: 9781492592006
    """
    # Try labeled ISBN first (most reliable)
    m = re.search(r'ISBN[\-\s]*(?:13)?[:\s]*(97[89][\d\-\s]{10,17})', text, re.IGNORECASE)
    if m:
        digits = re.sub(r'[^\d]', '', m.group(1))
        if len(digits) == 13 and digits[:3] in ('978', '979'):
            return digits

    # Fallback: any 13-digit sequence starting with 978/979 (with optional hyphens/spaces)
    m = re.search(r'(97[89][\d\-\s]{10,17})', text)
    if m:
        digits = re.sub(r'[^\d]', '', m.group(1))
        if len(digits) == 13:
            return digits

    # Last resort: bare 13-digit number anywhere
    m = re.search(r'\b(97[89]\d{10})\b', text)
    if m:
        return m.group(1)

    return None

def _extract_buyer_name(text: str) -> str | None:
    """
    Extract buyer name from BooksGoat shipping email. Handles:
      - "Dear Csaba Milian," (most reliable — greeting line)
      - "Shipping Address: Csaba\nMilian\n123 Main St" (name split across lines)
      - "Ship to: Mary Jane O'Brien"
      - Multi-word, hyphenated, apostrophe names
    """
    # Best source: "Dear <name>," greeting
    m = re.search(r'Dear\s+([A-Z][A-Za-z\'\-]+(?:\s+[A-Z][A-Za-z\'\-]+){1,4})\s*,', text)
    if m:
        return m.group(1).strip()

    # Shipping address — handle name split across lines
    m = re.search(
        r'[Ss]hipping\s+[Aa]ddress[:\s]+'
        r'([A-Z][A-Za-z\'\-]+(?:[\s\n]+[A-Z][A-Za-z\'\-]+){1,4})',
        text
    )
    if m:
        name = m.group(1).strip()
        # Stop at address lines (digits, PO Box, etc.)
        name = re.split(r'[\s\n]+(?:\d|PO\b|P\.O\.|Box\b|Suite\b|Apt\b|Unit\b)', name)[0].strip()
        # Normalize internal newlines to spaces
        name = re.sub(r'\s*\n\s*', ' ', name)
        if len(name) >= 3:
            return name

    # Other label patterns
    for pattern in [
        r'[Ss]hip\s*[Tt]o[:\s]+([A-Z][A-Za-z\'\-]+(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
        r'[Dd]eliver\s*[Tt]o[:\s]+([A-Z][A-Za-z\'\-]+(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
        r'[Rr]ecipient[:\s]+([A-Z][A-Za-z\'\-]+(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
    ]:
        m = re.search(pattern, text)
        if m:
            name = m.group(1).strip()
            name = re.split(r'\s+(?:\d|PO\b|P\.O\.|Box\b|Suite\b|Apt\b|Unit\b)', name)[0].strip()
            if len(name) >= 3:
                return name

    return None

def parse_shipping_email(text: str) -> dict:
    result = {}

    # Order ID
    m = re.search(r'[Oo]rder\s*(?:ID|#|No\.?)[:\s#]*(\d+)', text)
    result['order_id'] = m.group(1) if m else None

    # Tracking number
    m = re.search(r'[Tt]racking\s*[Nn]umber[:\s]*([A-Za-z0-9]{8,30})', text)
    result['tracking'] = m.group(1) if m else None

    # Carrier
    result['carrier'] = 'FEDEX'
    if re.search(r'\bups\b', text, re.IGNORECASE): result['carrier'] = 'UPS'
    elif re.search(r'\busps\b', text, re.IGNORECASE): result['carrier'] = 'USPS'

    # ISBN (robust extraction)
    result['isbn'] = _extract_isbn(text)

    # Buyer name (robust extraction)
    result['buyer_name'] = _extract_buyer_name(text)

    return result

def get_shipping_emails() -> list[dict]:
    log.info('Connecting to Gmail IMAP...')
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(SMTP_USER, SMTP_PASSWORD)
    mail.select('inbox')

    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime('%d-%b-%Y')
    _, msg_ids = mail.search(None, 'SINCE', since, 'FROM', 'booksgoat', 'SUBJECT', '"Order Update"')
    ids = msg_ids[0].split()
    log.info(f'Found {len(ids)} BooksGoat Order Update emails in last {LOOKBACK_DAYS} days')

    results = []
    for msg_id in ids:
        _, msg_data = mail.fetch(msg_id, '(RFC822)')
        msg = email.message_from_bytes(msg_data[0][1])
        raw, text = extract_raw_and_text(msg)

        if 'SHIPPED' not in raw.upper() and 'SHIPPED' not in text.upper():
            continue

        parsed = parse_shipping_email(text)
        if parsed.get('order_id'):
            results.append(parsed)

    mail.logout()
    log.info(f'{len(results)} shipping emails parsed')
    return results


# ── eBay OAuth ─────────────────────────────────────────────────────────────────
def get_ebay_token() -> str:
    creds = base64.b64encode(f'{EBAY_APP_ID}:{EBAY_CERT_ID}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type': 'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': 'https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.fulfillment'
        }
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Token error: {data}')
    return data['access_token']


# ── eBay Orders API ───────────────────────────────────────────────────────────
def find_ebay_order(isbn: str, buyer_name: str, token: str) -> dict | None:
    """
    Find matching eBay order by ISBN or buyer name.
    
    Search strategy (in order):
      1. SKU == ISBN (auto-listed books)
      2. ISBN in line item title
      3. ISBN in line item properties
      4. ISBN in legacyVariationId or customLabel
      5. Buyer name match (fallback)
    
    Fetches unfulfilled orders first (most relevant), then falls back
    to all recent orders. Handles pagination for stores with 200+ orders.
    """
    headers = {'Authorization': f'Bearer {token}'}

    # Try unfulfilled orders first (smaller set, most likely to need action)
    # Then fall back to all orders if no match found
    filter_sets = [
        'orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}',
        None,  # no filter = all recent orders
    ]

    for order_filter in filter_sets:
        offset = 0
        while True:
            params = {'limit': 200, 'offset': offset}
            if order_filter:
                params['filter'] = order_filter

            try:
                r = requests.get(
                    'https://api.ebay.com/sell/fulfillment/v1/order',
                    headers=headers,
                    params=params,
                    timeout=15
                )
            except Exception as e:
                log.error(f'Orders API request failed: {e}')
                return None

            if r.status_code != 200:
                log.error(f'Orders API error: {r.status_code} {r.text[:200]}')
                return None

            data = r.json()
            orders = data.get('orders', [])
            if not orders:
                break  # no more pages

            for order in orders:
                for item in order.get('lineItems', []):
                    title       = item.get('title', '')
                    sku         = item.get('sku', '')
                    custom_label = item.get('legacyVariationId', '')

                    # ISBN matching — multiple fallbacks
                    isbn_match = False
                    if isbn:
                        if sku == isbn:
                            isbn_match = True
                        elif isbn in title:
                            isbn_match = True
                        elif custom_label == isbn:
                            isbn_match = True
                        else:
                            for prop in item.get('properties', []):
                                if isinstance(prop, dict) and prop.get('name') == 'ISBN' and prop.get('value') == isbn:
                                    isbn_match = True
                                    break

                    # Buyer name matching — normalize accents and strip C/O prefixes
                    ship_to = (order.get('fulfillmentStartInstructions') or [{}])[0] \
                        .get('shippingStep', {}).get('shipTo', {})
                    ebay_name_raw = ship_to.get('fullName', '')
                    # Strip eIS/GSP C/O prefixes from eBay International Shipping
                    ebay_name_clean = re.sub(r'^(?:eIS|GSP)\s+C/O\s+', '', ebay_name_raw, flags=re.IGNORECASE)
                    # Normalize accents: Milián → Milian
                    ebay_name_norm = unicodedata.normalize('NFKD', ebay_name_clean.lower()) \
                        .encode('ascii', 'ignore').decode('ascii')
                    name_match = False
                    if buyer_name:
                        bn = unicodedata.normalize('NFKD', buyer_name.lower()) \
                            .encode('ascii', 'ignore').decode('ascii')
                        name_match = bn in ebay_name_norm or ebay_name_norm in bn

                    if isbn_match or name_match:
                        log.info(f"  Matched eBay order {order['orderId']} "
                                 f"(isbn={isbn_match}, name={name_match})")
                        return order

            # Check for more pages
            total = data.get('total', 0)
            offset += len(orders)
            if offset >= total:
                break  # no more pages

    log.warning(f'No eBay order match for ISBN={isbn}, buyer={buyer_name}')
    return None

def post_shipped(ebay_order_id: str, token: str, order: dict,
                 tracking: str = None, carrier: str = None) -> bool:
    carrier_map = {'FEDEX': 'FedEx', 'UPS': 'UPS', 'USPS': 'USPS'}
    line_items = [
        {'lineItemId': item['lineItemId']}
        for item in order.get('lineItems', [])
        if 'lineItemId' in item
    ]
    payload = {
        'shippedDate': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        'lineItems': line_items
    }
    # Only add tracking if provided
    if tracking:
        payload['trackingNumber'] = tracking
        payload['shippingCarrierCode'] = carrier_map.get(carrier, 'FedEx')

    r = requests.post(
        f'https://api.ebay.com/sell/fulfillment/v1/order/{ebay_order_id}/shipping_fulfillment',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json'},
        json=payload,
        timeout=15
    )
    if r.status_code in (200, 201):
        if tracking:
            log.info(f'  ✅ Marked shipped + tracking {tracking} → eBay order {ebay_order_id}')
        else:
            log.info(f'  ✅ Marked shipped (no tracking) → eBay order {ebay_order_id}')
        return True
    else:
        log.error(f'  ❌ Failed: {r.status_code} {r.text[:300]}')
        return False


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    log.info('=' * 60)
    log.info('shipping_tracker.py started')

    state = load_state()
    emails = get_shipping_emails()

    if not emails:
        log.info('No shipping emails to process')
        log.info('=' * 60)
        return

    token = get_ebay_token()
    log.info('eBay token acquired')

    posted = shipped_no_tracking = skipped = pending = failed = 0

    # Deduplicate: for each order_id, use the latest email (last occurrence = most recent tracking)
    by_order = {}
    for e in emails:
        by_order[e['order_id']] = e  # later entries overwrite earlier ones

    for order_id, parsed in by_order.items():
        tracking   = parsed['tracking']
        carrier    = parsed['carrier']
        isbn       = parsed['isbn']
        buyer      = parsed['buyer_name']
        existing   = state.get(order_id, {})

        # ── FULLY DONE: shipped + tracking already posted ─────────────
        if existing.get('status') == 'posted':
            log.info(f'Order #{order_id}: fully complete — skipping')
            skipped += 1
            continue

        # ── PASS 2: already marked shipped, now tracking is available ─
        if existing.get('status') == 'shipped_no_tracking':
            if not tracking:
                log.info(f'Order #{order_id}: shipped, still no tracking — waiting')
                pending += 1
                continue

            # Tracking now available — post update to eBay
            ebay_order_id = existing.get('ebay_order_id')
            if ebay_order_id:
                # Re-fetch order object for line items
                ebay_order = find_ebay_order(isbn, buyer, token)
                if ebay_order:
                    ok = post_shipped(ebay_order_id, token, ebay_order,
                                      tracking=tracking, carrier=carrier)
                    if ok:
                        state[order_id]['status'] = 'posted'
                        state[order_id]['tracking'] = tracking
                        state[order_id]['carrier'] = carrier
                        state[order_id]['tracking_posted_at'] = datetime.now().isoformat()
                        posted += 1
                    else:
                        log.warning(f'Order #{order_id}: tracking update failed — will retry')
                        pending += 1
                else:
                    log.warning(f'Order #{order_id}: could not re-match eBay order for tracking update')
                    pending += 1
            else:
                log.warning(f'Order #{order_id}: no stored ebay_order_id — cannot add tracking')
                pending += 1
            save_state(state)
            time.sleep(0.5)
            continue

        # ── PASS 1: new order — find eBay match and mark shipped ──────
        ebay_order = find_ebay_order(isbn, buyer, token)
        if not ebay_order:
            state[order_id] = {
                'status': 'no_match',
                'isbn': isbn, 'tracking': tracking, 'carrier': carrier,
                'buyer': buyer,
                'attempts': existing.get('attempts', 0) + 1,
                'last_attempt': datetime.now().isoformat()
            }
            failed += 1
            save_state(state)
            continue

        ebay_order_id = ebay_order['orderId']

        if tracking:
            # Have tracking already — post shipped + tracking in one call (skip pass 2)
            ok = post_shipped(ebay_order_id, token, ebay_order,
                              tracking=tracking, carrier=carrier)
            if ok:
                state[order_id] = {
                    'status': 'posted',
                    'ebay_order_id': ebay_order_id,
                    'isbn': isbn, 'tracking': tracking, 'carrier': carrier,
                    'buyer': buyer,
                    'shipped_at': datetime.now().isoformat(),
                    'tracking_posted_at': datetime.now().isoformat()
                }
                posted += 1
            else:
                state[order_id] = {
                    'status': 'failed',
                    'ebay_order_id': ebay_order_id,
                    'isbn': isbn, 'tracking': tracking, 'carrier': carrier,
                    'buyer': buyer,
                    'attempts': existing.get('attempts', 0) + 1,
                    'last_attempt': datetime.now().isoformat()
                }
                failed += 1
        else:
            # No tracking yet — mark shipped anyway (pass 1), tracking comes later
            ok = post_shipped(ebay_order_id, token, ebay_order)
            if ok:
                state[order_id] = {
                    'status': 'shipped_no_tracking',
                    'ebay_order_id': ebay_order_id,
                    'isbn': isbn, 'buyer': buyer,
                    'shipped_at': datetime.now().isoformat()
                }
                shipped_no_tracking += 1
            else:
                state[order_id] = {
                    'status': 'failed',
                    'ebay_order_id': ebay_order_id,
                    'isbn': isbn, 'buyer': buyer,
                    'attempts': existing.get('attempts', 0) + 1,
                    'last_attempt': datetime.now().isoformat()
                }
                failed += 1

        save_state(state)
        time.sleep(0.5)

    log.info('=' * 60)
    log.info(f'DONE: {posted} posted w/tracking | {shipped_no_tracking} marked shipped (no tracking) | '
             f'{pending} awaiting tracking | {skipped} already done | {failed} failed/no match')
    log.info('=' * 60)

if __name__ == '__main__':
    run()
