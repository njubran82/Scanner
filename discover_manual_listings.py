#!/usr/bin/env python3
"""
discover_manual_listings.py — Find eBay listings not tracked in CSV.

Fetches ALL offers from eBay Inventory API, cross-references with
booksgoat_enhanced.csv, and identifies listings that exist on eBay
but are missing from the CSV (manual Seller Hub listings).

For each discovered listing:
  - Adds to CSV as status=pending (so full_publish.py will relist via API)
  - Records current price, title, offer_id, listing_id
  - Sends email report with full list

Workflow:
  1. Run this script → discover untracked listings, add to CSV
  2. User ends manual listings in Seller Hub
  3. Run full_publish.py → relists them properly via API

Run: GitHub Actions discover_manual.yml (manual trigger)
     or locally: python discover_manual_listings.py
"""

import os, csv, base64, time, logging, requests
from datetime import datetime, timezone
from pathlib import Path

try:
    from email_helpers import (
        _email_wrapper, _summary_bar, _table_header, _table_row,
        _badge, send_html_email
    )
    HAS_EMAIL_HELPERS = True
except ImportError:
    HAS_EMAIL_HELPERS = False

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID') or os.getenv('EBAY_APP_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET') or os.getenv('EBAY_CERT_ID')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')

CSV_PATH = Path('booksgoat_enhanced.csv')
LOG_FILE = 'discover_manual.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── Auth ─────────────────────────────────────────────────────────────────────
def get_user_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type': 'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': 'https://api.ebay.com/oauth/api_scope/sell.inventory',
        }
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Token error: {data}')
    return data['access_token']


# ── CSV helpers ──────────────────────────────────────────────────────────────
def load_csv() -> dict:
    if not CSV_PATH.exists():
        return {}
    rows = {}
    with CSV_PATH.open(encoding='utf-8') as f:
        for row in csv.DictReader(f):
            isbn = row.get('isbn13', '').strip()
            if isbn:
                rows[isbn] = row
    return rows


def save_csv(rows: dict):
    all_rows = list(rows.values())
    all_fields = list(dict.fromkeys(k for r in all_rows for k in r))
    tmp = CSV_PATH.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(all_rows)
    tmp.replace(CSV_PATH)


# ── Fetch all inventory items ────────────────────────────────────────────────
def fetch_all_inventory_items(token: str) -> dict:
    """
    Fetch ALL inventory items from eBay Inventory API.
    Returns dict of sku -> item data.
    """
    hdrs = {'Authorization': f'Bearer {token}'}
    all_items = {}
    offset = 0
    limit = 100

    while True:
        try:
            r = requests.get(
                'https://api.ebay.com/sell/inventory/v1/inventory_item',
                headers=hdrs,
                params={'limit': limit, 'offset': offset},
                timeout=15
            )
        except Exception as e:
            log.error(f'Inventory API request failed: {e}')
            break

        if r.status_code != 200:
            log.error(f'Inventory API error: {r.status_code} {r.text[:200]}')
            break

        data = r.json()
        items = data.get('inventoryItems', [])
        for item in items:
            sku = item.get('sku', '')
            if sku:
                all_items[sku] = item

        total = data.get('total', 0)
        offset += len(items)
        log.info(f'  Fetched {offset}/{total} inventory items')

        if offset >= total or not items:
            break

        time.sleep(0.2)

    log.info(f'Total inventory items on eBay: {len(all_items)}')
    return all_items


# ── Fetch all offers ─────────────────────────────────────────────────────────
def fetch_all_offers(token: str) -> dict:
    """
    Fetch ALL offers from eBay Inventory API.
    Returns dict of sku -> list of offers.
    """
    hdrs = {'Authorization': f'Bearer {token}'}
    offers_by_sku = {}
    offset = 0
    limit = 100

    while True:
        try:
            r = requests.get(
                'https://api.ebay.com/sell/inventory/v1/offer',
                headers=hdrs,
                params={'limit': limit, 'offset': offset},
                timeout=15
            )
        except Exception as e:
            log.error(f'Offer API request failed: {e}')
            break

        if r.status_code != 200:
            # 404 means no offers at all
            if r.status_code == 404:
                log.info('No offers found on eBay')
                break
            log.error(f'Offer API error: {r.status_code} {r.text[:200]}')
            break

        data = r.json()
        offers = data.get('offers', [])
        for offer in offers:
            sku = offer.get('sku', '')
            if sku:
                if sku not in offers_by_sku:
                    offers_by_sku[sku] = []
                offers_by_sku[sku].append(offer)

        total = data.get('total', 0)
        offset += len(offers)
        log.info(f'  Fetched {offset}/{total} offers')

        if offset >= total or not offers:
            break

        time.sleep(0.2)

    log.info(f'Total SKUs with offers on eBay: {len(offers_by_sku)}')
    return offers_by_sku


# ── Build HTML email ─────────────────────────────────────────────────────────
def build_discover_email(discovered, already_tracked, ebay_only_no_offer):
    """Build HTML report."""
    summary = _summary_bar([
        ("Tracked in CSV", str(already_tracked), "green"),
        ("Discovered (New)", str(len(discovered)), "blue" if discovered else "gray"),
        ("eBay-Only (No Offer)", str(len(ebay_only_no_offer)), "orange" if ebay_only_no_offer else "gray"),
    ])

    parts = []

    if discovered:
        rows = ""
        for i, d in enumerate(discovered):
            status_text = d.get('offer_status', '')
            status_color = "green" if status_text == "PUBLISHED" else "orange"
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{d["isbn"]}</code>',
                d['title'][:40],
                f'${d["price"]:.2f}' if d.get('price') else 'N/A',
                _badge(status_text, status_color),
                _badge("added to CSV", "blue"),
            ], i)
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#1565c0;">'
            f'Discovered Manual Listings ({len(discovered)})</h3>'
            f'<p style="font-size:12px;color:#666;margin:0 0 8px;">'
            f'Added to CSV as status=pending. End these in Seller Hub, '
            f'then run full_publish.py to relist via API.</p>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
            f'{_table_header(["#", "ISBN", "Title", "Price", "eBay Status", "Action"])}'
            f'{rows}</table>'
        )
    else:
        parts.append(
            '<p style="font-size:14px;color:#2e7d32;">No untracked listings found. '
            'All eBay listings are in the CSV.</p>'
        )

    if ebay_only_no_offer:
        rows = ""
        for i, d in enumerate(ebay_only_no_offer):
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{d["isbn"]}</code>',
                d['title'][:45],
                'No active offer',
            ], i)
        parts.append(
            f'<h3 style="margin:12px 0 8px;font-size:14px;color:#e65100;">'
            f'Inventory Items Without Offers ({len(ebay_only_no_offer)})</h3>'
            f'<p style="font-size:12px;color:#666;margin:0 0 8px;">'
            f'These have inventory items on eBay but no active offer. '
            f'Likely orphaned from failed publish attempts.</p>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
            f'{_table_header(["#", "ISBN", "Title", "Status"])}'
            f'{rows}</table>'
        )

    footer = ("Next steps: End the discovered listings in Seller Hub, "
              "then run full_publish.py to relist via API."
              if discovered else "")

    return _email_wrapper("MANUAL LISTING DISCOVERY", summary, "\n".join(parts), footer)


# ── Main ─────────────────────────────────────────────────────────────────────
def run():
    log.info('=' * 60)
    log.info(f'DISCOVER MANUAL LISTINGS — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info('=' * 60)

    rows = load_csv()
    csv_isbns = set(rows.keys())
    log.info(f'CSV has {len(csv_isbns)} ISBNs')

    token = get_user_token()

    # Fetch everything from eBay
    inventory = fetch_all_inventory_items(token)
    token = get_user_token()  # refresh after potentially long fetch
    offers = fetch_all_offers(token)

    # All SKUs known to eBay
    ebay_skus = set(inventory.keys()) | set(offers.keys())
    log.info(f'eBay knows {len(ebay_skus)} unique SKUs')

    # Find SKUs on eBay but not in CSV
    missing_from_csv = ebay_skus - csv_isbns
    log.info(f'SKUs on eBay but not in CSV: {len(missing_from_csv)}')

    discovered = []
    ebay_only_no_offer = []
    already_tracked = len(csv_isbns & ebay_skus)

    for sku in sorted(missing_from_csv):
        # Skip non-ISBN SKUs (some may be legacy or test)
        if not sku.isdigit() or len(sku) != 13:
            log.info(f'  SKIP non-ISBN SKU: {sku}')
            continue

        inv_item = inventory.get(sku, {})
        sku_offers = offers.get(sku, [])

        title = inv_item.get('product', {}).get('title', sku)

        # Check if there's an active (PUBLISHED) offer
        published_offer = None
        for offer in sku_offers:
            if offer.get('status') == 'PUBLISHED':
                published_offer = offer
                break

        if published_offer:
            price = 0.0
            try:
                price = float(published_offer.get('pricingSummary', {})
                              .get('price', {}).get('value', 0))
            except (ValueError, TypeError):
                pass

            offer_id = published_offer.get('offerId', '')
            listing_id = published_offer.get('listing', {}).get('listingId', '')
            offer_status = published_offer.get('status', '')

            log.info(f'  DISCOVERED: {sku} — {title[:45]} — ${price} — {offer_status}')

            discovered.append({
                'isbn': sku,
                'title': title,
                'price': price,
                'offer_id': offer_id,
                'listing_id': listing_id,
                'offer_status': offer_status,
            })

            # Add to CSV as pending
            rows[sku] = {
                'isbn13': sku,
                'title': title,
                'format': inv_item.get('product', {}).get('aspects', {}).get('Format', ['Paperback'])[0],
                'cost': '',
                'product_url': '',
                'category_path': 'discovered',
                'sell_price': str(price),
                'status': 'pending',
                'score': '',
                'listed_at': '',
                'sold_at': '',
                'delisted_at': '',
                'delist_reason': '',
                'checked_at': '',
                'offer_id': '',  # Clear — full_publish will create fresh
                'description': inv_item.get('product', {}).get('description', ''),
            }

        elif sku_offers:
            # Has offers but none are PUBLISHED
            statuses = [o.get('status', '?') for o in sku_offers]
            log.info(f'  ORPHAN: {sku} — {title[:45]} — offers: {statuses}')
            ebay_only_no_offer.append({'isbn': sku, 'title': title})

            # Still add to CSV so we track it
            if sku not in rows:
                rows[sku] = {
                    'isbn13': sku,
                    'title': title,
                    'format': 'Paperback',
                    'cost': '',
                    'product_url': '',
                    'category_path': 'discovered',
                    'sell_price': '',
                    'status': 'pending',
                    'score': '',
                    'listed_at': '',
                    'sold_at': '',
                    'delisted_at': '',
                    'delist_reason': '',
                    'checked_at': '',
                    'offer_id': '',
                    'description': '',
                }
        else:
            # Inventory item exists but no offers at all
            log.info(f'  ORPHAN (no offer): {sku} — {title[:45]}')
            ebay_only_no_offer.append({'isbn': sku, 'title': title})

    save_csv(rows)

    log.info('=' * 60)
    log.info(f'DONE: {len(discovered)} discovered | '
             f'{len(ebay_only_no_offer)} orphaned | '
             f'{already_tracked} already tracked')
    log.info('=' * 60)

    # ── Send email report ────────────────────────────────────────────────
    subject = (f'[discover] {len(discovered)} manual listings found, '
               f'{len(ebay_only_no_offer)} orphaned')

    if HAS_EMAIL_HELPERS:
        html = build_discover_email(discovered, already_tracked, ebay_only_no_offer)
        send_html_email(subject, html)
    else:
        import smtplib
        from email.mime.text import MIMEText
        lines = [
            f'DISCOVER REPORT — {datetime.now():%Y-%m-%d %H:%M}',
            f'Discovered: {len(discovered)} | Orphaned: {len(ebay_only_no_offer)}', '',
        ]
        for d in discovered:
            lines.append(f'  {d["isbn"]} — {d["title"][:45]} — ${d["price"]:.2f}')
        smtp_user = os.environ.get('SMTP_USER', '')
        smtp_pass = os.environ.get('SMTP_PASSWORD', '')
        email_to = os.environ.get('EMAIL_TO', '')
        if all([smtp_user, smtp_pass, email_to]):
            msg = MIMEText('\n'.join(lines))
            msg['Subject'] = subject
            msg['From'] = os.environ.get('EMAIL_FROM', smtp_user)
            msg['To'] = email_to
            try:
                with smtplib.SMTP(os.environ.get('SMTP_HOST', 'smtp.gmail.com'),
                                  int(os.environ.get('SMTP_PORT', '587'))) as s:
                    s.starttls()
                    s.login(smtp_user, smtp_pass)
                    s.sendmail(msg['From'], [email_to], msg.as_string())
                log.info('Report email sent (plain text)')
            except Exception as e:
                log.error(f'Email failed: {e}')


if __name__ == '__main__':
    run()
