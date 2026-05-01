#!/usr/bin/env python3
"""
fix_images.py — Re-scan active listings for missing or thumbnail images.
Finds active listings in booksgoat_enhanced.csv that have:
  - image_flag='thumbnail' (listed with low-res image, needs upgrade)
  - No image on eBay (previously failed, now may have coverage)

For each, runs the full image chain. If a full-quality image is found,
updates the eBay inventory item via PUT.

Run: GitHub Actions fix_images.yml (manual trigger)
     or locally: python fix_images.py
"""

import os, csv, json, base64, time, logging, requests, re, smtplib
from datetime import datetime, timezone
from pathlib import Path
from email.mime.text import MIMEText

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID') or os.getenv('EBAY_APP_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET') or os.getenv('EBAY_CERT_ID')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
SMTP_HOST          = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT          = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER          = os.getenv('SMTP_USER', '')
SMTP_PASSWORD      = os.getenv('SMTP_PASSWORD', '')
EMAIL_FROM         = os.getenv('EMAIL_FROM', SMTP_USER)
EMAIL_TO           = os.getenv('EMAIL_TO', SMTP_USER)

CSV_PATH = Path('booksgoat_enhanced.csv')
LOG_FILE = 'fix_images_log.txt'

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
        }
    )
    return r.json()['access_token']


# ── ISBN-10 conversion ────────────────────────────────────────────────────
def isbn13_to_isbn10(isbn13: str) -> str:
    if not isbn13 or not isbn13.startswith('978') or len(isbn13) != 13:
        return ''
    body = isbn13[3:12]
    try:
        total = sum(int(d) * (10 - i) for i, d in enumerate(body))
        check = (11 - (total % 11)) % 11
        return body + ('X' if check == 10 else str(check))
    except (ValueError, IndexError):
        return ''


# ── Image validation ──────────────────────────────────────────────────────
def is_real_image(url: str, min_bytes: int = 5000) -> bool:
    try:
        r = requests.get(url, timeout=12, stream=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return False
        chunk = next(r.iter_content(chunk_size=min_bytes + 1), b"")
        r.close()
        is_jpg = chunk[:2] == b"\xff\xd8"
        is_png = chunk[:4] == b"\x89PNG"
        return (is_jpg or is_png) and len(chunk) >= min_bytes
    except Exception:
        return False


# ── Image search (full quality only — we're upgrading, not downgrading) ───
def find_full_image(isbn13: str, isbn10: str, title: str) -> str | None:
    """Search all sources for a full-quality image (3000+ bytes)."""

    # 1. Open Library Covers
    for isbn in [isbn13, isbn10]:
        if not isbn:
            continue
        for size in ["L", "M"]:
            url = f"https://covers.openlibrary.org/b/isbn/{isbn}-{size}.jpg"
            if is_real_image(url, min_bytes=3000):
                return url

    # 2. Open Library Works API
    try:
        r = requests.get(
            "https://openlibrary.org/api/books",
            params={"bibkeys": f"ISBN:{isbn13}", "format": "json", "jscmd": "data"},
            timeout=8)
        data = r.json().get(f"ISBN:{isbn13}", {})
        for key in ["large", "medium"]:
            cover_url = data.get("cover", {}).get(key)
            if cover_url and is_real_image(cover_url, min_bytes=3000):
                return cover_url
    except Exception:
        pass

    # 3. Amazon CDN
    patterns = [
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn13}.01.LZZZZZZZ.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn13}.01._SX500_.jpg",
        f"https://m.media-amazon.com/images/I/{isbn13}.jpg",
    ]
    if isbn10:
        patterns += [
            f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.LZZZZZZZ.jpg",
            f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01._SX500_.jpg",
        ]
    for url in patterns:
        if is_real_image(url, min_bytes=5000):
            return url

    # 4. Google Books ISBN search
    try:
        r = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": f"isbn:{isbn13}", "maxResults": 3}, timeout=8)
        for item in r.json().get("items", []):
            img = item.get("volumeInfo", {}).get("imageLinks", {})
            for key, zoom in [("extraLarge", 3), ("large", 3), ("medium", 2)]:
                src = img.get(key)
                if not src:
                    continue
                src = src.replace("http://", "https://")
                src = re.sub(r"zoom=\d", f"zoom={zoom}", src)
                if is_real_image(src, min_bytes=3000):
                    return src
    except Exception:
        pass

    # 5. Open Library cover ID
    try:
        r = requests.get(
            "https://openlibrary.org/search.json",
            params={"isbn": isbn13, "fields": "cover_i", "limit": 1}, timeout=8)
        docs = r.json().get("docs", [])
        if docs and docs[0].get("cover_i"):
            url = f"https://covers.openlibrary.org/b/id/{docs[0]['cover_i']}-L.jpg"
            if is_real_image(url, min_bytes=3000):
                return url
    except Exception:
        pass

    # 6. Google Books title search
    if title:
        try:
            clean = re.sub(r'[^\w\s]', ' ', title).strip()[:80]
            r = requests.get(
                "https://www.googleapis.com/books/v1/volumes",
                params={"q": f"intitle:{clean}", "maxResults": 5}, timeout=8)
            for item in r.json().get("items", []):
                img = item.get("volumeInfo", {}).get("imageLinks", {})
                for key, zoom in [("extraLarge", 3), ("large", 3), ("medium", 2)]:
                    src = img.get(key)
                    if not src:
                        continue
                    src = src.replace("http://", "https://")
                    src = re.sub(r"zoom=\d", f"zoom={zoom}", src)
                    if is_real_image(src, min_bytes=3000):
                        return src
        except Exception:
            pass

    return None


# ── eBay inventory update ─────────────────────────────────────────────────
def update_inventory_image(isbn: str, image_url: str, token: str) -> bool:
    """
    Fetch existing inventory item, add/replace image, PUT back.
    Preserves all existing fields (title, description, aspects, etc).
    """
    hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
            'Content-Language': 'en-US'}

    # GET existing item
    r = requests.get(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers=hdrs, timeout=15)
    if r.status_code != 200:
        log.warning(f'  GET inventory item failed {r.status_code}')
        return False

    item = r.json()
    # Update image
    if 'product' not in item:
        item['product'] = {}
    item['product']['imageUrls'] = [image_url]

    # Strip invalid weight/packageWeightAndSize data that causes error 25709
    for bad_key in ['packageWeightAndSize', 'weight']:
        if bad_key in item:
            w = item[bad_key]
            if isinstance(w, dict):
                val = w.get('value') or w.get('weight', {}).get('value')
                if not val or val == 0:
                    del item[bad_key]

    # PUT back
    r2 = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers=hdrs, json=item, timeout=15)
    if r2.status_code in (200, 204):
        return True
    log.warning(f'  PUT inventory item failed {r2.status_code}: {r2.text[:150]}')
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
    log.info(f'FIX_IMAGES STARTED — {datetime.now():%Y-%m-%d %H:%M:%S}')
    log.info('=' * 60)

    rows = load_csv()
    if not rows:
        log.error('CSV empty or not found')
        return

    token = get_user_token()

    # Find candidates: ALL active listings — check eBay API for missing images
    active = [(isbn, row) for isbn, row in rows.items() if row.get('status') == 'active']
    log.info(f'Checking {len(active)} active listings for missing/thumbnail images...')

    candidates = []
    for i, (isbn, row) in enumerate(active):
        flag = row.get('image_flag', '').strip()

        # If already flagged as thumbnail or missing, include immediately
        if flag in ('thumbnail', 'missing'):
            candidates.append((isbn, row, flag))
            continue

        # If flag is empty, check eBay inventory API for actual image status
        if flag == '':
            try:
                r = requests.get(
                    f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
                    headers={'Authorization': f'Bearer {token}'},
                    timeout=10)
                if r.status_code == 200:
                    item = r.json()
                    images = item.get('product', {}).get('imageUrls', [])
                    if not images:
                        log.info(f'  {isbn} — no image on eBay')
                        row['image_flag'] = 'missing'
                        candidates.append((isbn, row, 'missing'))
                    # else: has image, skip
                elif r.status_code == 404:
                    # No inventory item — skip
                    pass
            except Exception:
                pass

        # Token refresh every 50 checks
        if (i + 1) % 50 == 0:
            token = get_user_token()
            log.info(f'  Checked {i+1}/{len(active)} — token refreshed')

        time.sleep(0.15)  # light rate limiting for GET calls

    log.info(f'Found {len(candidates)} candidates for image upgrade')
    if not candidates:
        log.info('No image upgrades needed')
        save_csv(rows)  # Save any new flags
        return

    upgraded = []
    still_missing = []

    for i, (isbn, row, flag) in enumerate(candidates):
        title = row.get('title', isbn)
        isbn10 = isbn13_to_isbn10(isbn)

        log.info(f'[{i+1}/{len(candidates)}] {isbn} ({flag}) — {title[:40]}')

        image_url = find_full_image(isbn, isbn10, title)
        if not image_url:
            log.info(f'  Still no full-quality image')
            still_missing.append({'isbn': isbn, 'title': title[:50], 'flag': flag})
            continue

        # Update eBay
        ok = update_inventory_image(isbn, image_url, token)
        if ok:
            log.info(f'  UPGRADED — full image applied')
            row['image_flag'] = ''  # Clear the flag
            upgraded.append({'isbn': isbn, 'title': title[:50], 'url': image_url})
        else:
            log.warning(f'  Image found but eBay update failed')
            still_missing.append({'isbn': isbn, 'title': title[:50], 'flag': flag})

        # Token refresh every 30
        if (i + 1) % 30 == 0:
            token = get_user_token()

        time.sleep(0.5)

    save_csv(rows)

    log.info('=' * 60)
    log.info(f'FIX_IMAGES DONE: {len(upgraded)} upgraded | {len(still_missing)} still missing')
    log.info('=' * 60)

    # Email report
    if all([SMTP_USER, SMTP_PASSWORD, EMAIL_TO]) and (upgraded or still_missing):
        lines = [
            f'IMAGE FIX REPORT — {datetime.now():%Y-%m-%d %H:%M}',
            f'Upgraded: {len(upgraded)} | Still missing: {len(still_missing)}',
            '=' * 50, '',
        ]
        if upgraded:
            lines.append('UPGRADED TO FULL IMAGE:')
            for u in upgraded:
                lines.append(f'  {u["isbn"]} — {u["title"]}')
            lines.append('')
        if still_missing:
            lines.append('STILL NEED MANUAL PHOTO:')
            for m in still_missing:
                lines.append(f'  {m["isbn"]} — {m["title"]} (was: {m["flag"]})')
            lines.append('')

        msg = MIMEText('\n'.join(lines))
        msg['Subject'] = f'[fix_images] {len(upgraded)} upgraded, {len(still_missing)} still missing'
        msg['From'] = EMAIL_FROM or SMTP_USER
        msg['To'] = EMAIL_TO
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(msg['From'], [EMAIL_TO], msg.as_string())
            log.info('Report email sent')
        except Exception as e:
            log.error(f'Email failed: {e}')


if __name__ == '__main__':
    run()
