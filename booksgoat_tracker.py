#!/usr/bin/env python3
"""
booksgoat_tracker.py — Daily OOS + price change tracker
Location : E:\\Book\\Tracker\\booksgoat_tracker.py
Schedule : Windows Task Scheduler, daily

For each active listing:
  1. Visit BooksGoat product_url via Playwright
  2. Detect OOS: page title = "Product not found" OR no Add to Cart button
  3. Detect price change: scrape current price, compare to CSV cost
  4. If unprofitable after price change: delist
  5. If OOS: delist
  6. Call eBay end_listing API and update CSV
"""

import csv
import logging
import re
import time
import os
import base64
import requests
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════
EBAY_APP_ID        = os.environ["EBAY_APP_ID"]
EBAY_CERT_ID       = os.environ["EBAY_CERT_ID"]
EBAY_REFRESH_TOKEN = os.environ["EBAY_REFRESH_TOKEN"]

EBAY_FEE_RATE = 0.153
MIN_PROFIT    = 12.00

CSV_PATH = Path(r"E:\Book\Lister\booksgoat_enhanced.csv")
LOG_PATH = Path(r"E:\Book\Tracker\tracker.log")

# ════════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════════
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)
import smtplib
from email.mime.text import MIMEText

def send_alert(subject: str, body: str):
    smtp_host     = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    smtp_port     = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user     = os.environ.get('SMTP_USER', '')
    smtp_password = os.environ.get('SMTP_PASSWORD', '')
    email_from    = os.environ.get('EMAIL_FROM', smtp_user)
    email_to      = os.environ.get('EMAIL_TO', smtp_user)
    if not smtp_user or not smtp_password:
        return
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From']    = email_from
        msg['To']      = email_to
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_password)
            s.sendmail(email_from, [email_to], msg.as_string())
    except Exception as e:
        pass



# ════════════════════════════════════════════════════════════════
# EBAY AUTH
# ════════════════════════════════════════════════════════════════
def get_user_token() -> str:
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=(
            "grant_type=refresh_token"
            f"&refresh_token={EBAY_REFRESH_TOKEN}"
            "&scope=https://api.ebay.com/oauth/api_scope/sell.inventory"
            " https://api.ebay.com/oauth/api_scope/sell.account"
        ),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def end_listing(offer_id: str, token: str) -> bool:
    r = requests.delete(
        f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    return r.status_code in (200, 204)


# ════════════════════════════════════════════════════════════════
# CSV HELPERS
# ════════════════════════════════════════════════════════════════
def load_csv() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    return rows


def save_csv(rows: list[dict]):
    if not rows:
        return
    all_fields = list(dict.fromkeys(k for r in rows for k in r))
    tmp = CSV_PATH.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(CSV_PATH)


# ════════════════════════════════════════════════════════════════
# PAGE CHECKS
# ════════════════════════════════════════════════════════════════
def check_page(page, url: str) -> dict:
    """
    Visit product URL and return:
      - is_oos: bool
      - current_price: float | None
      - page_title: str
    """
    result = {"is_oos": False, "current_price": None, "page_title": ""}

    try:
        page.goto(url, timeout=25000, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
    except PWTimeout:
        log.warning(f"  Timeout visiting {url[:60]}")
        return result

    try:
        data = page.evaluate("""
            () => {
                const title = document.title || '';

                // OOS check 1: title contains "not found"
                const titleOos = title.toLowerCase().includes('not found');

                // OOS check 2: no Add to Cart button
                const cartBtn = document.querySelector('#button-cart, button.button-cart, [id*=button-cart]');
                const noCart = !cartBtn;

                // Price: look for .price-new first, then .price
                let priceText = '';
                const priceNew = document.querySelector('.price-new');
                if (priceNew) {
                    priceText = priceNew.textContent.trim();
                } else {
                    const priceEl = document.querySelector('.price');
                    if (priceEl) priceText = priceEl.textContent.trim();
                }

                return {title, titleOos, noCart, priceText};
            }
        """)

        result["page_title"] = data.get("title", "")
        result["is_oos"] = data.get("titleOos", False) or data.get("noCart", False)

        # Parse price
        price_text = data.get("priceText", "")
        m = re.search(r'\$([\d,]+\.?\d*)', price_text)
        if m:
            try:
                result["current_price"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

    except Exception as e:
        log.warning(f"  Page evaluate error: {e}")

    return result


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════
def run():
    log.info("=" * 70)
    log.info(f"Tracker started | {datetime.now().isoformat()}")

    token = get_user_token()
    log.info("eBay token acquired")

    rows = load_csv()
    active_rows = [r for r in rows if r.get("status") == "active"]
    log.info(f"Active books to check: {len(active_rows)}")

    now_iso = datetime.now().isoformat()

    stats = {
        "checked": 0,
        "oos_delisted": 0,
        "price_change_delisted": 0,
        "price_changed_kept": 0,
        "errors": 0,
    }

    # Refresh token every 50 books
    token_counter = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # Build row index for fast updates
        row_index = {r.get("isbn13", ""): r for r in rows}

        for i, row in enumerate(active_rows):
            isbn      = row.get("isbn13", "")
            title     = row.get("title", "")[:50]
            offer_id  = row.get("offer_id", "")
            url       = row.get("product_url", "")
            sell_price = float(row.get("sell_price", 0) or 0)
            old_cost  = float(row.get("cost", 0) or 0)

            log.info(f"[{i+1}/{len(active_rows)}] {isbn} | {title}")

            # Refresh token every 50 books
            token_counter += 1
            if token_counter % 50 == 0:
                log.info("  Refreshing OAuth token...")
                token = get_user_token()

            if not url:
                log.warning(f"  No product_url — skipping")
                stats["errors"] += 1
                continue

            if not offer_id:
                log.warning(f"  No offer_id — cannot delist if needed, skipping")
                stats["errors"] += 1
                continue

            # Check the page
            result = check_page(page, url)
            stats["checked"] += 1

            # Update checked_at
            row_index[isbn]["checked_at"] = now_iso

            # ── OOS check ────────────────────────────────────────
            if result["is_oos"]:
                log.info(f"  OOS DETECTED (title='{result['page_title'][:40]}') — delisting")
                ok = end_listing(offer_id, token)
                if ok:
                    row_index[isbn]["status"]       = "delisted"
                    row_index[isbn]["delisted_at"]  = now_iso
                    row_index[isbn]["delist_reason"] = "unavailable"
                    stats["oos_delisted"] += 1
                    log.info(f"  Delisted OK")
                else:
                    log.warning(f"  Delist API call failed for {offer_id}")
                    stats["errors"] += 1
                time.sleep(0.5)
                continue

            # ── Price change check ────────────────────────────────
            current_price = result["current_price"]
            if current_price and current_price != old_cost:
                change_pct = abs(current_price - old_cost) / old_cost if old_cost > 0 else 1.0
                log.info(f"  Price change: ${old_cost} → ${current_price} ({change_pct:.1%})")

                # Recalculate profit at new cost
                net_profit = round(sell_price * (1 - EBAY_FEE_RATE) - current_price, 2)
                log.info(f"  New profit at current cost: ${net_profit:.2f} (min=${MIN_PROFIT})")

                if net_profit < MIN_PROFIT:
                    log.info(f"  UNPROFITABLE — delisting")
                    ok = end_listing(offer_id, token)
                    if ok:
                        row_index[isbn]["status"]        = "delisted"
                        row_index[isbn]["delisted_at"]   = now_iso
                        row_index[isbn]["delist_reason"] = "unprofitable"
                        row_index[isbn]["cost"]          = str(round(current_price, 2))
                        stats["price_change_delisted"] += 1
                        log.info(f"  Delisted OK")
                    else:
                        log.warning(f"  Delist API call failed for {offer_id}")
                        stats["errors"] += 1
                else:
                    # Still profitable — update cost in CSV, keep listed
                    row_index[isbn]["cost"] = str(round(current_price, 2))
                    stats["price_changed_kept"] += 1
                    log.info(f"  Still profitable — updated cost in CSV, keeping listed")

            time.sleep(0.4)

        page.close()
        context.close()
        browser.close()

    # Save all updates
    save_csv(list(row_index.values()))

    log.info("-" * 70)
    log.info(
        f"Done: {stats['checked']} checked | "
        f"{stats['oos_delisted']} OOS delisted | "
        f"{stats['price_change_delisted']} unprofitable delisted | "
        f"{stats['price_changed_kept']} price updated (kept) | "
        f"{stats['errors']} errors"
    )
    log.info("=" * 70)


if __name__ == "__main__":
    run()
