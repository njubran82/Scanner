#!/usr/bin/env python3
"""
shipping_tracker.py — Automated BooksGoat shipping email → eBay tracking update.

- Polls Gmail via IMAP for BooksGoat shipping emails
- Parses order ID, tracking number, carrier, ISBN
- Finds matching eBay order via Orders API
- POSTs shipment tracking to eBay Fulfillment API
- Retries on subsequent runs if tracking not yet available
- Tracks state in shipping_state.json to avoid duplicates
- Runs every 2 hours via GitHub Actions
"""

import os, json, imaplib, email, re, base64, logging, time, requests
from pathlib import Path
from datetime import datetime, timezone
from email.header import decode_header

# ── Config ────────────────────────────────────────────────────────────────────
EBAY_APP_ID        = os.environ['EBAY_APP_ID']
EBAY_CERT_ID       = os.environ['EBAY_CERT_ID']
EBAY_REFRESH_TOKEN = os.environ['EBAY_REFRESH_TOKEN']
SMTP_USER          = os.environ['SMTP_USER']          # jubran.industries@gmail.com
SMTP_PASSWORD      = os.environ['SMTP_PASSWORD']      # Gmail App Password

IMAP_HOST          = 'imap.gmail.com'
IMAP_PORT          = 993
BOOKSGOAT_SENDER   = 'booksgoat.com'                  # matches any @booksgoat.com address
BOOKSGOAT_SUBJECT  = 'ORDER HAS BEEN SHIPPED'

STATE_FILE         = Path('shipping_state.json')
LOG_FILE           = 'shipping_tracker.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── State management ──────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}   # {booksgoat_order_id: {status, ebay_order_id, tracking, attempts, last_attempt}}


def save_state(state: dict):
    tmp = str(STATE_FILE) + '.tmp'
    Path(tmp).write_text(json.dumps(state, indent=2))
    Path(tmp).replace(STATE_FILE)


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
            'scope': ' '.join([
                'https://api.ebay.com/oauth/api_scope/sell.fulfillment',
                'https://api.ebay.com/oauth/api_scope/sell.inventory',
            ])
        }
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Token error: {data}')
    return data['access_token']


# ── Gmail IMAP ────────────────────────────────────────────────────────────────
def get_shipping_emails() -> list[dict]:
    """Connect to Gmail, find BooksGoat shipping emails from last 7 days."""
    log.info('Connecting to Gmail IMAP...')
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(SMTP_USER, SMTP_PASSWORD)
    mail.select('inbox')

    # Search for BooksGoat shipping emails
    _, msg_ids = mail.search(None, 'UNSEEN', f'SUBJECT "{BOOKSGOAT_SUBJECT}"')
    
    # Also check SEEN in case we missed some — search last 7 days
    _, msg_ids_all = mail.search(None, 'SINCE', 
        (datetime.now() - __import__('datetime').timedelta(days=7)).strftime('%d-%b-%Y'),
        f'SUBJECT "{BOOKSGOAT_SUBJECT}"'
    )

    all_ids = set(msg_ids[0].split() + msg_ids_all[0].split())
    log.info(f'Found {len(all_ids)} BooksGoat shipping emails')

    results = []
    for msg_id in all_ids:
        _, msg_data = mail.fetch(msg_id, '(RFC822)')
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        
        # Verify sender
        sender = msg.get('From', '')
        if BOOKSGOAT_SENDER not in sender.lower():
            continue

        body = extract_body(msg)
        parsed = parse_shipping_email(body)
        if parsed:
            results.append(parsed)
            log.info(f"  Parsed: Order #{parsed['order_id']} | "
                     f"ISBN {parsed['isbn']} | "
                     f"Tracking: {parsed['tracking'] or 'NONE'}")

    mail.logout()
    return results


def extract_body(msg) -> str:
    """Extract plain text body from email."""
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/plain':
                try:
                    body += part.get_payload(decode=True).decode('utf-8', errors='ignore')
                except Exception:
                    pass
            elif ct == 'text/html' and not body:
                try:
                    raw = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    # Strip HTML tags for basic text
                    body += re.sub(r'<[^>]+>', ' ', raw)
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
        except Exception:
            pass
    return body


def parse_shipping_email(body: str) -> dict | None:
    """Extract key fields from BooksGoat shipping email body."""
    result = {}

    # Order ID — matches "Order ID: #26918" or "order # 26918"
    m = re.search(r'[Oo]rder\s*(?:ID|#)[:\s#]*(\d+)', body)
    if not m:
        log.warning('Could not extract order ID from email')
        return None
    result['order_id'] = m.group(1)

    # Tracking number
    m = re.search(r'[Tt]racking\s*[Nn]umber[:\s]*([A-Za-z0-9]{8,30})', body)
    result['tracking'] = m.group(1) if m else None

    # Carrier
    result['carrier'] = 'FEDEX'  # default
    if re.search(r'fedex', body, re.IGNORECASE):
        result['carrier'] = 'FEDEX'
    elif re.search(r'ups', body, re.IGNORECASE):
        result['carrier'] = 'UPS'
    elif re.search(r'usps', body, re.IGNORECASE):
        result['carrier'] = 'USPS'

    # ISBN — 13 digits
    m = re.search(r'ISBN[:\s]*(97[89]\d{10})', body)
    result['isbn'] = m.group(1) if m else None

    # Buyer name (first line of shipping address block)
    m = re.search(r'Shipping Address:\s*([A-Z][a-zA-Z]+\s+[A-Z][a-zA-Z]+)', body)
    result['buyer_name'] = m.group(1) if m else None

    return result


# ── eBay Orders API ───────────────────────────────────────────────────────────
def find_ebay_order(isbn: str, buyer_name: str, token: str) -> dict | None:
    """Find matching eBay order by ISBN and/or buyer name."""
    headers = {'Authorization': f'Bearer {token}'}
    
    # Get recent orders (last 30 days, awaiting shipment or shipped)
    r = requests.get(
        'https://api.ebay.com/sell/fulfillment/v1/order',
        headers=headers,
        params={
            'filter': 'orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}',
            'limit': 200,
        },
        timeout=15
    )
    
    if r.status_code != 200:
        log.error(f'Orders API error: {r.status_code} {r.text[:200]}')
        return None

    orders = r.json().get('orders', [])
    log.info(f'Fetched {len(orders)} open eBay orders')

    for order in orders:
        for item in order.get('lineItems', []):
            # Match by ISBN in title or properties
            title = item.get('title', '')
            props = item.get('properties', [])
            
            isbn_match = isbn and isbn in title
            if not isbn_match:
                for prop in props:
                    if prop.get('name') == 'ISBN' and prop.get('value') == isbn:
                        isbn_match = True
                        break

            # Match by buyer name as fallback
            buyer = order.get('buyer', {})
            ebay_buyer = (buyer.get('username', '') + ' ' + 
                         order.get('fulfillmentStartInstructions', [{}])[0]
                         .get('shippingStep', {})
                         .get('shipTo', {})
                         .get('fullName', '')).lower()
            name_match = buyer_name and buyer_name.lower() in ebay_buyer

            if isbn_match or name_match:
                log.info(f"  Matched eBay order {order['orderId']} "
                         f"(isbn_match={isbn_match}, name_match={name_match})")
                return order

    log.warning(f'No matching eBay order found for ISBN={isbn}, buyer={buyer_name}')
    return None


def post_tracking(ebay_order_id: str, tracking: str, carrier: str, token: str) -> bool:
    """POST shipment tracking to eBay Fulfillment API."""
    
    # Map carrier to eBay enum
    carrier_map = {
        'FEDEX': 'FedEx',
        'UPS':   'UPS',
        'USPS':  'USPS',
    }
    ebay_carrier = carrier_map.get(carrier, 'FedEx')

    payload = {
        'trackingNumber': tracking,
        'shippingCarrierCode': ebay_carrier,
        'shippedDate': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        'lineItems': []  # empty = applies to all items in order
    }

    r = requests.post(
        f'https://api.ebay.com/sell/fulfillment/v1/order/{ebay_order_id}/shipping_fulfillment',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json'},
        json=payload,
        timeout=15
    )

    if r.status_code in (200, 201):
        log.info(f'  ✅ Tracking {tracking} posted to eBay order {ebay_order_id}')
        return True
    else:
        log.error(f'  ❌ Failed to post tracking: {r.status_code} {r.text[:300]}')
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

    posted = skipped = retried = failed = 0

    for parsed in emails:
        order_id = parsed['order_id']
        tracking = parsed['tracking']
        carrier  = parsed['carrier']
        isbn     = parsed['isbn']
        buyer    = parsed['buyer_name']

        existing = state.get(order_id, {})

        # Already successfully posted
        if existing.get('status') == 'posted':
            log.info(f'Order #{order_id}: already posted — skipping')
            skipped += 1
            continue

        # No tracking yet — record and wait for next run
        if not tracking:
            log.info(f'Order #{order_id}: no tracking number yet — will retry next run')
            state[order_id] = {
                'status': 'pending_tracking',
                'isbn': isbn,
                'buyer': buyer,
                'attempts': existing.get('attempts', 0) + 1,
                'last_attempt': datetime.now().isoformat()
            }
            retried += 1
            save_state(state)
            continue

        # Find matching eBay order
        ebay_order = find_ebay_order(isbn, buyer, token)
        if not ebay_order:
            log.warning(f'Order #{order_id}: no matching eBay order found')
            state[order_id] = {
                'status': 'no_match',
                'isbn': isbn,
                'tracking': tracking,
                'attempts': existing.get('attempts', 0) + 1,
                'last_attempt': datetime.now().isoformat()
            }
            failed += 1
            save_state(state)
            continue

        ebay_order_id = ebay_order['orderId']

        # Post tracking
        ok = post_tracking(ebay_order_id, tracking, carrier, token)
        state[order_id] = {
            'status': 'posted' if ok else 'failed',
            'ebay_order_id': ebay_order_id,
            'isbn': isbn,
            'tracking': tracking,
            'carrier': carrier,
            'attempts': existing.get('attempts', 0) + 1,
            'last_attempt': datetime.now().isoformat()
        }
        if ok:
            posted += 1
        else:
            failed += 1
        save_state(state)
        time.sleep(0.5)

    log.info('=' * 60)
    log.info(f'DONE: {posted} posted | {retried} awaiting tracking | '
             f'{skipped} already done | {failed} failed')
    log.info('=' * 60)


if __name__ == '__main__':
    run()
