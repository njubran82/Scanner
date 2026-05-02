#!/usr/bin/env python3
"""
fix_descriptions.py — Add AI descriptions to active listings missing them.
Checks all active listings in booksgoat_enhanced.csv for empty descriptions.
Generates via Anthropic API, updates eBay inventory item, saves to CSV.

Run: GitHub Actions fix_descriptions.yml (manual trigger)
     or locally: python fix_descriptions.py

v1.1 — HTML email reports via email_helpers module
"""

import os, csv, base64, time, logging, requests, re
from datetime import datetime, timezone
from pathlib import Path

try:
    from email_helpers import build_fix_descriptions_email, send_html_email
except ImportError:
    build_fix_descriptions_email = None
    send_html_email = None

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID') or os.getenv('EBAY_APP_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET') or os.getenv('EBAY_CERT_ID')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
ANTHROPIC_API_KEY  = os.getenv('ANTHROPIC_API_KEY', '')
SMTP_HOST          = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT          = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER          = os.getenv('SMTP_USER', '')
SMTP_PASSWORD      = os.getenv('SMTP_PASSWORD', '')
EMAIL_FROM         = os.getenv('EMAIL_FROM', SMTP_USER)
EMAIL_TO           = os.getenv('EMAIL_TO', SMTP_USER)

CSV_PATH = Path('booksgoat_enhanced.csv')
LOG_FILE = 'fix_descriptions_log.txt'

CLOSING_STATEMENT = (
    "This item is sourced internationally to offer significant savings. "
    "Tracking information may not update until the package reaches the United States. "
    "All books are brand new, in mint condition, and carefully inspected before shipment."
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── Auth ───────────────────────────────────────────────────────────────────
def get_user_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type':    'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': 'https://api.ebay.com/oauth/api_scope/sell.inventory',
        })
    return r.json()['access_token']


# ── AI description ─────────────────────────────────────────────────────────
def generate_description(title: str, isbn: str) -> str:
    clean = re.sub(r'\s*[-–]\s*(Hardcover|Paperback|Spiral Bound|Loose Leaf)\s*$', '', title, flags=re.I)
    clean = clean.strip(' –-')[:80]
    fmt = 'Paperback'
    if re.search(r'Hardcover', title, re.I): fmt = 'Hardcover'
    elif re.search(r'Spiral', title, re.I): fmt = 'Spiral Bound'

    if not ANTHROPIC_API_KEY:
        return f"Brand new {fmt} copy of {clean} (ISBN {isbn}).\n\n{CLOSING_STATEMENT}"

    try:
        r = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key':         ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type':      'application/json',
            },
            json={
                'model':      'claude-haiku-4-5-20251001',
                'max_tokens': 300,
                'messages': [{
                    'role': 'user',
                    'content': (
                        f"Write a compelling, specific eBay book listing description.\n"
                        f"Book: {clean}\nISBN: {isbn}\nFormat: {fmt}\n\n"
                        f"Rules:\n- 3-4 sentences ONLY\n"
                        f"- Be specific about the subject and who benefits\n"
                        f"- Mention it is a brand-new {fmt}\n"
                        f"- Do NOT start with the book title verbatim\n"
                        f"- Do NOT use vague filler\n"
                        f"- Plain text only, no markdown\n\n"
                        f"Output ONLY the description text."
                    )
                }],
            },
            timeout=15,
        )
        if r.status_code == 200:
            content = r.json().get('content', [])
            if content and content[0].get('type') == 'text':
                body = content[0]['text'].strip()
                return f"{body}\n\n{CLOSING_STATEMENT}"
    except Exception as e:
        log.warning(f'  Claude API error: {e}')

    return f"Brand new {fmt} copy of {clean} (ISBN {isbn}).\n\n{CLOSING_STATEMENT}"


# ── eBay update ────────────────────────────────────────────────────────────
def update_inventory_description(isbn: str, description: str, token: str) -> bool:
    hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
            'Content-Language': 'en-US'}

    r = requests.get(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers=hdrs, timeout=15)
    if r.status_code != 200:
        log.warning(f'  GET inventory item failed {r.status_code}')
        return False

    item = r.json()
    if 'product' not in item:
        item['product'] = {}
    item['product']['description'] = description

    r2 = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers=hdrs, json=item, timeout=15)
    if r2.status_code in (200, 204):
        return True
    log.warning(f'  PUT failed {r2.status_code}: {r2.text[:150]}')
    return False


# ── CSV helpers ────────────────────────────────────────────────────────────
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


# ── Main ───────────────────────────────────────────────────────────────────
def run():
    log.info('=' * 60)
    log.info(f'FIX_DESCRIPTIONS STARTED — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info('=' * 60)

    rows = load_csv()
    if not rows:
        log.error('CSV empty or not found')
        return

    candidates = []
    for isbn, row in rows.items():
        if row.get('status') != 'active':
            continue
        desc = row.get('description', '').strip()
        if not desc:
            candidates.append((isbn, row))

    log.info(f'Found {len(candidates)} active listings without descriptions')
    if not candidates:
        log.info('All active listings have descriptions')
        return

    token = get_user_token()
    updated = []
    failed = []

    for i, (isbn, row) in enumerate(candidates):
        title = row.get('title', isbn)
        log.info(f'[{i+1}/{len(candidates)}] {isbn} — {title[:45]}')

        desc = generate_description(title, isbn)
        ok = update_inventory_description(isbn, desc, token)

        if ok:
            log.info(f'  Description added + eBay updated')
            row['description'] = desc
            updated.append({'isbn': isbn, 'title': title[:50]})
        else:
            log.warning(f'  Description generated but eBay update failed')
            row['description'] = desc
            failed.append({'isbn': isbn, 'title': title[:50]})

        if (i + 1) % 30 == 0:
            token = get_user_token()

        time.sleep(0.5)

    save_csv(rows)

    log.info('=' * 60)
    log.info(f'FIX_DESCRIPTIONS DONE: {len(updated)} updated | {len(failed)} failed')
    log.info('=' * 60)

    # ── Send HTML email report ───────────────────────────────────────────
    if updated or failed:
        subject = f'[fix_descriptions] {len(updated)} added, {len(failed)} failed'
        if send_html_email and build_fix_descriptions_email:
            html = build_fix_descriptions_email(updated, failed)
            send_html_email(subject, html)
        elif all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
            # Fallback: plain text
            import smtplib
            from email.mime.text import MIMEText
            lines = [
                f'DESCRIPTION FIX REPORT — {datetime.now():%Y-%m-%d %H:%M}',
                f'Updated: {len(updated)} | Failed: {len(failed)}',
                '=' * 50, '',
            ]
            if updated:
                lines.append(f'DESCRIPTIONS ADDED ({len(updated)}):')
                for u in updated:
                    lines.append(f'  {u["isbn"]} — {u["title"]}')
                lines.append('')
            if failed:
                lines.append(f'EBAY UPDATE FAILED ({len(failed)}):')
                for f_item in failed:
                    lines.append(f'  {f_item["isbn"]} — {f_item["title"]}')
                lines.append('')
            msg = MIMEText('\n'.join(lines))
            msg['Subject'] = subject
            msg['From'] = EMAIL_FROM or SMTP_USER
            msg['To'] = EMAIL_TO
            try:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                    s.starttls()
                    s.login(SMTP_USER, SMTP_PASSWORD)
                    s.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
                log.info('Report email sent (plain text fallback)')
            except Exception as e:
                log.error(f'Email failed: {e}')


if __name__ == '__main__':
    run()
