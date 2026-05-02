#!/usr/bin/env python3
"""
shipping_tracker.py - Automated BooksGoat shipping email -> eBay tracking update.

Flow:
  Pass 1: BooksGoat confirms shipment -> save state only (no eBay call)
  Pass 2: Tracking number appears in email -> create fulfillment with tracking on eBay

Matching safety (v3 - strict matching):
  - Prefer orders matching BOTH isbn AND buyer name
  - Fall back to isbn-only if buyer name unavailable
  - NEVER match on buyer name alone
  - Pass 2 fetches order by stored ID instead of re-searching

- Polls Gmail via IMAP for BooksGoat shipping emails
- Parses order ID, tracking number, carrier, ISBN, buyer name
- Finds matching eBay order via Orders API
- POSTs shipment tracking to eBay Fulfillment API
- Retries on subsequent runs if tracking not yet available
- Tracks state in shipping_state.json to avoid duplicates
- Runs every 2 hours via GitHub Actions

v3.0 - Strict matching after #27048 wrong-tracking incident
"""

import os, json, imaplib, email, re, base64, logging, time, requests, unicodedata
from email.header import decode_header as _dh
from pathlib import Path
from datetime import datetime, timedelta, timezone

try:
    from email_helpers import build_shipping_tracker_email, send_html_email
except ImportError:
    build_shipping_tracker_email = None
    send_html_email = None

# -- Config -------------------------------------------------------------------
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

TRACKING_ENABLED   = True

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# -- State --------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict):
    tmp = str(STATE_FILE) + '.tmp'
    Path(tmp).write_text(json.dumps(state, indent=2))
    Path(tmp).replace(STATE_FILE)


# -- Email helpers ------------------------------------------------------------
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
    m = re.search(r'ISBN[\-\s]*(?:13)?[:\s]*(97[89][\d\-\s]{10,17})', text, re.IGNORECASE)
    if m:
        digits = re.sub(r'[^\d]', '', m.group(1))
        if len(digits) == 13 and digits[:3] in ('978', '979'):
            return digits
    m = re.search(r'(97[89][\d\-\s]{10,17})', text)
    if m:
        digits = re.sub(r'[^\d]', '', m.group(1))
        if len(digits) == 13:
            return digits
    m = re.search(r'\b(97[89]\d{10})\b', text)
    if m:
        return m.group(1)
    return None

def _extract_buyer_name(text: str) -> str | None:
    m = re.search(r'Dear\s+([A-Z][A-Za-z\'\-]*(?:\s+[A-Z][A-Za-z\'\-]+){1,4})\s*,', text)
    if m:
        return m.group(1).strip()
    m = re.search(
        r'[Ss]hipping\s+[Aa]ddress[:\s]+'
        r'([A-Z][A-Za-z\'\-]*(?:[\s\n]+[A-Z][A-Za-z\'\-]+){1,4})',
        text
    )
    if m:
        name = m.group(1).strip()
        name = re.split(r'[\s\n]+(?:\d|PO\b|P\.O\.|Box\b|Suite\b|Apt\b|Unit\b)', name)[0].strip()
        name = re.sub(r'\s*\n\s*', ' ', name)
        if len(name) >= 3:
            return name
    for pattern in [
        r'[Ss]hip\s*[Tt]o[:\s]+([A-Z][A-Za-z\'\-]*(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
        r'[Dd]eliver\s*[Tt]o[:\s]+([A-Z][A-Za-z\'\-]*(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
        r'[Rr]ecipient[:\s]+([A-Z][A-Za-z\'\-]*(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
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
    m = re.search(r'[Oo]rder\s*(?:ID|#|No\.?)[:\s#]*(\d+)', text)
    result['order_id'] = m.group(1) if m else None
    m = re.search(r'[Tt]racking\s*[Nn]umber[:\s]*([A-Za-z0-9]{8,30})', text)
    result['tracking'] = m.group(1) if m else None
    result['carrier'] = 'FEDEX'
    if re.search(r'\bups\b', text, re.IGNORECASE): result['carrier'] = 'UPS'
    elif re.search(r'\busps\b', text, re.IGNORECASE): result['carrier'] = 'USPS'
    result['isbn'] = _extract_isbn(text)
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

        combined = (raw + ' ' + text).upper()
        status_signals = ['SHIPPED', 'IN TRANSIT', 'TRACKING NUMBER', 'TRACKING:']
        if not any(sig in combined for sig in status_signals):
            continue

        parsed = parse_shipping_email(text)
        if parsed.get('order_id'):
            results.append(parsed)

    mail.logout()
    log.info(f'{len(results)} shipping emails parsed')
    return results


# -- eBay OAuth ---------------------------------------------------------------
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


# -- eBay Orders API ----------------------------------------------------------
def _normalize_name(name: str) -> str:
    """Normalize name for comparison: lowercase, strip accents, strip C/O prefixes."""
    clean = re.sub(r'^(?:eIS|GSP)\s+C/O\s+', '', name, flags=re.IGNORECASE)
    return unicodedata.normalize('NFKD', clean.lower()).encode('ascii', 'ignore').decode('ascii')

def _check_isbn_match(isbn: str, item: dict) -> bool:
    """Check if ISBN matches any field on an eBay line item."""
    if not isbn:
        return False
    sku = item.get('sku', '')
    title = item.get('title', '')
    custom_label = item.get('legacyVariationId', '')
    if sku == isbn or isbn in title or custom_label == isbn:
        return True
    for prop in item.get('properties', []):
        if isinstance(prop, dict) and prop.get('name') == 'ISBN' and prop.get('value') == isbn:
            return True
    return False

def _check_name_match(buyer_name: str, order: dict) -> bool:
    """Check if buyer name matches the eBay order ship-to name."""
    if not buyer_name:
        return False
    ship_to = (order.get('fulfillmentStartInstructions') or [{}])[0] \
        .get('shippingStep', {}).get('shipTo', {})
    ebay_name = _normalize_name(ship_to.get('fullName', ''))
    bn = _normalize_name(buyer_name)
    return bn in ebay_name or ebay_name in bn

def get_order_by_id(ebay_order_id: str, token: str) -> dict | None:
    """Fetch a specific eBay order by its order ID. Used in Pass 2."""
    try:
        r = requests.get(
            f'https://api.ebay.com/sell/fulfillment/v1/order/{ebay_order_id}',
            headers={'Authorization': f'Bearer {token}'},
            timeout=15
        )
        if r.status_code == 200:
            return r.json()
        log.error(f'  Failed to fetch order {ebay_order_id}: {r.status_code}')
    except Exception as e:
        log.error(f'  Exception fetching order {ebay_order_id}: {e}')
    return None

def find_ebay_order(isbn: str, buyer_name: str, token: str) -> dict | None:
    """
    Find matching eBay order with STRICT matching:
      1. Prefer orders matching BOTH isbn AND buyer name
      2. Fall back to isbn-only if buyer name not available from email
      3. NEVER match on buyer name alone (too risky)

    This prevents the #27048 wrong-tracking bug where isbn-OR-name
    matching attached tracking to the wrong order.
    """
    headers = {'Authorization': f'Bearer {token}'}

    both_matches = []
    isbn_only_matches = []

    filter_sets = [
        'orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}',
        None,
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
                    headers=headers, params=params, timeout=15
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
                break

            for order in orders:
                isbn_match = any(_check_isbn_match(isbn, item) for item in order.get('lineItems', []))
                name_match = _check_name_match(buyer_name, order)

                if isbn_match and name_match:
                    both_matches.append(order)
                elif isbn_match:
                    isbn_only_matches.append(order)
                # name-only matches are intentionally ignored

            total = data.get('total', 0)
            offset += len(orders)
            if offset >= total:
                break

        # If we found a both-match in unfulfilled orders, use it immediately
        if both_matches:
            log.info(f"  Matched eBay order {both_matches[0]['orderId']} (isbn=True, name=True)")
            return both_matches[0]

    # After all orders: prefer both-match, then isbn-only with safety checks
    if both_matches:
        log.info(f"  Matched eBay order {both_matches[0]['orderId']} (isbn=True, name=True)")
        return both_matches[0]

    if isbn_only_matches:
        if not buyer_name:
            # No buyer name from email -- isbn-only is acceptable
            log.info(f"  Matched eBay order {isbn_only_matches[0]['orderId']} (isbn=True, name=N/A)")
            return isbn_only_matches[0]
        elif len(isbn_only_matches) == 1:
            # Buyer name available but did not match -- only accept if single ISBN match
            log.warning(f"  Matched eBay order {isbn_only_matches[0]['orderId']} "
                        f"(isbn=True, name=False -- single match, proceeding with caution)")
            return isbn_only_matches[0]
        else:
            # Multiple isbn-only matches and name did not match any -- too risky
            log.warning(f"  {len(isbn_only_matches)} ISBN matches but name '{buyer_name}' "
                        f"matched none -- skipping to avoid wrong-order match")
            return None

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
            log.info(f'  Marked shipped + tracking {tracking} -> eBay order {ebay_order_id}')
        else:
            log.info(f'  Marked shipped (no tracking) -> eBay order {ebay_order_id}')
        return True

    log.error(f'  Failed: {r.status_code} {r.text[:300]}')
    return False


# -- Main ---------------------------------------------------------------------
def run():
    log.info('=' * 60)
    log.info('shipping_tracker.py started')
    log.info(f'TRACKING_ENABLED = {TRACKING_ENABLED}')

    state = load_state()

    # -- State migration --
    migrated = 0
    for oid, entry in state.items():
        if entry.get('status') == 'posted' and not entry.get('tracking'):
            entry['status'] = 'shipped_no_tracking'
            migrated += 1
    if migrated:
        log.info(f'State migration: reset {migrated} stale "posted" entries to "shipped_no_tracking"')
        save_state(state)

    emails = get_shipping_emails()

    if not emails:
        log.info('No shipping emails to process')
        log.info('=' * 60)
        return

    token = get_ebay_token()
    log.info('eBay token acquired')

    # Track results for email report
    shipped_list = []
    skipped_list = []
    failed_list  = []

    posted = shipped_no_tracking = skipped = pending = failed = 0

    # Deduplicate: preserve tracking from earlier emails
    by_order = {}
    for e in emails:
        existing = by_order.get(e['order_id'])
        if existing and existing.get('tracking') and not e.get('tracking'):
            e['tracking'] = existing['tracking']
            e['carrier'] = existing.get('carrier', e.get('carrier'))
        by_order[e['order_id']] = e

    for order_id, parsed in by_order.items():
        tracking   = parsed['tracking']
        carrier    = parsed['carrier']
        isbn       = parsed['isbn']
        buyer      = parsed['buyer_name']
        existing   = state.get(order_id, {})

        # -- FULLY DONE --
        if existing.get('status') == 'posted':
            log.info(f'Order #{order_id}: fully complete -- skipping')
            skipped += 1
            skipped_list.append({
                'isbn': isbn or '', 'title': f'Order #{order_id}',
                'reason': 'Already fully posted',
            })
            continue

        # -- PASS 2: tracking now available --
        if existing.get('status') == 'shipped_no_tracking':
            if not tracking:
                log.info(f'Order #{order_id}: shipped, still no tracking -- waiting')
                pending += 1
                skipped_list.append({
                    'isbn': isbn or '', 'title': f'Order #{order_id}',
                    'reason': 'Awaiting tracking number',
                })
                continue

            if not TRACKING_ENABLED:
                log.info(f'Order #{order_id}: tracking available but TRACKING_ENABLED=False -- skipping')
                pending += 1
                continue

            ebay_order_id = existing.get('ebay_order_id')
            if not ebay_order_id:
                log.warning(f'Order #{order_id}: no stored ebay_order_id -- cannot add tracking')
                pending += 1
                continue

            # Fetch the specific order by stored ID (do not re-search)
            ebay_order = get_order_by_id(ebay_order_id, token)
            if not ebay_order:
                log.warning(f'Order #{order_id}: could not fetch stored order {ebay_order_id}')
                pending += 1
                failed_list.append({
                    'isbn': isbn or '', 'title': f'Order #{order_id}',
                    'order_id': ebay_order_id, 'error': 'Could not fetch stored order',
                })
                save_state(state)
                time.sleep(0.5)
                continue

            ok = post_shipped(ebay_order_id, token, ebay_order,
                              tracking=tracking, carrier=carrier)
            if ok:
                state[order_id]['status'] = 'posted'
                state[order_id]['tracking'] = tracking
                state[order_id]['carrier'] = carrier
                state[order_id]['tracking_posted_at'] = datetime.now().isoformat()
                posted += 1
                shipped_list.append({
                    'isbn': isbn or '', 'title': f'Order #{order_id}',
                    'order_id': ebay_order_id, 'tracking': tracking,
                })
            else:
                log.warning(f'Order #{order_id}: tracking update failed -- will retry')
                pending += 1
                failed_list.append({
                    'isbn': isbn or '', 'title': f'Order #{order_id}',
                    'order_id': ebay_order_id, 'error': 'Tracking POST failed',
                })
            save_state(state)
            time.sleep(0.5)
            continue

        # -- PASS 1: new order -- find eBay match, save state only --
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
            failed_list.append({
                'isbn': isbn or '', 'title': f'Order #{order_id}',
                'order_id': '', 'error': 'No matching eBay order found',
            })
            save_state(state)
            continue

        ebay_order_id = ebay_order['orderId']

        if tracking and TRACKING_ENABLED:
            # Have tracking already -- create fulfillment with tracking in one call
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
                shipped_list.append({
                    'isbn': isbn or '', 'title': f'Order #{order_id}',
                    'order_id': ebay_order_id, 'tracking': tracking,
                })
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
                failed_list.append({
                    'isbn': isbn or '', 'title': f'Order #{order_id}',
                    'order_id': ebay_order_id, 'error': 'Fulfillment POST failed',
                })
        else:
            # No tracking yet -- record in state only, do NOT call eBay
            state[order_id] = {
                'status': 'shipped_no_tracking',
                'ebay_order_id': ebay_order_id,
                'isbn': isbn, 'buyer': buyer,
                'shipped_at': datetime.now().isoformat()
            }
            shipped_no_tracking += 1
            shipped_list.append({
                'isbn': isbn or '', 'title': f'Order #{order_id}',
                'order_id': ebay_order_id, 'tracking': '',
            })

        save_state(state)
        time.sleep(0.5)

    log.info('=' * 60)
    log.info(f'DONE: {posted} posted w/tracking | {shipped_no_tracking} marked shipped (no tracking) | '
             f'{pending} awaiting tracking | {skipped} already done | {failed} failed/no match')
    log.info('=' * 60)

    # -- Send HTML email report -----------------------------------------------
    if (shipped_list or skipped_list or failed_list) and send_html_email and build_shipping_tracker_email:
        subject = (f'[shipping_tracker] {len(shipped_list)} shipped, '
                   f'{len(skipped_list)} skipped, {len(failed_list)} failed')
        html = build_shipping_tracker_email(shipped_list, skipped_list, failed_list, TRACKING_ENABLED)
        send_html_email(subject, html)


if __name__ == '__main__':
    run()
