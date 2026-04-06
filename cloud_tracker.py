"""
cloud_tracker.py
─────────────────────────────────────────────────────────────────
Daily tracker for GitHub Actions.

Responsibilities:
  1. Load active_listings from lister_state.json (unified registry)
  2. Scrape BooksGoat for each active ISBN
  3. Detect: OOS | price spike | profit gone negative
  4. Auto-delist via eBay Inventory API immediately
  5. Update lister_state.json with new prices / statuses
  6. Email a daily summary

State persistence:
  lister_state.json is committed back to GitHub by tracker.yml
  after this script exits.  No SQLite required.

Covers BOTH auto-listed items (source=auto) and manually listed
items (source=manual) as long as they have an offer_id in the
lister_state.json registry.  Run migrate_tracker_input.py once
to import existing manual listings.
─────────────────────────────────────────────────────────────────
"""

import json
import os
import re
import sys
import time
import smtplib
import traceback
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Optional BeautifulSoup ─────────────────────────────────
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# ── Config from environment variables ─────────────────────
LISTER_STATE_PATH = os.getenv("LISTER_STATE_PATH", "lister_state.json")
EBAY_FEE_RATE     = float(os.getenv("EBAY_FEE_RATE", "0.1325"))
MIN_PROFIT_FLOOR  = float(os.getenv("MIN_PROFIT_FLOOR", "1.00"))

# Price spike threshold — delist if supplier cost rises by this % or more
PRICE_SPIKE_PCT   = float(os.getenv("PRICE_SPIKE_PCT", "20.0"))

# Consecutive scrape failures before precautionary delist
MAX_SCRAPE_FAILS  = int(os.getenv("MAX_SCRAPE_FAILS", "3"))

# Email
SMTP_HOST    = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER", "")
SMTP_PASS    = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM   = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO     = os.getenv("EMAIL_TO", "")

# eBay API
EBAY_BASE    = "https://api.ebay.com/sell/inventory/v1"

# Request headers for BooksGoat scraping
SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ══════════════════════════════════════════════════════════
# SECTION 1 — State management
# ══════════════════════════════════════════════════════════

def load_state() -> dict:
    if not os.path.exists(LISTER_STATE_PATH):
        print(f"[WARN] {LISTER_STATE_PATH} not found — nothing to track.")
        return {"listings": {}, "listed_isbns": [], "last_run": None}
    with open(LISTER_STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("listings", {})
    state.setdefault("listed_isbns", [])
    return state


def save_state(state: dict):
    state["tracker_last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(LISTER_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_active_listings(state: dict) -> list[dict]:
    """Returns list of (isbn, entry) dicts that are ACTIVE and have a BooksGoat URL."""
    active = []
    for isbn, entry in state.get("listings", {}).items():
        if entry.get("status") != "ACTIVE":
            continue
        url = entry.get("booksgoat_url", "").strip()
        if not url:
            print(f"  [SKIP] {isbn} — no booksgoat_url in state.")
            continue
        active.append({"isbn": isbn, **entry})
    return active


# ══════════════════════════════════════════════════════════
# SECTION 2 — BooksGoat scraper
# ══════════════════════════════════════════════════════════

def scrape_booksgoat(url: str) -> dict:
    """
    Scrape a BooksGoat product page.
    Returns: {"price": float, "available": bool}
    Raises:  RuntimeError on failure (caller counts these)
    """
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Request failed: {e}")

    if resp.status_code == 404:
        # Page gone — treat as OOS
        return {"price": None, "available": False}

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")

    html = resp.text

    # ── Availability detection ──────────────────────────────
    html_lower = html.lower()
    out_of_stock_signals = [
        "out of stock", "out-of-stock", "outofstock",
        "currently unavailable", "not available",
        "sold out", "soldout",
    ]
    in_stock_signals = [
        "add to cart", "add-to-cart", "addtocart",
        "in stock", "instock",
        "buy now",
    ]
    available = None
    for sig in out_of_stock_signals:
        if sig in html_lower:
            available = False
            break
    if available is None:
        for sig in in_stock_signals:
            if sig in html_lower:
                available = True
                break
    # If neither signal found, assume available (conservative — don't delist on ambiguity)
    if available is None:
        available = True

    # ── Price extraction ────────────────────────────────────
    price = None

    if BS4_AVAILABLE:
        soup = BeautifulSoup(html, "html.parser")
        # Common price selectors
        selectors = [
            ".price",
            "#product-price",
            ".product-price",
            ".price-new",
            '[class*="price"]',
            '[id*="price"]',
        ]
        for sel in selectors:
            els = soup.select(sel)
            for el in els:
                text = el.get_text(strip=True)
                m = re.search(r"\$\s*([\d,]+\.?\d*)", text)
                if m:
                    try:
                        price = float(m.group(1).replace(",", ""))
                        break
                    except ValueError:
                        pass
            if price:
                break

    # Regex fallback — find dollar amounts in HTML
    if price is None:
        matches = re.findall(r"\$\s*([\d,]+\.\d{2})", html)
        prices  = []
        for m in matches:
            try:
                val = float(m.replace(",", ""))
                if 1.0 < val < 2000.0:  # Sanity range for books
                    prices.append(val)
            except ValueError:
                pass
        if prices:
            # Use most common value (or median) as the likely product price
            from collections import Counter
            most_common = Counter(prices).most_common(1)
            price = most_common[0][0] if most_common else sorted(prices)[len(prices)//2]

    return {"price": price, "available": available}


# ══════════════════════════════════════════════════════════
# SECTION 3 — Risk assessment
# ══════════════════════════════════════════════════════════

def assess_risk(isbn: str, entry: dict, scraped: dict) -> dict | None:
    """
    Returns a risk dict if the listing should be delisted, else None.
    Risk dict: {"reason": str, "detail": str}
    """
    new_price   = scraped.get("price")
    available   = scraped.get("available", True)
    ebay_price  = float(entry.get("ebay_price", 0))
    orig_cost   = float(entry.get("cost", 0))
    last_cost   = float(entry.get("last_supplier_price") or orig_cost)

    # ── Rule 1: Out of stock ───────────────────────────────
    if not available:
        return {
            "reason": "OUT_OF_STOCK",
            "detail": "BooksGoat reports item unavailable"
        }

    if new_price is None:
        # Can't read price — not a delist trigger by itself
        return None

    # ── Rule 2: Price spike vs original cost ──────────────
    if orig_cost > 0:
        spike_pct = ((new_price - orig_cost) / orig_cost) * 100
        if spike_pct >= PRICE_SPIKE_PCT:
            return {
                "reason": "PRICE_SPIKE",
                "detail": (
                    f"Cost rose from ${orig_cost:.2f} to ${new_price:.2f} "
                    f"(+{spike_pct:.1f}% >= {PRICE_SPIKE_PCT}% threshold)"
                )
            }

    # ── Rule 3: Profit gone below floor ───────────────────
    if ebay_price > 0 and new_price > 0:
        new_profit = ebay_price * (1 - EBAY_FEE_RATE) - new_price
        if new_profit < MIN_PROFIT_FLOOR:
            return {
                "reason": "PROFIT_NEGATIVE",
                "detail": (
                    f"New cost ${new_price:.2f} leaves only ${new_profit:.2f} profit "
                    f"(floor is ${MIN_PROFIT_FLOOR:.2f})"
                )
            }

    return None


# ══════════════════════════════════════════════════════════
# SECTION 4 — eBay delist
# ══════════════════════════════════════════════════════════

def get_ebay_token() -> str:
    """Get eBay access token via refresh token flow."""
    import base64
    client_id     = os.getenv("EBAY_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
    refresh_token = os.getenv("EBAY_REFRESH_TOKEN", "")

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError(
            "Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET / EBAY_REFRESH_TOKEN env vars"
        )

    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    scopes = " ".join([
        "https://api.ebay.com/oauth/api_scope",
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
        "https://api.ebay.com/oauth/api_scope/sell.account",
    ])
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "scope":         scopes,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


_ebay_token_cache = {"token": None, "expires": 0}

def ebay_headers() -> dict:
    if not _ebay_token_cache["token"] or time.time() > _ebay_token_cache["expires"] - 300:
        _ebay_token_cache["token"]   = get_ebay_token()
        _ebay_token_cache["expires"] = time.time() + 7200
    return {
        "Authorization":            f"Bearer {_ebay_token_cache['token']}",
        "Content-Type":             "application/json",
        "Content-Language":         "en-US",
        "X-EBAY-C-MARKETPLACE-ID":  "EBAY_US",
    }


def withdraw_offer(offer_id: str) -> bool:
    """Withdraw (end) an eBay listing by offer ID. Returns True on success."""
    url  = f"{EBAY_BASE}/offer/{offer_id}/withdraw"
    resp = requests.post(url, headers=ebay_headers(), timeout=15)
    return resp.status_code in (200, 204)


# ══════════════════════════════════════════════════════════
# SECTION 5 — Email summary
# ══════════════════════════════════════════════════════════

def send_email(subject: str, body: str):
    if not all([SMTP_USER, SMTP_PASS, EMAIL_TO]):
        print("[WARN] Email not configured — skipping alert.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO.split(","), msg.as_string())
        print(f"[EMAIL] Alert sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[WARN] Email failed: {e}")


def build_summary(
    checked: int,
    delisted: list,
    price_changes: list,
    skipped: list,
    errors: list,
    run_ts: str,
) -> str:
    lines = [
        f"BooksGoat Tracker — Daily Summary",
        f"Run: {run_ts} UTC",
        f"{'='*60}",
        f"Active listings checked : {checked}",
        f"Delisted this run       : {len(delisted)}",
        f"Price changes noted     : {len(price_changes)}",
        f"Skipped (no URL)        : {len(skipped)}",
        f"Scrape errors           : {len(errors)}",
        "",
    ]

    if delisted:
        lines.append("── DELISTED ─────────────────────────────")
        for d in delisted:
            lines.append(
                f"  [{d['reason']}] {d['title'][:50]}\n"
                f"    ISBN: {d['isbn']} | {d['detail']}"
            )
        lines.append("")

    if price_changes:
        lines.append("── PRICE CHANGES (not delisted) ─────────")
        for p in price_changes:
            lines.append(
                f"  {p['title'][:50]}\n"
                f"    ISBN: {p['isbn']} | {p['old']:.2f} → {p['new']:.2f}"
            )
        lines.append("")

    if errors:
        lines.append("── SCRAPE ERRORS ────────────────────────")
        for e in errors:
            lines.append(f"  {e['isbn']}: {e['error']}")
        lines.append("")

    lines.append("── All active listings ──────────────────")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# SECTION 6 — Main
# ══════════════════════════════════════════════════════════

def main():
    run_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"BooksGoat Cloud Tracker — {run_ts} UTC")
    print(f"{'='*60}\n")

    state   = load_state()
    active  = get_active_listings(state)

    print(f"Active listings to check: {len(active)}\n")

    if not active:
        print("Nothing to track. Add listings to lister_state.json.")
        send_email(
            f"[Tracker] No active listings — {run_ts[:10]}",
            "No active listings found in lister_state.json."
        )
        return

    delisted      = []
    price_changes = []
    skipped       = []
    errors        = []
    checked       = 0

    for entry in active:
        isbn  = entry["isbn"]
        title = entry.get("title", "Unknown")[:50]
        url   = entry.get("booksgoat_url", "")

        print(f"[{isbn}] {title[:40]}...")

        if not url:
            print(f"  SKIP — no BooksGoat URL")
            skipped.append(isbn)
            continue

        # ── Scrape ──────────────────────────────────────────
        try:
            scraped = scrape_booksgoat(url)
            checked += 1
            state["listings"][isbn]["scrape_failures"] = 0

            new_price = scraped.get("price")
            old_price = float(entry.get("last_supplier_price") or entry.get("cost") or 0)

            print(
                f"  Price: ${new_price:.2f}" if new_price else "  Price: unknown",
                f"| Available: {scraped['available']}"
            )

            # Track price changes for reporting
            if new_price and old_price and abs(new_price - old_price) >= 0.50:
                price_changes.append({
                    "isbn":  isbn,
                    "title": title,
                    "old":   old_price,
                    "new":   new_price,
                })

            # Update tracker state
            if new_price:
                state["listings"][isbn]["last_supplier_price"] = new_price
            state["listings"][isbn]["last_supplier_available"] = scraped["available"]
            state["listings"][isbn]["last_checked"]            = run_ts

        except RuntimeError as e:
            fail_count = int(entry.get("scrape_failures", 0)) + 1
            state["listings"][isbn]["scrape_failures"] = fail_count
            state["listings"][isbn]["last_checked"]    = run_ts
            print(f"  SCRAPE ERROR ({fail_count}/{MAX_SCRAPE_FAILS}): {e}")
            errors.append({"isbn": isbn, "error": str(e)})

            if fail_count >= MAX_SCRAPE_FAILS:
                scraped = {"price": None, "available": False}
                print(f"  Treating as OOS after {fail_count} consecutive failures.")
            else:
                time.sleep(1)
                continue

        # ── Risk assessment ──────────────────────────────────
        risk = assess_risk(isbn, entry, scraped)

        if risk:
            offer_id = entry.get("offer_id", "")
            print(f"  FLAGGED [{risk['reason']}]: {risk['detail']}")

            delist_success = False
            if offer_id:
                try:
                    delist_success = withdraw_offer(offer_id)
                    if delist_success:
                        print(f"  DELISTED from eBay (offer {offer_id})")
                        state["listings"][isbn]["status"]        = "DELISTED"
                        state["listings"][isbn]["delisted_at"]   = run_ts
                        state["listings"][isbn]["delist_reason"] = risk["reason"]
                    else:
                        print(f"  Delist API call FAILED for offer {offer_id}")
                except Exception as ex:
                    print(f"  Delist exception: {ex}")
            else:
                print(f"  No offer_id — cannot auto-delist. Manual action required.")
                state["listings"][isbn]["status"] = "NEEDS_MANUAL_DELIST"

            delisted.append({
                "isbn":        isbn,
                "title":       title,
                "reason":      risk["reason"],
                "detail":      risk["detail"],
                "offer_id":    offer_id,
                "auto_delisted": delist_success,
            })

        time.sleep(1.5)  # Polite scraping delay

    # ── Save updated state ───────────────────────────────────
    save_state(state)
    print(f"\nState saved to {LISTER_STATE_PATH}")

    # ── Build and send summary ───────────────────────────────
    summary = build_summary(checked, delisted, price_changes, skipped, errors, run_ts)
    print(f"\n{summary}")

    subject = (
        f"[Tracker] {run_ts[:10]} — "
        f"{len(delisted)} delisted | {checked} checked"
    )
    send_email(subject, summary)

    print(f"\n{'='*60}")
    print(f"Done. {checked} checked | {len(delisted)} delisted | {len(errors)} errors")
    print(f"{'='*60}\n")

    # Exit with error code if any auto-delist failed (for CI awareness)
    failed_delistings = [d for d in delisted if not d.get("auto_delisted") and d.get("offer_id")]
    if failed_delistings:
        sys.exit(1)


if __name__ == "__main__":
    main()
