#!/usr/bin/env python3
"""
order_status_report.py — Daily unified order lifecycle reporter
Runs daily 8AM UTC via GitHub Actions.

Cross-references three signals to track every eBay order:
  1. eBay Orders API     → all sales (last 14 days)
  2. BG order confirmation emails → marks order placed on BooksGoat
  3. eBay order shipment status → marks tracking added

Sends daily report grouped by status, flagging stuck orders.

Email parsing:
  - Subject contains: "Your order has been Confirmed" OR "THANK YOU FOR PLACING YOUR ORDER"
  - From: any sender (we ignore From, match by ISBN in body)
  - ISBN extracted from body: "(ISBN 9781234567890)"

Required env vars:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN
  SMTP_USER, SMTP_PASSWORD (Gmail app password — also used for IMAP read)
  EMAIL_TO, EMAIL_FROM
"""

import os, base64, imaplib, email, re, smtplib, requests
from datetime import datetime, timezone, timedelta
from email.header import decode_header
from email.mime.text import MIMEText

# ── Config ────────────────────────────────────────────────────────────────
EBAY_APP_ID        = os.environ['EBAY_CLIENT_ID']
EBAY_CERT_ID       = os.environ['EBAY_CLIENT_SECRET']
EBAY_REFRESH_TOKEN = os.environ['EBAY_REFRESH_TOKEN']

SMTP_HOST     = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT     = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER     = os.environ['SMTP_USER']
SMTP_PASSWORD = os.environ['SMTP_PASSWORD']
EMAIL_FROM    = os.environ.get('EMAIL_FROM', SMTP_USER)
EMAIL_TO      = os.environ.get('EMAIL_TO', SMTP_USER)

IMAP_HOST   = 'imap.gmail.com'
LOOKBACK_DAYS = 14
URGENT_HOURS  = 24   # flag orders stuck in awaiting_bg_order > 24h


# ── eBay ──────────────────────────────────────────────────────────────────
def get_token():
    creds = base64.b64encode(f'{EBAY_APP_ID}:{EBAY_CERT_ID}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data=('grant_type=refresh_token'
              f'&refresh_token={EBAY_REFRESH_TOKEN}'
              '&scope=https://api.ebay.com/oauth/api_scope/sell.fulfillment'),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()['access_token']


def fetch_orders(token):
    """Fetch all orders from last LOOKBACK_DAYS days."""
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime(
        '%Y-%m-%dT%H:%M:%S.000Z'
    )
    headers = {'Authorization': f'Bearer {token}'}
    orders = []
    offset = 0
    while True:
        r = requests.get(
            'https://api.ebay.com/sell/fulfillment/v1/order',
            headers=headers,
            params={'filter': f'creationdate:[{since}]', 'limit': 50, 'offset': offset},
            timeout=20,
        )
        if r.status_code != 200:
            print(f'Orders API error {r.status_code}: {r.text[:200]}')
            break
        data = r.json()
        batch = data.get('orders', [])
        orders.extend(batch)
        if len(orders) >= data.get('total', 0) or not batch:
            break
        offset += 50
    return orders


# ── BooksGoat order confirmation email scan ──────────────────────────────
def scan_bg_confirmations():
    """
    Scan inbox for BG order confirmation emails. Returns set of ISBNs that
    have been ordered on BooksGoat (within lookback window).
    """
    confirmed_isbns = set()
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST)
        m.login(SMTP_USER, SMTP_PASSWORD)
        m.select('INBOX', readonly=True)

        since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime('%d-%b-%Y')

        # Two different subject patterns BooksGoat uses
        queries = [
            f'(SINCE {since} SUBJECT "order has been Confirmed")',
            f'(SINCE {since} SUBJECT "THANK YOU FOR PLACING YOUR ORDER")',
            f'(SINCE {since} SUBJECT "Order Confirmed")',
        ]

        for query in queries:
            typ, data = m.search(None, query)
            if typ != 'OK':
                continue
            for num in data[0].split():
                typ, msg_data = m.fetch(num, '(RFC822)')
                if typ != 'OK':
                    continue
                msg = email.message_from_bytes(msg_data[0][1])

                body = ''
                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        if ctype in ('text/plain', 'text/html'):
                            try:
                                body += part.get_payload(decode=True).decode(errors='ignore')
                            except Exception:
                                pass
                else:
                    try:
                        body = msg.get_payload(decode=True).decode(errors='ignore')
                    except Exception:
                        pass

                # Extract ISBNs — pattern: (ISBN 9781234567890)
                for match in re.finditer(r'ISBN[\s:]*?(97[89]\d{10})', body):
                    confirmed_isbns.add(match.group(1))

        m.close()
        m.logout()
    except Exception as e:
        print(f'IMAP scan failed: {e}')
    return confirmed_isbns


# ── Order classification ─────────────────────────────────────────────────
def classify_order(order, bg_confirmed_isbns):
    """
    Returns (status, info_dict).
    Status: tracking_added | bg_ordered | awaiting_bg_order
    """
    # Check if eBay has tracking on this order
    has_tracking = False
    for fs in order.get('fulfillmentStartInstructions', []):
        for ship in (fs.get('shippingStep', {}).get('shipmentInfo', {}) or {}).items():
            pass
    # Better: check fulfillmentInstructions or use orderFulfillmentStatus
    fulfillment_status = order.get('orderFulfillmentStatus', '')
    if fulfillment_status in ('FULFILLED', 'IN_PROGRESS'):
        # Verify tracking actually exists
        for fi in order.get('fulfillmentHrefs', []):
            has_tracking = True
            break
        # Fallback: assume IN_PROGRESS or FULFILLED means tracking is added
        has_tracking = True

    # Get ISBNs from line items
    order_isbns = set()
    for item in order.get('lineItems', []):
        sku = item.get('sku', '').strip()
        if sku and re.match(r'^97[89]\d{10}$', sku):
            order_isbns.add(sku)

    bg_match = bool(order_isbns & bg_confirmed_isbns)

    if has_tracking:
        status = 'tracking_added'
    elif bg_match:
        status = 'bg_ordered'
    else:
        status = 'awaiting_bg_order'

    return status, {
        'isbns': order_isbns,
        'bg_match': bg_match,
        'has_tracking': has_tracking,
    }


# ── Email ────────────────────────────────────────────────────────────────
def send_email(subject, body):
    msg = MIMEText(body, 'plain')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    print(f'Email sent: {subject}')


def format_order_line(order, info):
    order_id = order.get('orderId', 'N/A')
    created = order.get('creationDate', '')[:19].replace('T', ' ')
    try:
        sale_dt = datetime.fromisoformat(order.get('creationDate', '').replace('Z', '+00:00'))
        age_h = (datetime.now(timezone.utc) - sale_dt).total_seconds() / 3600
        age_str = f'{int(age_h)}h ago' if age_h < 48 else f'{int(age_h/24)}d ago'
    except Exception:
        age_str = '?'

    items = order.get('lineItems', [])
    titles = [it.get('title', '')[:50] for it in items]
    isbns  = [it.get('sku', '') for it in items]

    return (f'  Order {order_id} | {age_str} | {created} UTC\n'
            f'    {titles[0] if titles else "?"}\n'
            f'    ISBN: {", ".join(isbns)}')


def run():
    now = datetime.now(timezone.utc)
    print(f'Order status report — {now.isoformat()}')

    token = get_token()
    orders = fetch_orders(token)
    print(f'Fetched {len(orders)} orders from last {LOOKBACK_DAYS} days')

    bg_confirmed = scan_bg_confirmations()
    print(f'BG confirmation emails found for {len(bg_confirmed)} ISBNs')

    buckets = {
        'urgent':            [],
        'awaiting_bg_order': [],
        'bg_ordered':        [],
        'tracking_added':    [],
    }

    for order in orders:
        status, info = classify_order(order, bg_confirmed)

        # Skip already-shipped (final state) — keep noise low
        if status == 'tracking_added':
            buckets['tracking_added'].append((order, info))
            continue

        # Determine urgency
        try:
            sale_dt = datetime.fromisoformat(order.get('creationDate', '').replace('Z', '+00:00'))
            age_h = (now - sale_dt).total_seconds() / 3600
        except Exception:
            age_h = 0

        if status == 'awaiting_bg_order' and age_h > URGENT_HOURS:
            buckets['urgent'].append((order, info))
        else:
            buckets[status].append((order, info))

    # Build report
    body = [
        f'📊 ORDER STATUS REPORT — {now.strftime("%a %b %d, %Y")}',
        f'Lookback: {LOOKBACK_DAYS} days | Total orders: {len(orders)}',
        '=' * 60,
        '',
    ]

    if buckets['urgent']:
        body.append(f'🔴 URGENT — Awaiting BG order (>{URGENT_HOURS}h since sale): {len(buckets["urgent"])}')
        for order, info in buckets['urgent']:
            body.append(format_order_line(order, info))
        body.append('')

    if buckets['awaiting_bg_order']:
        body.append(f'🟡 Awaiting BG order (recent): {len(buckets["awaiting_bg_order"])}')
        for order, info in buckets['awaiting_bg_order']:
            body.append(format_order_line(order, info))
        body.append('')

    if buckets['bg_ordered']:
        body.append(f'🟢 BG ordered, awaiting shipment: {len(buckets["bg_ordered"])}')
        for order, info in buckets['bg_ordered']:
            body.append(format_order_line(order, info))
        body.append('')

    body.append(f'✅ Shipped with tracking: {len(buckets["tracking_added"])} orders')
    body.append('')

    body.append('=' * 60)
    body.append('Action: For URGENT and awaiting-BG-order items, place orders on BooksGoat.')
    body.append('Once BG sends a confirmation email, the next report will mark them green.')

    # Subject reflects urgency
    if buckets['urgent']:
        subject = f'🔴 Order Status: {len(buckets["urgent"])} URGENT — {now.strftime("%a %b %d")}'
    elif buckets['awaiting_bg_order']:
        subject = f'🟡 Order Status: {len(buckets["awaiting_bg_order"])} pending — {now.strftime("%a %b %d")}'
    else:
        subject = f'✅ Order Status: all clear — {now.strftime("%a %b %d")}'

    send_email(subject, '\n'.join(body))


if __name__ == '__main__':
    run()
