#!/usr/bin/env python3
"""
shipping_tracker.py — Automated BooksGoat shipping email → eBay shipped status.

TRACKING DISABLED after order #27048 wrong-tracking incident (2026-05-01).
Currently: marks orders as shipped on eBay (no tracking).
Set TRACKING_ENABLED = True after auditing matching logic to re-enable.

- Polls Gmail via IMAP for BooksGoat shipping emails
- Parses order ID, ISBN, buyer name
- Finds matching eBay order via Orders API
- Marks order as shipped (FULFILLED) on eBay
- Tracks state in shipping_state.json to avoid duplicates
- Runs every 2 hours via GitHub Actions

FIX LOG (2026-05-01):
  - Guard 1: Only match unfulfilled eBay orders (removed fallback to all orders)
  - Guard 2: Skip eBay orders already claimed in shipping_state.json by a different BG order
  - Guard 3: Require buyer name match when multiple ISBN matches exist in unfulfilled orders
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

# ── Safety toggle ─────────────────────────────────────────────────────────────
# TRACKING DISABLED after order #27048 wrong-tracking incident.
# Set to True ONLY after auditing tracking-to-order matching logic.
TRACKING_ENABLED = False

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


# ── Claimed-order helper ──────────────────────────────────────────────────────
def _get_claimed_ebay_orders(state: dict, current_bg_order_id: str) -> set:
    """Return set of eBay order IDs already claimed by OTHER BooksGoat orders.

    This prevents the duplicate-ISBN bug: if BG orders 27039 and 27048 both
    match the same ISBN, and 27039 already claimed eBay order 15-14553-42498,
    then 27048 must NOT match that same eBay order.
    """
    claimed = set()
    for bg_id, entry in state.items():
        if bg_id == current_bg_order_id:
            continue  # don't block ourselves
        ebay_id = entry.get('ebay_order_id')
        if ebay_id and entry.get('status') in ('shipped', 'posted', 'failed'):
            claimed.add(ebay_id)
    return claimed


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


# ── Name matching helper ──────────────────────────────────────────────────────
def _normalize_name(name: str) -> str:
    """Normalize a name for comparison: lowercase, strip accents, ASCII-only."""
    return unicodedata.normalize('NFKD', name.lower()) \
        .encode('ascii', 'ignore').decode('ascii').strip()

def _names_match(name_a: str | None, name_b: str) -> bool:
    """Fuzzy name match: either name contains the other (handles middle names, initials)."""
    if not name_a:
        return False
    a = _normalize_name(name_a)
    b = _normalize_name(name_b)
    if not a or not b:
        return False
    return a in b or b in a


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
def find_ebay_order(isbn: str, buyer_name: str | None, token: str,
                    state: dict, bg_order_id: str) -> dict | None:
    """Find the matching eBay order for a BooksGoat shipment.

    Three safety guards (added 2026-05-01 after order #27048 incident):

    Guard 1 — UNFULFILLED ONLY
        Only search orders with status NOT_STARTED or IN_PROGRESS.
        Never fall back to searching all orders. An already-fulfilled order
        should never be matched again.

    Guard 2 — CLAIMED-ORDER CHECK
        Skip any eBay order that is already mapped to a DIFFERENT BooksGoat
        order in shipping_state.json. This prevents the duplicate-ISBN bug
        where BG orders 27039 and 27048 (same ISBN) both matched the same
        eBay order.

    Guard 3 — BUYER NAME DISAMBIGUATION
        Collect ALL ISBN matches from unfulfilled orders. If there is exactly
        one match, accept it (single-match = unambiguous). If there are
        multiple matches (same ISBN sold to different buyers), require a
        buyer name match to disambiguate. If no name is available from the
        email, reject the match as ambiguous.
    """
    headers = {'Authorization': f'Bearer {token}'}
    claimed = _get_claimed_ebay_orders(state, bg_order_id)

    # ── Guard 1: Only search unfulfilled orders ──
    # No fallback pass. If the order is already fulfilled, we should not
    # be touching it again.
    all_isbn_matches = []  # collect all ISBN-matching unfulfilled orders
    offset = 0
    while True:
        params = {
            'limit': 200,
            'offset': offset,
            'filter': 'orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}'
        }
        try:
            r = requests.get(
                'https://api.ebay.com/sell/fulfillment/v1/order',
                headers=headers, params=params, timeout=15)
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
            ebay_order_id = order['orderId']

            # ── Guard 2: Skip orders already claimed by another BG order ──
            if ebay_order_id in claimed:
                log.info(f"  Skipping eBay order {ebay_order_id} — already claimed "
                         f"by another BooksGoat order")
                continue

            # Check ISBN match across all line items
            isbn_match = False
            for item in order.get('lineItems', []):
                title = item.get('title', '')
                sku = item.get('sku', '')
                custom_label = item.get('legacyVariationId', '')
                if isbn:
                    if sku == isbn or isbn in title or custom_label == isbn:
                        isbn_match = True
                        break
                    for prop in item.get('properties', []):
                        if isinstance(prop, dict) and prop.get('name') == 'ISBN' \
                                and prop.get('value') == isbn:
                            isbn_match = True
                            break
                    if isbn_match:
                        break

            if isbn_match:
                # Extract buyer name from this order for Guard 3
                ship_to = (order.get('fulfillmentStartInstructions') or [{}])[0] \
                    .get('shippingStep', {}).get('shipTo', {})
                ebay_name_raw = ship_to.get('fullName', '')
                ebay_name_clean = re.sub(r'^(?:eIS|GSP)\s+C/O\s+', '',
                                         ebay_name_raw, flags=re.IGNORECASE)
                all_isbn_matches.append({
                    'order': order,
                    'ebay_name': ebay_name_clean,
                    'name_match': _names_match(buyer_name, ebay_name_clean)
                })

        total = data.get('total', 0)
        offset += len(orders)
        if offset >= total:
            break

    # ── Guard 3: Disambiguate by buyer name if multiple matches ──
    if not all_isbn_matches:
        log.warning(f'No unfulfilled eBay order match for ISBN={isbn}, buyer={buyer_name}')
        return None

    if len(all_isbn_matches) == 1:
        # Single match — unambiguous, accept regardless of name
        match = all_isbn_matches[0]
        oid = match['order']['orderId']
        log.info(f"  Single ISBN match: eBay order {oid} "
                 f"(buyer_name_match={match['name_match']}, "
                 f"ebay_buyer={match['ebay_name']})")
        return match['order']

    # Multiple ISBN matches — same book sold to multiple buyers.
    # This is exactly the scenario that caused the #27048 bug.
    # Require buyer name match to disambiguate.
    log.info(f"  Multiple ISBN matches ({len(all_isbn_matches)}) for ISBN={isbn} "
             f"— requiring buyer name match to disambiguate")

    name_matches = [m for m in all_isbn_matches if m['name_match']]

    if len(name_matches) == 1:
        match = name_matches[0]
        oid = match['order']['orderId']
        log.info(f"  Disambiguated by name: eBay order {oid} "
                 f"(buyer={buyer_name} ↔ ebay_buyer={match['ebay_name']})")
        return match['order']

    if len(name_matches) > 1:
        # Multiple name matches too (e.g., same buyer bought same book twice)
        # Still ambiguous — reject to be safe
        oids = [m['order']['orderId'] for m in name_matches]
        log.warning(f"  AMBIGUOUS: {len(name_matches)} orders match both ISBN={isbn} "
                    f"and buyer={buyer_name}: {oids} — REJECTING")
        return None

    # No name matches among the multiple ISBN matches
    if not buyer_name:
        log.warning(f"  AMBIGUOUS: {len(all_isbn_matches)} ISBN matches but no buyer name "
                    f"parsed from email — REJECTING")
    else:
        ebay_names = [m['ebay_name'] for m in all_isbn_matches]
        log.warning(f"  AMBIGUOUS: {len(all_isbn_matches)} ISBN matches for ISBN={isbn}, "
                    f"but buyer '{buyer_name}' doesn't match any eBay buyer: "
                    f"{ebay_names} — REJECTING")
    return None


def post_shipped(ebay_order_id: str, token: str, order: dict,
                 tracking: str = None, carrier: str = None) -> bool:
    """Mark order as shipped on eBay. Only includes tracking if TRACKING_ENABLED."""
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

    # Only add tracking if TRACKING_ENABLED flag is True
    if TRACKING_ENABLED and tracking:
        payload['trackingNumber'] = tracking
        payload['shippingCarrierCode'] = carrier_map.get(carrier, 'FedEx')
        log.info(f'  Posting shipped + tracking {tracking}')
    else:
        if tracking:
            log.info(f'  Tracking {tracking} available but TRACKING_ENABLED=False — marking shipped only')
        else:
            log.info(f'  No tracking available — marking shipped only')

    r = requests.post(
        f'https://api.ebay.com/sell/fulfillment/v1/order/{ebay_order_id}/shipping_fulfillment',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json'},
        json=payload,
        timeout=15
    )
    if r.status_code in (200, 201):
        log.info(f'  Marked shipped on eBay order {ebay_order_id}')
        return True

    # Already fulfilled — not an error
    if r.status_code == 409 or (r.status_code == 400 and 'already' in r.text.lower()):
        log.info(f'  Order {ebay_order_id} already fulfilled — skipping')
        return True

    log.error(f'  Failed: {r.status_code} {r.text[:300]}')
    return False


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    log.info('=' * 60)
    log.info(f'shipping_tracker.py started (TRACKING_ENABLED={TRACKING_ENABLED})')

    state = load_state()
    emails = get_shipping_emails()

    if not emails:
        log.info('No shipping emails to process')
        log.info('=' * 60)
        return

    token = get_ebay_token()
    log.info('eBay token acquired')

    marked_shipped = skipped = failed = 0

    # Deduplicate by order_id
    by_order = {}
    for e in emails:
        existing = by_order.get(e['order_id'])
        if existing and existing.get('tracking') and not e.get('tracking'):
            e['tracking'] = existing['tracking']
            e['carrier'] = existing.get('carrier', e.get('carrier'))
        by_order[e['order_id']] = e

    for order_id, parsed in by_order.items():
        tracking = parsed['tracking']
        carrier  = parsed['carrier']
        isbn     = parsed['isbn']
        buyer    = parsed['buyer_name']
        existing = state.get(order_id, {})

        # Already processed — skip
        if existing.get('status') in ('shipped', 'posted'):
            log.info(f'Order #{order_id}: already processed — skipping')
            skipped += 1
            continue

        # ISBN is required for matching (name-only matching disabled for safety)
        if not isbn:
            log.warning(f'Order #{order_id}: no ISBN parsed from email — cannot match safely, skipping')
            state[order_id] = {
                'status': 'no_isbn',
                'isbn': isbn, 'tracking': tracking, 'carrier': carrier,
                'buyer': buyer,
                'last_attempt': datetime.now().isoformat()
            }
            failed += 1
            save_state(state)
            continue

        # Find matching eBay order — passes state + BG order ID for Guard 2
        ebay_order = find_ebay_order(isbn, buyer, token, state, order_id)
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

        # Mark as shipped (tracking only included if TRACKING_ENABLED)
        ok = post_shipped(ebay_order_id, token, ebay_order,
                          tracking=tracking, carrier=carrier)

        if ok:
            state[order_id] = {
                'status': 'shipped',
                'ebay_order_id': ebay_order_id,
                'isbn': isbn,
                'tracking': tracking,  # stored but not posted to eBay
                'tracking_posted': TRACKING_ENABLED and bool(tracking),
                'carrier': carrier,
                'buyer': buyer,
                'shipped_at': datetime.now().isoformat()
            }
            marked_shipped += 1
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

        save_state(state)
        time.sleep(0.5)

    log.info('=' * 60)
    log.info(f'DONE: {marked_shipped} marked shipped | '
             f'{skipped} already done | {failed} failed/no match | '
             f'TRACKING_ENABLED={TRACKING_ENABLED}')
    log.info('=' * 60)

if __name__ == '__main__':
    run()
