#!/usr/bin/env python3
"""
audit_listings.py — eBay listing health check.

Checks all 'active' ISBNs in booksgoat_enhanced.csv against eBay's
Inventory API to detect listings that have ended, expired, or are
in a broken state. Resets dead listings to status=pending so
full_publish.py can relist them on the next run.

Detects:
  - Offers that no longer exist (deleted or expired)
  - Offers in non-PUBLISHED state
  - Inventory items that have been removed
  - Listings ended by eBay (qty exhausted, policy violation, GTC expiry)

Schedule: GitHub Actions audit_listings.yml (weekly or manual)
          or locally: python audit_listings.py

v1.0 — Initial build
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
LOG_FILE = 'audit_listings.log'

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


# ── Check offer status ───────────────────────────────────────────────────────
def check_offer_status(isbn: str, offer_id: str, token: str) -> dict:
    """
    Check if an offer is still live on eBay.
    Returns dict with 'alive' bool and 'reason' string.
    """
    hdrs = {'Authorization': f'Bearer {token}'}

    # Strategy 1: Check offer directly if we have an offer_id
    if offer_id:
        try:
            r = requests.get(
                f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
                headers=hdrs, timeout=10
            )
            if r.status_code == 200:
                offer = r.json()
                status = offer.get('status', '')
                listing_id = offer.get('listing', {}).get('listingId', '')

                if status == 'PUBLISHED':
                    return {'alive': True, 'reason': f'PUBLISHED (listing {listing_id})'}
                else:
                    return {'alive': False, 'reason': f'Offer status: {status}'}

            elif r.status_code == 404:
                return {'alive': False, 'reason': 'Offer not found (404)'}
            else:
                return {'alive': False, 'reason': f'Offer check failed ({r.status_code})'}
        except Exception as e:
            return {'alive': False, 'reason': f'Offer check error: {e}'}

    # Strategy 2: No offer_id — check if any offer exists for this SKU
    try:
        r = requests.get(
            'https://api.ebay.com/sell/inventory/v1/offer',
            headers=hdrs, params={'sku': isbn}, timeout=10
        )
        if r.status_code == 200:
            offers = r.json().get('offers', [])
            if not offers:
                return {'alive': False, 'reason': 'No offers for SKU'}

            for offer in offers:
                if offer.get('status') == 'PUBLISHED':
                    return {'alive': True, 'reason': f'PUBLISHED (offer {offer.get("offerId", "?")})'}

            # All offers exist but none are PUBLISHED
            statuses = [o.get('status', '?') for o in offers]
            return {'alive': False, 'reason': f'Offers not published: {", ".join(statuses)}'}

        elif r.status_code == 404:
            return {'alive': False, 'reason': 'No inventory item for SKU'}
        else:
            return {'alive': False, 'reason': f'SKU check failed ({r.status_code})'}
    except Exception as e:
        return {'alive': False, 'reason': f'SKU check error: {e}'}


# ── Build HTML email ─────────────────────────────────────────────────────────
def build_audit_email(alive_count, dead_listings, already_ok):
    """Build HTML report for audit results."""
    summary = _summary_bar([
        ("Healthy", str(alive_count), "green"),
        ("Dead/Ended", str(len(dead_listings)), "red" if dead_listings else "green"),
        ("Reset to Pending", str(len(dead_listings)), "orange" if dead_listings else "gray"),
    ])

    parts = []

    if dead_listings:
        rows = ""
        for i, d in enumerate(dead_listings):
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{d["isbn"]}</code>',
                d['title'][:40],
                f'<span style="font-size:11px;color:#c62828;">{d["reason"]}</span>',
                _badge("reset to pending", "orange"),
            ], i)
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#c62828;">'
            f'Dead Listings Reset to Pending ({len(dead_listings)})</h3>'
            f'<p style="font-size:12px;color:#666;margin:0 0 8px;">'
            f'These will be relisted on the next full_publish.py run.</p>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
            f'{_table_header(["#", "ISBN", "Title", "Reason", "Action"])}'
            f'{rows}</table>'
        )
    else:
        parts.append(
            '<p style="font-size:14px;color:#2e7d32;">All active listings are healthy. '
            'No action needed.</p>'
        )

    footer = ("Run full_publish.py to relist the pending books shown above."
              if dead_listings else "")

    return _email_wrapper("LISTING HEALTH AUDIT", summary, "\n".join(parts), footer)


# ── Main ─────────────────────────────────────────────────────────────────────
def run():
    log.info('=' * 60)
    log.info(f'AUDIT_LISTINGS STARTED — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info('=' * 60)

    rows = load_csv()
    if not rows:
        log.error('CSV empty or not found')
        return

    # Get all active ISBNs
    active = [(isbn, row) for isbn, row in rows.items() if row.get('status') == 'active']
    log.info(f'Checking {len(active)} active listings against eBay...')

    token = get_user_token()

    alive_count = 0
    dead_listings = []

    for i, (isbn, row) in enumerate(active):
        offer_id = row.get('offer_id', '').strip()
        title = row.get('title', isbn)

        result = check_offer_status(isbn, offer_id, token)

        if result['alive']:
            alive_count += 1
        else:
            log.warning(f'  DEAD: {isbn} — {title[:40]} — {result["reason"]}')
            dead_listings.append({
                'isbn': isbn,
                'title': title,
                'reason': result['reason'],
                'offer_id': offer_id,
            })

            # Reset to pending for relisting
            row['status'] = 'pending'
            row['offer_id'] = ''
            row['delisted_at'] = datetime.now(timezone.utc).isoformat()
            row['delist_reason'] = 'audit_ended'

        # Token refresh every 50
        if (i + 1) % 50 == 0:
            token = get_user_token()
            log.info(f'  Checked {i+1}/{len(active)} — token refreshed')

        time.sleep(0.15)

    save_csv(rows)

    log.info('=' * 60)
    log.info(f'AUDIT DONE: {alive_count} healthy | {len(dead_listings)} dead (reset to pending)')
    log.info('=' * 60)

    # ── Send email report ────────────────────────────────────────────────
    subject = f'[audit] {alive_count} healthy, {len(dead_listings)} dead listings reset'

    if HAS_EMAIL_HELPERS and dead_listings:
        html = build_audit_email(alive_count, dead_listings, len(active))
        send_html_email(subject, html)
    elif HAS_EMAIL_HELPERS and not dead_listings:
        html = build_audit_email(alive_count, dead_listings, len(active))
        send_html_email(subject, html)
    else:
        # Fallback plain text
        if dead_listings:
            import smtplib
            from email.mime.text import MIMEText
            lines = [f'AUDIT REPORT — {datetime.now():%Y-%m-%d %H:%M}',
                     f'Healthy: {alive_count} | Dead: {len(dead_listings)}', '']
            for d in dead_listings:
                lines.append(f'  {d["isbn"]} — {d["title"][:45]} — {d["reason"]}')
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
