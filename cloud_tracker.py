"""
cloud_tracker.py  v2
─────────────────────────────────────────────────────────────────
Daily tracker for GitHub Actions.

SCRAPING STRATEGY — IMPORTANT:
  BooksGoat blocks datacenter IPs (GitHub Actions = AWS).
  Instead of scraping individual product pages, this tracker
  reads the same live Google Sheets CSV that the scanner uses.
  One request fetches all supplier stock in one shot.
  If an ISBN is absent from the sheet → treat as OOS.
  If price has risen significantly → flag for delist.

Responsibilities:
  1. Fetch BooksGoat Google Sheets CSV (all supplier stock)
  2. Cross-reference each active listing against the sheet
  3. Detect: OOS (ISBN missing) | price spike | profit gone
  4. Auto-delist via eBay Inventory API immediately
  5. Update lister_state.json
  6. Email a daily summary

State persistence:
  lister_state.json is committed back to GitHub by tracker.yml
  after this script exits.  No SQLite needed.
─────────────────────────────────────────────────────────────────
"""

import csv
import io
import json
import os
import sys
import time
import smtplib
import base64
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# ══════════════════════════════════════════════════════════
# CONFIG — from environment variables
# ══════════════════════════════════════════════════════════

LISTER_STATE_PATH = os.getenv("LISTER_STATE_PATH", "lister_state.json")

# BooksGoat Google Sheets CSV — same one the scanner uses
BOOKSGOAT_CSV_URL = os.getenv(
    "BOOKSGOAT_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1uXD9-87xzSsU4wV0qw34r3OxmoZ5Gz4hw4vhrFykPfU/export?format=csv"
)

# Thresholds
EBAY_FEE_RATE    = float(os.getenv("EBAY_FEE_RATE",    "0.1325"))
MIN_PROFIT_FLOOR = float(os.getenv("MIN_PROFIT_FLOOR", "1.00"))
PRICE_SPIKE_PCT  = float(os.getenv("PRICE_SPIKE_PCT",  "20.0"))

# Which column holds the price we use (default: 5 Qty)
PRICE_COLUMN = os.getenv("PRICE_COLUMN", "5 Qty")

# Email
SMTP_HOST  = os.getenv("SMTP_HOST",  "smtp.gmail.com")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER",  "")
SMTP_PASS  = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO   = os.getenv("EMAIL_TO",   "")

EBAY_BASE  = "https://api.ebay.com/sell/inventory/v1"


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


def get_active_listings(state: dict) -> list:
    return [
        {"isbn": isbn, **entry}
        for isbn, entry in state.get("listings", {}).items()
        if entry.get("status") == "ACTIVE"
    ]


# ══════════════════════════════════════════════════════════
# SECTION 2 — BooksGoat sheet fetch
# ══════════════════════════════════════════════════════════

def fetch_booksgoat_sheet() -> dict:
    """
    Fetches the live BooksGoat Google Sheets CSV.
    Returns a dict keyed by ISBN-13 → {"price": float, "available": bool}
    """
    print(f"Fetching BooksGoat sheet...")
    try:
        resp = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Failed to fetch BooksGoat sheet: {e}")

    catalog = {}
    reader = csv.DictReader(io.StringIO(resp.text))

    for row in reader:
        isbn13 = str(row.get("ISBN-13", "")).strip().replace("-", "").replace(" ", "")
        if not isbn13 or len(isbn13) < 10:
            continue

        price_str = str(row.get(PRICE_COLUMN, "")).strip().replace("$", "").replace(",", "")
        try:
            price = float(price_str) if price_str else None
        except ValueError:
            price = None

        # If price column exists and > 0 → available
        available = price is not None and price > 0

        catalog[isbn13] = {"price": price, "available": available}

    print(f"  Sheet loaded: {len(catalog)} ISBNs found.")
    return catalog


# ══════════════════════════════════════════════════════════
# SECTION 3 — Risk assessment
# ══════════════════════════════════════════════════════════

def assess_risk(isbn: str, entry: dict, sheet_data: dict | None) -> dict | None:
    """
    Returns {"reason": str, "detail": str} if listing should be delisted, else None.

    sheet_data is None if the ISBN was not found in the catalog at all.
    """
    ebay_price = float(entry.get("ebay_price", 0) or 0)
    orig_cost  = float(entry.get("cost", 0) or 0)

    # ── Rule 1: ISBN not in sheet at all = OOS ─────────────
    if sheet_data is None:
        return {
            "reason": "OUT_OF_STOCK",
            "detail": "ISBN not found in BooksGoat catalog (removed or delisted)"
        }

    # ── Rule 2: Explicitly unavailable ────────────────────
    if not sheet_data.get("available", True):
        return {
            "reason": "OUT_OF_STOCK",
            "detail": "BooksGoat price is 0 or unavailable in sheet"
        }

    new_price = sheet_data.get("price")
    if new_price is None:
        return None  # Can't determine price — don't delist on uncertainty

    # ── Rule 3: Price spike vs original cost ──────────────
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

    # ── Rule 4: Profit gone negative ──────────────────────
    if ebay_price > 0 and new_price > 0:
        new_profit = ebay_price * (1 - EBAY_FEE_RATE) - new_price
        if new_profit < MIN_PROFIT_FLOOR:
            return {
                "reason": "PROFIT_GONE",
                "detail": (
                    f"New cost ${new_price:.2f} vs eBay price ${ebay_price:.2f} "
                    f"= ${new_profit:.2f} profit (floor is ${MIN_PROFIT_FLOOR:.2f})"
                )
            }

    return None


# ══════════════════════════════════════════════════════════
# SECTION 4 — eBay delist
# ══════════════════════════════════════════════════════════

_token_cache = {"token": None, "expires": 0}

def get_ebay_token() -> str:
    client_id     = os.getenv("EBAY_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
    refresh_token = os.getenv("EBAY_REFRESH_TOKEN", "")
    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError("Missing eBay credentials in environment")
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    scopes = " ".join([
        "https://api.ebay.com/oauth/api_scope",
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
        "https://api.ebay.com/oauth/api_scope/sell.account",
    ])
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": scopes},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def ebay_headers() -> dict:
    if not _token_cache["token"] or time.time() > _token_cache["expires"] - 300:
        _token_cache["token"]   = get_ebay_token()
        _token_cache["expires"] = time.time() + 7200
        print("  eBay token refreshed.")
    return {
        "Authorization":           f"Bearer {_token_cache['token']}",
        "Content-Type":            "application/json",
        "Content-Language":        "en-US",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }


def withdraw_offer(offer_id: str) -> bool:
    resp = requests.post(
        f"{EBAY_BASE}/offer/{offer_id}/withdraw",
        headers=ebay_headers(),
        timeout=15,
    )
    return resp.status_code in (200, 204)


# ══════════════════════════════════════════════════════════
# SECTION 5 — Email summary
# ══════════════════════════════════════════════════════════

def send_email(subject: str, body: str):
    if not all([SMTP_USER, SMTP_PASS, EMAIL_TO]):
        print("[WARN] Email not configured — skipping.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO.split(","), msg.as_string())
        print(f"[EMAIL] Sent to {EMAIL_TO}")
    except Exception as e:
        print(f"[WARN] Email failed: {e}")


def build_summary(run_ts, checked, delisted, price_changes, not_in_sheet, no_offer_id):
    lines = [
        "BooksGoat Tracker — Daily Summary",
        f"Run: {run_ts} UTC",
        "=" * 60,
        f"Active listings checked : {checked}",
        f"Delisted this run       : {len(delisted)}",
        f"Price changes noted     : {len(price_changes)}",
        f"Not in BooksGoat sheet  : {len(not_in_sheet)}",
        f"No offer_id (manual)    : {len(no_offer_id)}",
        "",
    ]

    if delisted:
        lines.append("── DELISTED ─────────────────────────────────────")
        for d in delisted:
            auto = "auto-delisted" if d.get("auto_delisted") else "NEEDS MANUAL DELIST"
            lines.append(f"  [{d['reason']}] {d['title'][:50]}")
            lines.append(f"    ISBN: {d['isbn']} | {auto}")
            lines.append(f"    {d['detail']}")
        lines.append("")

    if price_changes:
        lines.append("── PRICE CHANGES (still profitable) ─────────────")
        for p in price_changes:
            lines.append(f"  {p['title'][:50]}")
            lines.append(f"    ISBN: {p['isbn']} | ${p['old']:.2f} → ${p['new']:.2f}")
        lines.append("")

    if no_offer_id:
        lines.append("── MANUAL LISTINGS (no offer_id — cannot auto-delist) ──")
        for isbn in no_offer_id:
            lines.append(f"  {isbn}")
        lines.append("  Run migrate_tracker_input.py to add offer IDs.")
        lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# SECTION 6 — Main
# ══════════════════════════════════════════════════════════

def main():
    run_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"BooksGoat Cloud Tracker v2 — {run_ts} UTC")
    print(f"{'='*60}\n")

    # Load state
    state  = load_state()
    active = get_active_listings(state)
    print(f"Active listings in registry: {len(active)}")

    if not active:
        msg = "No active listings found in lister_state.json."
        print(msg)
        send_email(f"[Tracker] No active listings — {run_ts[:10]}", msg)
        return

    # Fetch entire BooksGoat catalog in one request
    try:
        catalog = fetch_booksgoat_sheet()
    except RuntimeError as e:
        msg = f"CRITICAL: Could not fetch BooksGoat sheet.\n{e}"
        print(msg)
        send_email(f"[Tracker] SHEET FETCH FAILED — {run_ts[:10]}", msg)
        save_state(state)
        sys.exit(1)

    # Process each active listing
    delisted      = []
    price_changes = []
    not_in_sheet  = []
    no_offer_id   = []
    checked       = 0

    for entry in active:
        isbn  = entry["isbn"]
        title = entry.get("title", "Unknown")[:50]
        print(f"\n[{isbn}] {title[:45]}...")

        # Look up in catalog
        sheet_data = catalog.get(isbn)
        checked   += 1

        if sheet_data:
            new_price = sheet_data.get("price")
            old_price = float(entry.get("last_supplier_price") or entry.get("cost") or 0)
            avail     = sheet_data.get("available")
            print(f"  Sheet: price=${new_price:.2f}" if new_price else "  Sheet: price=unknown", end="")
            print(f" | available={avail}")

            # Track meaningful price changes (not triggering delist)
            if new_price and old_price and abs(new_price - old_price) >= 0.50:
                price_changes.append({
                    "isbn": isbn, "title": title,
                    "old": old_price, "new": new_price
                })

            # Update state
            if new_price:
                state["listings"][isbn]["last_supplier_price"] = new_price
            state["listings"][isbn]["last_supplier_available"] = avail
        else:
            print(f"  NOT IN SHEET — treating as OOS")
            not_in_sheet.append(isbn)

        state["listings"][isbn]["last_checked"] = run_ts

        # Assess risk
        risk = assess_risk(isbn, entry, sheet_data)
        if not risk:
            print(f"  OK — no risk detected.")
            continue

        # ── Delist ────────────────────────────────────────────
        print(f"  FLAGGED [{risk['reason']}]: {risk['detail']}")
        offer_id = entry.get("offer_id", "")

        if not offer_id:
            print(f"  No offer_id — cannot auto-delist. Alert sent.")
            no_offer_id.append(isbn)
            state["listings"][isbn]["status"] = "NEEDS_MANUAL_DELIST"
            state["listings"][isbn]["delist_reason"] = risk["reason"]
            delisted.append({
                "isbn": isbn, "title": title,
                "reason": risk["reason"], "detail": risk["detail"],
                "offer_id": "", "auto_delisted": False
            })
            continue

        try:
            success = withdraw_offer(offer_id)
        except Exception as ex:
            success = False
            print(f"  Delist exception: {ex}")

        if success:
            print(f"  DELISTED from eBay (offer {offer_id})")
            state["listings"][isbn]["status"]        = "DELISTED"
            state["listings"][isbn]["delisted_at"]   = run_ts
            state["listings"][isbn]["delist_reason"] = risk["reason"]
        else:
            print(f"  Delist API FAILED for offer {offer_id} — manual action required.")
            state["listings"][isbn]["status"] = "DELIST_FAILED"

        delisted.append({
            "isbn": isbn, "title": title,
            "reason": risk["reason"], "detail": risk["detail"],
            "offer_id": offer_id, "auto_delisted": success
        })

    # Save state
    save_state(state)
    print(f"\nState saved to {LISTER_STATE_PATH}")

    # Summary
    summary = build_summary(run_ts, checked, delisted, price_changes, not_in_sheet, no_offer_id)
    print(f"\n{summary}")

    subject = (
        f"[Tracker] {run_ts[:10]} — "
        f"{len(delisted)} delisted | {checked} checked | "
        f"{len(not_in_sheet)} OOS"
    )
    send_email(subject, summary)

    print(f"\n{'='*60}")
    print(f"Done. {checked} checked | {len(delisted)} flagged | {len(price_changes)} price changes")
    print(f"{'='*60}\n")

    # Non-zero exit if any delist failed (GitHub Actions will flag the run)
    failed = [d for d in delisted if not d["auto_delisted"] and d["offer_id"]]
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
