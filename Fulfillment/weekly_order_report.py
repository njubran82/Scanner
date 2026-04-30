"""
weekly_order_report.py — Weekly email report of all eBay orders.

For each order, identifies whether the listing originated from the
BooksGoat merchant sheet or from a scraped category page.

Source detection logic:
  1. Download the BooksGoat merchant sheet CSV
  2. For each eBay order, check if ISBN exists in the merchant sheet
  3. If yes → source = "Merchant Sheet"
  4. If no → source = "Category Scrape"

Also checks booksgoat_enhanced.csv in the repo if present (has
the definitive category_path column).

Env vars required:
    EBAY_APP_ID, EBAY_CERT_ID, EBAY_REFRESH_TOKEN
    BOOKSGOAT_CSV_URL
    SMTP_USER, SMTP_PASSWORD
"""
import base64
import csv
import io
import logging
import os
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "")
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID", "")
EBAY_REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN", "")
BOOKSGOAT_CSV_URL = os.environ.get("BOOKSGOAT_CSV_URL", "")
EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_FULFILL_API = "https://api.ebay.com/sell/fulfillment/v1"
SCOPES = "https://api.ebay.com/oauth/api_scope/sell.fulfillment"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "jubran.industries@gmail.com")

LOOKBACK_DAYS = int(os.environ.get("REPORT_LOOKBACK_DAYS", "7"))

# Path to local enhanced CSV (in repo, if committed)
ENHANCED_CSV = Path(__file__).parent / "booksgoat_enhanced.csv"


# ── eBay Auth ────────────────────────────────────────────────
def get_ebay_token() -> str:
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = requests.post(
        EBAY_OAUTH_URL,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": EBAY_REFRESH_TOKEN,
            "scope": SCOPES,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OAuth failed: {r.status_code} {r.text}")
    log.info("eBay token acquired")
    return r.json()["access_token"]


def ebay_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ── Data Sources ─────────────────────────────────────────────
def load_merchant_sheet_isbns() -> set[str]:
    """Download BooksGoat merchant sheet and extract all ISBNs."""
    if not BOOKSGOAT_CSV_URL:
        log.warning("BOOKSGOAT_CSV_URL not set — merchant sheet source detection disabled")
        return set()
    try:
        r = requests.get(BOOKSGOAT_CSV_URL, timeout=30)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        isbns = set()
        for row in reader:
            isbn = row.get("ISBN-13", "").strip().replace("-", "")
            if isbn and len(isbn) == 13:
                isbns.add(isbn)
        log.info(f"Merchant sheet: {len(isbns)} ISBNs loaded")
        return isbns
    except Exception as e:
        log.error(f"Failed to load merchant sheet: {e}")
        return set()


def load_enhanced_csv_sources() -> dict[str, str]:
    """Load category_path from booksgoat_enhanced.csv if in repo."""
    sources = {}
    if not ENHANCED_CSV.exists():
        return sources
    try:
        with open(ENHANCED_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                isbn = row.get("isbn13", "").strip()
                cat = row.get("category_path", "").strip()
                if isbn and cat:
                    sources[isbn] = cat
        log.info(f"Enhanced CSV: {len(sources)} ISBNs with category_path")
    except Exception as e:
        log.error(f"Failed to load enhanced CSV: {e}")
    return sources


def determine_source(isbn: str, merchant_isbns: set, csv_sources: dict) -> str:
    """Determine if a listing came from merchant sheet or category scrape."""
    # Definitive source from enhanced CSV
    if isbn in csv_sources:
        cat = csv_sources[isbn]
        if cat == "merchant_sheet":
            return "Merchant Sheet"
        return f"Category {cat}"

    # Fallback: check merchant sheet
    if isbn in merchant_isbns:
        return "Merchant Sheet"

    return "Category Scrape"


# ── Fetch eBay Orders ────────────────────────────────────────
def fetch_recent_orders(token: str, days: int) -> list[dict]:
    """Fetch all orders from the last N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT00:00:00.000Z"
    )
    results = []
    url = f"{EBAY_FULFILL_API}/order"
    params = {
        "filter": f"creationdate:[{since}]",
        "limit": 200,
    }
    while url:
        r = requests.get(url, headers=ebay_headers(token), params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Order fetch failed: {r.status_code} {r.text}")
        data = r.json()
        results.extend(data.get("orders", []))
        url = data.get("next")
        params = {}
    return results


def parse_order(order: dict) -> dict:
    items = order.get("lineItems", [])
    first = items[0] if items else {}
    sku = first.get("sku", "")
    isbn = sku if sku.isdigit() and len(sku) == 13 else ""

    total = order.get("pricingSummary", {}).get("total", {})
    price = float(total.get("value", 0))

    created = order.get("creationDate", "")
    sold_date = created[:10] if created else ""

    status = order.get("orderFulfillmentStatus", "")
    buyer = order.get("buyer", {}).get("username", "")

    return {
        "order_id": order.get("orderId", ""),
        "isbn": isbn,
        "title": first.get("title", "")[:60],
        "price": price,
        "sold_date": sold_date,
        "status": status,
        "buyer": buyer,
    }


# ── Report Generation ────────────────────────────────────────
def generate_report(orders: list[dict], merchant_isbns: set, csv_sources: dict) -> str:
    lines = []
    lines.append(f"WEEKLY ORDER REPORT — {datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"Period: last {LOOKBACK_DAYS} days")
    lines.append(f"Total orders: {len(orders)}")
    lines.append("")

    # Summary by source
    source_counts: dict[str, int] = {}
    source_revenue: dict[str, float] = {}

    for o in orders:
        src = determine_source(o["isbn"], merchant_isbns, csv_sources)
        o["source"] = src
        source_counts[src] = source_counts.get(src, 0) + 1
        source_revenue[src] = source_revenue.get(src, 0) + o["price"]

    lines.append("SOURCE SUMMARY")
    lines.append("-" * 50)
    for src in sorted(source_counts.keys()):
        cnt = source_counts[src]
        rev = source_revenue[src]
        lines.append(f"  {src:<25} {cnt:>3} orders   ${rev:>8.2f}")
    lines.append("")

    # Order detail table
    lines.append("ORDER DETAIL")
    lines.append("-" * 100)
    lines.append(f"{'Date':<12} {'ISBN':<15} {'Price':>8} {'Source':<20} {'Title':<40} {'Status'}")
    lines.append("-" * 100)

    for o in sorted(orders, key=lambda x: x["sold_date"], reverse=True):
        lines.append(
            f"{o['sold_date']:<12} "
            f"{o['isbn']:<15} "
            f"${o['price']:>7.2f} "
            f"{o['source']:<20} "
            f"{o['title']:<40} "
            f"{o['status']}"
        )

    lines.append("")
    lines.append(f"Total revenue: ${sum(o['price'] for o in orders):.2f}")
    return "\n".join(lines)


def send_report(subject: str, body: str):
    if not SMTP_USER or not SMTP_PASSWORD:
        log.warning("SMTP not configured — printing report to stdout")
        print(body)
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    # Also attach as .txt for easy reference
    msg.add_attachment(
        body.encode("utf-8"),
        maintype="text",
        subtype="plain",
        filename=f"order_report_{datetime.now().strftime('%Y%m%d')}.txt",
    )

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
    log.info("Report email sent")


# ── Main ─────────────────────────────────────────────────────
def main() -> int:
    log.info("=" * 60)
    log.info("weekly_order_report.py started")

    token = get_ebay_token()
    merchant_isbns = load_merchant_sheet_isbns()
    csv_sources = load_enhanced_csv_sources()

    orders_raw = fetch_recent_orders(token, LOOKBACK_DAYS)
    log.info(f"Fetched {len(orders_raw)} orders from last {LOOKBACK_DAYS} days")

    orders = [parse_order(o) for o in orders_raw]
    report = generate_report(orders, merchant_isbns, csv_sources)

    print(report)
    send_report(
        f"[Weekly] Order Report — {datetime.now().strftime('%Y-%m-%d')} — {len(orders)} orders",
        report,
    )

    log.info("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
