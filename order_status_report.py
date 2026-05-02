#!/usr/bin/env python3
"""
order_status_report.py v2 — Daily order fulfillment lifecycle tracker.

Runs: Daily 8AM UTC via GitHub Actions
Location: Fulfillment/order_status_report.py

Cross-references three signals to track every eBay order:
  1. eBay Orders API → all sales (last 14 days)
  2. BooksGoat confirmation emails via Gmail IMAP → order placed on BG
  3. BooksGoat shipping emails via Gmail IMAP → BG shipped the order
  4. shipping_state.json → tracking posted to eBay

Order lifecycle:
  eBay sale → AWAITING BG ORDER (<48h)
           → URGENT (>48h, no BG confirmation found)
           → BG ORDERED (confirmation email matched)
           → SHIPPED (shipping email matched or eBay fulfilled)
           → CANCEL (blocklisted ISBN — cannot fulfill)

Matching logic (BG confirmation → eBay order):
  - Primary: ISBN match (BG confirmation ISBN = eBay order SKU/ISBN)
  - Secondary: buyer name fuzzy match (for disambiguation when same ISBN
    sold to multiple buyers)
  - BG confirmations contain: order number, ISBN, customer name, address

v2 changes (2026-05-01):
  - Robust multi-pattern ISBN extraction from BG emails
  - Buyer name extraction and fuzzy matching
  - 48-hour URGENT threshold (was 24h)
  - Blocklist detection with CANCEL category
  - HTML formatted email
  - Detailed logging for parser debugging

v2.1 changes (2026-05-01):
  - Updated for actual BG email format: "THANK YOU FOR PLACING YOUR ORDER",
    "Your order has been Confirmed", "Your order has been Packed"
  - Product title extraction for fallback matching when ISBN missing
  - Dedup by BG order ID (3 emails per order: placed, confirmed, packed)
  - Title fuzzy matching as ISBN fallback
  - Shipping Address name extraction pattern from actual BG format

Env vars:
  EBAY_APP_ID (or EBAY_CLIENT_ID), EBAY_CERT_ID (or EBAY_CLIENT_SECRET)
  EBAY_REFRESH_TOKEN
  SMTP_USER, SMTP_PASSWORD
  EMAIL_FROM, EMAIL_TO
"""
import base64
import imaplib
import email as email_lib
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from email.header import decode_header as _dh
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
EBAY_APP_ID = os.environ.get("EBAY_APP_ID") or os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID") or os.environ.get("EBAY_CLIENT_SECRET", "")
EBAY_REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", os.environ.get("SMTP_USER", ""))
EMAIL_TO = os.environ.get("EMAIL_TO", "jubran.industries@gmail.com")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
LOOKBACK_DAYS = 14
URGENT_HOURS = 48

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_FULFILL_API = "https://api.ebay.com/sell/fulfillment/v1"
SCOPES = ("https://api.ebay.com/oauth/api_scope "
           "https://api.ebay.com/oauth/api_scope/sell.fulfillment")

# shipping_state.json — check in script dir and repo root
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
STATE_PATHS = [
    SCRIPT_DIR / "shipping_state.json",
    REPO_ROOT / "shipping_state.json",
]

# ── Blocklist ────────────────────────────────────────────────
# ISBNs that cannot be fulfilled via BooksGoat dropship
BLOCKLIST = {
    "9781260460445": "Min qty 5 — Lange Q&A Radiography",
    "9780990873853": "Min qty 5 — Overcoming Gravity",
    "9781119826798": "PDF only — Architect's Studio Companion",
    "9780973501827": "Min qty 10 — Back Mechanic",
    "9780393979503": "Download only — C Programming Modern Approach",
    "9781118115121": "Min qty 5 — Art & Science of Technical Analysis",
    "9781466516946": "Counterfeit flag — American Herbal Pharmacopoeia",
}


# ── eBay Auth ────────────────────────────────────────────────
def get_ebay_token() -> str:
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = requests.post(
        EBAY_OAUTH_URL,
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token",
              "refresh_token": EBAY_REFRESH_TOKEN,
              "scope": SCOPES},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OAuth failed: {r.status_code} {r.text}")
    log.info("eBay token acquired")
    return r.json()["access_token"]


# ── Fetch eBay Orders ────────────────────────────────────────
def fetch_ebay_orders(token: str) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime(
        "%Y-%m-%dT00:00:00.000Z")
    results = []
    url = f"{EBAY_FULFILL_API}/order"
    params = {"filter": f"creationdate:[{since}]", "limit": 200}
    while True:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Order fetch failed: {r.status_code} {r.text}")
        data = r.json()
        results.extend(data.get("orders", []))
        next_url = data.get("next")
        if not next_url:
            break
        url = next_url
        params = {}
    log.info(f"Fetched {len(results)} eBay orders from last {LOOKBACK_DAYS} days")
    return results


def parse_ebay_order(order: dict) -> dict:
    """Extract key fields from an eBay order."""
    items = order.get("lineItems", [])
    first = items[0] if items else {}
    sku = first.get("sku", "")
    isbn = sku if sku.isdigit() and len(sku) == 13 else ""
    title = first.get("title", "")[:60]
    total = order.get("pricingSummary", {}).get("total", {})
    price = float(total.get("value", 0))
    created = order.get("creationDate", "")
    status = order.get("orderFulfillmentStatus", "")

    # Cancel/refund detection — check multiple signals
    cancel_status = order.get("cancelStatus", {})
    cancel_state = cancel_status.get("cancelState", "NONE_REQUESTED")
    cancel_requests = cancel_status.get("cancelRequests", [])
    payment_status = order.get("orderPaymentStatus", "")

    # Broad cancel detection:
    # - cancelState explicitly set to a cancel value
    # - cancelRequests array is non-empty (cancel was requested even if state hasn't updated)
    # - payment status indicates refund
    is_canceled = cancel_state not in ("NONE_REQUESTED", "")
    has_cancel_requests = len(cancel_requests) > 0
    is_refunded = "REFUND" in payment_status.upper() if payment_status else False
    is_effectively_canceled = is_canceled or has_cancel_requests or is_refunded

    cancel_detail = ""
    if is_canceled:
        cancel_detail = cancel_state
    elif has_cancel_requests:
        cancel_detail = f"CANCEL_REQUESTED ({len(cancel_requests)} requests)"
    elif is_refunded:
        cancel_detail = "REFUNDED"

    # Buyer info
    ship_to = (order.get("fulfillmentStartInstructions") or [{}])[0] \
        .get("shippingStep", {}).get("shipTo", {})
    buyer_name = ship_to.get("fullName", "")

    # Parse creation datetime
    created_dt = None
    if created:
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            pass

    return {
        "order_id": order.get("orderId", ""),
        "isbn": isbn,
        "title": title,
        "price": price,
        "created": created,
        "created_dt": created_dt,
        "status": status,
        "buyer_name": buyer_name,
        "is_canceled": is_effectively_canceled,
        "cancel_detail": cancel_detail,
        # Raw fields for debugging orders that fall through to URGENT/AWAITING
        "_cancel_state": cancel_state,
        "_payment_status": payment_status,
        "_cancel_requests_count": len(cancel_requests),
    }


# ── Gmail IMAP — BooksGoat Emails ────────────────────────────
def _decode_header(val: str) -> str:
    parts = _dh(val or "")
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="ignore")
        else:
            result += part
    return result


def _get_email_text(msg) -> str:
    """Extract plain text from email message."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_payload(decode=True) or b""
            if ct == "text/plain":
                text += payload.decode("utf-8", errors="ignore")
            elif ct == "text/html":
                html = payload.decode("utf-8", errors="ignore")
                text += re.sub(r"<[^>]+>", " ", html)
    else:
        payload = msg.get_payload(decode=True) or b""
        text = payload.decode("utf-8", errors="ignore")
    return text


def _extract_isbn_from_text(text: str) -> str | None:
    """Extract ISBN-13 from email text. Tries multiple patterns."""
    # Pattern 1: Labeled ISBN
    m = re.search(r'ISBN[\-\s]*(?:13)?[:\s]*(97[89][\d\-\s]{10,17})', text, re.IGNORECASE)
    if m:
        digits = re.sub(r'[^\d]', '', m.group(1))
        if len(digits) == 13 and digits[:3] in ('978', '979'):
            return digits

    # Pattern 2: Bare 978/979 with possible formatting
    m = re.search(r'(97[89][\d\-\s]{10,17})', text)
    if m:
        digits = re.sub(r'[^\d]', '', m.group(1))
        if len(digits) == 13:
            return digits

    # Pattern 3: Clean 13-digit starting with 978/979
    m = re.search(r'\b(97[89]\d{10})\b', text)
    if m:
        return m.group(1)

    return None


def _extract_buyer_name_from_text(text: str) -> str | None:
    """Extract buyer/recipient name from BG email.
    
    Actual BG format: 'Shipping Address: Tracy Smith LPC SURGERY CENTER
                       770 Park East Blvd Lafayette, Indiana 47905'
    The name is the first line after 'Shipping Address:' before the street address.
    """
    # Pattern 1: BG-specific — "Shipping Address:" followed by name
    # The name line(s) come before the street address (starts with digits)
    m = re.search(
        r'[Ss]hipping\s+[Aa]ddress\s*:\s*'
        r'([A-Z][A-Za-z\'\-\.]*(?:[\s]+[A-Z][A-Za-z\'\-\.]*){0,5})',
        text
    )
    if m:
        name = m.group(1).strip()
        # Stop at address-like fragments (digits, PO Box, etc.)
        name = re.split(
            r'[\s\n]+(?:\d|PO\b|P\.O\.|Box\b|Suite\b|Apt\b|Unit\b|LLC\b|INC\b|CENTER\b|CENTRE\b)',
            name, flags=re.IGNORECASE
        )[0].strip()
        name = re.sub(r'\s*\n\s*', ' ', name)
        # Also stop at known non-name business suffixes
        name = re.split(r'\s+(?:LPC|LLC|INC|DBA|CENTER|CENTRE|SCHOOL|CORP)\b',
                        name, flags=re.IGNORECASE)[0].strip()
        if len(name) >= 3:
            return name

    # Pattern 2: "Dear Name,"
    m = re.search(r'Dear\s+([A-Z][A-Za-z\'\-]*(?:\s+[A-Z][A-Za-z\'\-]+){1,4})\s*,', text)
    if m:
        return m.group(1).strip()

    # Pattern 3-6: Other common patterns
    for pattern in [
        r'[Ss]hip\s*[Tt]o[:\s]+([A-Z][A-Za-z\'\-]*(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
        r'[Dd]eliver\s*[Tt]o[:\s]+([A-Z][A-Za-z\'\-]*(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
        r'[Rr]ecipient[:\s]+([A-Z][A-Za-z\'\-]*(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
        r'[Cc]ustomer[:\s]+([A-Z][A-Za-z\'\-]*(?:\s+[A-Z][A-Za-z\'\-]+){1,4})',
    ]:
        m = re.search(pattern, text)
        if m:
            name = m.group(1).strip()
            name = re.split(r'[\s\n]+(?:\d|PO\b|P\.O\.|Box\b|Suite\b|Apt\b|Unit\b)',
                            name)[0].strip()
            name = re.sub(r'\s*\n\s*', ' ', name)
            if len(name) >= 3:
                return name
    return None


def _extract_bg_order_id(text: str) -> str | None:
    """Extract BooksGoat order number from email.
    Actual format: 'Order ID: #26917' or 'Order ID:#26917'"""
    m = re.search(r'[Oo]rder\s*(?:ID|#|No\.?|Number)?[:\s#]*#?(\d{4,6})', text)
    return m.group(1) if m else None


def _extract_bg_order_status(text: str) -> str | None:
    """Extract BG order status from email.
    Actual format: 'Order Status: Confirmed' or 'Order Status: Packed'"""
    m = re.search(r'[Oo]rder\s+[Ss]tatus\s*:\s*(\w+)', text)
    return m.group(1).lower() if m else None


def _extract_product_title(text: str) -> str | None:
    """Extract product/book title from BG email.
    
    In BG emails, the product title appears in the order details table.
    After HTML stripping, it looks like:
    'Sterile Processing Technical Manual (CRCST), 9th Edition by
     Healthcare Sterile Processing Association (HSPA) ISBN: 9788350705218 – Paperback'
    
    Strategy: find text near 'Product' header or before 'ISBN:' in the product section.
    """
    # Pattern 1: Text between "Product" table header and "ISBN:" or "PAPERBACK"/"HARDCOVER"
    # After HTML stripping, the product section has the title run together
    m = re.search(
        r'(?:Product|Item)\s{2,}'  # "Product" header followed by whitespace
        r'([\w][\w\s,\.\'\-\(\):&;]+?)'  # title text
        r'(?:\s+ISBN[:\s]|\s+PAPERBACK|\s+HARDCOVER|\s+Model\b)',
        text, re.IGNORECASE
    )
    if m:
        title = m.group(1).strip()
        # Clean up — remove trailing "by Author Name" if very long
        if len(title) > 20:
            return title[:120]

    # Pattern 2: Text immediately before "ISBN:" 
    m = re.search(
        r'([A-Z][\w\s,\.\'\-\(\):&;]{15,120}?)\s*ISBN[:\s]',
        text
    )
    if m:
        title = m.group(1).strip()
        if len(title) > 10:
            return title[:120]

    # Pattern 3: Text after product/item label in a table-like structure
    m = re.search(
        r'(?:Book|Title|Product\s*Name)[:\s]+'
        r'([A-Z][\w\s,\.\'\-\(\):&;]{10,120})',
        text, re.IGNORECASE
    )
    if m:
        return m.group(1).strip()[:120]

    return None


def _titles_match(title_a: str | None, title_b: str | None) -> bool:
    """Fuzzy title match — checks if significant words overlap."""
    if not title_a or not title_b:
        return False
    # Normalize: lowercase, remove punctuation, split into words
    def _words(t):
        t = re.sub(r'[^\w\s]', ' ', t.lower())
        # Remove common noise words
        stop = {'the', 'a', 'an', 'of', 'and', 'for', 'by', 'in', 'to', 'edition',
                'ed', '2nd', '3rd', '4th', '5th', '6th', '7th', '8th', '9th', '10th',
                'isbn', 'paperback', 'hardcover'}
        return {w for w in t.split() if len(w) > 2 and w not in stop}

    words_a = _words(title_a)
    words_b = _words(title_b)
    if not words_a or not words_b:
        return False
    overlap = words_a & words_b
    # Match if ≥50% of the shorter word set overlaps
    shorter = min(len(words_a), len(words_b))
    return len(overlap) >= max(2, shorter * 0.5)


def _extract_tracking_from_text(text: str) -> tuple[str | None, str | None]:
    """Extract tracking number and carrier from BG shipping/delivered emails.
    
    Actual BG format: 'Tracking Number: 870988831002 Carrier: www.Fedex.com'
    Also: 'Order has been successfully delivered. 870988831002'
    """
    tracking = None
    carrier = None

    m = re.search(r'[Tt]racking\s*[Nn]umber\s*[:\s]*([A-Za-z0-9]{8,30})', text)
    if m:
        tracking = m.group(1)

    if not tracking:
        m = re.search(r'(?:delivered|shipped)[.\s]+(\d{10,20})', text, re.IGNORECASE)
        if m:
            tracking = m.group(1)

    if re.search(r'fedex\.com|fedex', text, re.IGNORECASE):
        carrier = 'FEDEX'
    elif re.search(r'\bups\.com|\bups\b', text, re.IGNORECASE):
        carrier = 'UPS'
    elif re.search(r'usps\.com|\busps\b', text, re.IGNORECASE):
        carrier = 'USPS'

    return tracking, carrier


def _classify_bg_email(text: str) -> tuple[str, int]:
    """Classify a BG email into a lifecycle stage and priority rank.
    
    BG sends up to 6 emails per order:
      1. 'THANK YOU FOR PLACING YOUR ORDER!'  → confirmed   (rank 1)
      2. 'Your order has been Confirmed.'      → confirmed   (rank 1)
      3. 'Your order has been Packed.'          → packed      (rank 2)
      4. 'YOUR ORDER HAS BEEN SHIPPED!'        → shipped     (rank 3)
      5. 'Your order has been In Transit.'      → in_transit  (rank 4)
      6. 'YOUR ORDER HAS BEEN DELIVERED!'       → delivered   (rank 5)
    
    Returns (stage, rank). Higher rank = further in lifecycle.
    """
    upper = text.upper()

    if 'HAS BEEN DELIVERED' in upper or 'SUCCESSFULLY DELIVERED' in upper:
        return 'delivered', 5
    if 'IN TRANSIT' in upper or 'IN-TRANSIT' in upper:
        return 'in_transit', 4
    if 'HAS BEEN SHIPPED' in upper or 'SUCCESSFULLY SHIPPED' in upper:
        return 'shipped', 3
    if 'HAS BEEN PACKED' in upper or 'READY FOR SHIPPING' in upper:
        return 'packed', 2
    if any(s in upper for s in [
        'THANK YOU FOR PLACING YOUR ORDER',
        'HAS BEEN CONFIRMED',
        'ORDER CONFIRMATION',
        'PAYMENT SUCCESSFUL',
        'ORDER RECEIVED',
        'ORDER PLACED',
        'YOUR ORDER',
        'ORDER DETAILS',
        'ORDER SUMMARY',
    ]):
        return 'confirmed', 1

    return 'unknown', 0


def fetch_bg_emails() -> list[dict]:
    """Fetch ALL BooksGoat emails from Gmail as unified lifecycle events.
    
    Every BG email — confirmation, packed, shipped, in transit, delivered —
    is evidence that the order was placed. The highest lifecycle stage wins
    after dedup by BG order ID. Up to 6 emails per order = 6 chances to catch it.
    
    Returns list of: {bg_order_id, isbn, buyer_name, product_title,
                      bg_stage, bg_rank, tracking, carrier}
    """
    raw_entries = []

    try:
        log.info("Connecting to Gmail IMAP...")
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(SMTP_USER, SMTP_PASSWORD)
        mail.select("inbox")

        since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        _, msg_ids = mail.search(None, "SINCE", since, "FROM", "booksgoat")
        ids = msg_ids[0].split()
        log.info(f"Found {len(ids)} BooksGoat emails in last {LOOKBACK_DAYS} days")

        for msg_id in ids:
            try:
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                msg = email_lib.message_from_bytes(msg_data[0][1])
                subject = _decode_header(msg.get("Subject", ""))
                text = _get_email_text(msg)

                isbn = _extract_isbn_from_text(text)
                buyer = _extract_buyer_name_from_text(text)
                bg_order = _extract_bg_order_id(text)
                product_title = _extract_product_title(text)
                bg_stage, bg_rank = _classify_bg_email(text)
                tracking, carrier = _extract_tracking_from_text(text)

                if bg_stage == 'unknown' and not bg_order:
                    log.info(f"  Unclassified BG email: subject='{subject[:50]}', "
                             f"isbn={isbn}, buyer={buyer}")
                    continue

                raw_entries.append({
                    "bg_order_id": bg_order,
                    "isbn": isbn,
                    "buyer_name": buyer,
                    "product_title": product_title,
                    "bg_stage": bg_stage,
                    "bg_rank": bg_rank,
                    "tracking": tracking,
                    "carrier": carrier,
                    "subject": subject[:60],
                })

            except Exception as e:
                log.warning(f"  Failed to parse email {msg_id}: {e}")
                continue

        mail.logout()
    except Exception as e:
        log.error(f"Gmail IMAP failed: {e}")
        return []

    log.info(f"Parsed {len(raw_entries)} BG lifecycle emails")

    # Deduplicate by BG order ID — merge all 6 emails into one entry
    by_order: dict[str | None, dict] = {}
    no_order = []
    for e in raw_entries:
        oid = e.get("bg_order_id")
        if not oid:
            no_order.append(e)
            continue
        existing = by_order.get(oid)
        if not existing:
            by_order[oid] = e
        else:
            for key in ("isbn", "buyer_name", "product_title", "tracking", "carrier"):
                if e.get(key) and not existing.get(key):
                    existing[key] = e[key]
            if e["bg_rank"] > existing["bg_rank"]:
                existing["bg_stage"] = e["bg_stage"]
                existing["bg_rank"] = e["bg_rank"]

    entries = list(by_order.values()) + no_order

    log.info(f"After dedup: {len(entries)} unique BG orders "
             f"(from {len(raw_entries)} raw emails)")

    stage_counts: dict[str, int] = {}
    for e in entries:
        stage_counts[e["bg_stage"]] = stage_counts.get(e["bg_stage"], 0) + 1
        log.info(f"  BG#{e['bg_order_id']} stage={e['bg_stage']} isbn={e['isbn']} "
                 f"buyer={e['buyer_name']} tracking={e['tracking']} "
                 f"title={e['product_title'][:40] if e['product_title'] else 'None'}")

    log.info(f"  Stage breakdown: {stage_counts}")
    log.info(f"  ISBNs: {sum(1 for e in entries if e['isbn'])}/{len(entries)} | "
             f"Titles: {sum(1 for e in entries if e['product_title'])}/{len(entries)} | "
             f"Tracking: {sum(1 for e in entries if e['tracking'])}/{len(entries)}")

    return entries


# ── Shipping State ───────────────────────────────────────────
def load_shipping_state() -> dict:
    """Load shipping_state.json if available."""
    for path in STATE_PATHS:
        if path.exists():
            try:
                state = json.loads(path.read_text())
                log.info(f"Loaded shipping_state.json from {path} ({len(state)} entries)")
                return state
            except Exception as e:
                log.warning(f"Failed to load {path}: {e}")
    log.info("No shipping_state.json found")
    return {}


# ── Name Matching ────────────────────────────────────────────
def _normalize_name(name: str) -> str:
    return unicodedata.normalize("NFKD", name.lower()) \
        .encode("ascii", "ignore").decode("ascii").strip()


def _names_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    na = _normalize_name(a)
    nb = _normalize_name(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


# ── Order Classification ────────────────────────────────────
def classify_orders(ebay_orders: list[dict],
                    bg_emails: list[dict],
                    shipping_state: dict) -> list[dict]:
    """Classify each eBay order into a fulfillment status category.
    
    Matching hierarchy:
      1. ISBN match (primary — most reliable)
      2. Title fuzzy match (fallback — when BG email has no ISBN)
      3. Buyer name used for disambiguation when multiple matches exist
    
    BG lifecycle stages (from bg_emails) determine the detail level:
      confirmed/packed = BG_ORDERED
      shipped/in_transit/delivered = SHIPPED (from BG email signal)
    """
    now = datetime.now(timezone.utc)

    # Build lookup from unified BG emails
    # isbn → list of BG entries
    bg_by_isbn: dict[str, list[dict]] = {}
    bg_by_title: list[dict] = []  # for title-based fallback
    bg_all: list[dict] = bg_emails  # for buyer-name fallback
    for e in bg_emails:
        if e["isbn"]:
            bg_by_isbn.setdefault(e["isbn"], []).append(e)
        if e.get("product_title"):
            bg_by_title.append(e)

    # Shipping state: ebay_order_id → entry
    state_by_ebay = {}
    for bg_id, entry in shipping_state.items():
        ebay_oid = entry.get("ebay_order_id")
        if ebay_oid:
            state_by_ebay[ebay_oid] = entry

    def _find_bg_match(isbn: str, title: str, buyer_name: str) -> dict | None:
        """Find the best BG email match for this eBay order.
        Returns the matched BG entry (with bg_stage, bg_rank, tracking, etc.)
        or None if no match found.
        
        Match hierarchy:
          1. ISBN match (strongest signal)
          2. Title fuzzy match (when BG email has no ISBN)
          3. Buyer name match (last resort — when BG email has no ISBN and no title)
        """
        # Try ISBN first
        if isbn and isbn in bg_by_isbn:
            entries = bg_by_isbn[isbn]
            if len(entries) == 1:
                return entries[0]
            # Multiple — prefer buyer name match
            for e in entries:
                if _names_match(buyer_name, e.get("buyer_name")):
                    return e
            # No name match — return the highest-stage one
            return max(entries, key=lambda e: e.get("bg_rank", 0))

        # Fallback 2: title match
        if title:
            for e in bg_by_title:
                if _titles_match(title, e.get("product_title")):
                    if buyer_name and e.get("buyer_name"):
                        if _names_match(buyer_name, e["buyer_name"]):
                            log.info(f"  Title+name match: '{title[:40]}' ↔ "
                                     f"BG#{e.get('bg_order_id')}")
                            return e
                    else:
                        log.info(f"  Title-only match: '{title[:40]}' ↔ "
                                 f"BG#{e.get('bg_order_id')} (no name verification)")
                        return e

        # Fallback 3: buyer name match (for BG emails with no ISBN and no title)
        # This catches cases like BG#27154 where ISBN extraction failed
        if buyer_name:
            name_matches = [e for e in bg_all if _names_match(buyer_name, e.get("buyer_name"))]
            if len(name_matches) == 1:
                e = name_matches[0]
                log.info(f"  Name-only match: '{buyer_name}' ↔ "
                         f"BG#{e.get('bg_order_id')} (no ISBN/title verification)")
                return e
            elif len(name_matches) > 1:
                # Multiple name matches — too ambiguous, reject
                log.info(f"  Name match ambiguous: '{buyer_name}' matched "
                         f"{len(name_matches)} BG orders — rejecting")

        return None

    results = []
    for order in ebay_orders:
        o = parse_ebay_order(order)
        isbn = o["isbn"]
        title = o["title"]
        order_id = o["order_id"]
        buyer_name = o["buyer_name"]
        hours_ago = None
        if o["created_dt"]:
            hours_ago = (now - o["created_dt"]).total_seconds() / 3600

        # Classification priority:

        # 1. CANCELED/REFUNDED — eBay order was canceled or refunded
        #    Skip these entirely — no action needed
        if o.get("is_canceled"):
            o["category"] = "CANCELED"
            o["category_detail"] = o.get("cancel_detail", "Canceled")
            o["hours_ago"] = hours_ago
            results.append(o)
            continue

        # 2. FULFILLED — eBay says it's done
        if o["status"] == "FULFILLED":
            o["category"] = "FULFILLED"
            o["category_detail"] = "eBay: FULFILLED"
            o["hours_ago"] = hours_ago
            results.append(o)
            continue

        # 3. SHIPPED — shipping_state.json has this order
        state_entry = state_by_ebay.get(order_id)
        if state_entry and state_entry.get("status") in ("shipped", "posted"):
            tracking = state_entry.get("tracking")
            posted = state_entry.get("tracking_posted", False)
            detail = "Shipped"
            if tracking and posted:
                detail = f"Shipped + tracking {tracking}"
            elif tracking:
                detail = f"Shipped (tracking {tracking} not posted to eBay)"
            o["category"] = "SHIPPED"
            o["category_detail"] = detail
            o["hours_ago"] = hours_ago
            results.append(o)
            continue

        # 4. Check BG emails — unified lifecycle match
        bg_match = _find_bg_match(isbn, title, buyer_name)
        if bg_match:
            stage = bg_match.get("bg_stage", "confirmed")
            rank = bg_match.get("bg_rank", 1)
            tracking = bg_match.get("tracking")

            if rank >= 3:
                # shipped / in_transit / delivered → SHIPPED
                detail = f"BG: {stage}"
                if tracking:
                    detail += f" (tracking {tracking})"
                o["category"] = "SHIPPED"
                o["category_detail"] = detail
            else:
                # confirmed / packed → BG_ORDERED
                o["category"] = "BG_ORDERED"
                o["category_detail"] = f"BG: {stage} (BG#{bg_match.get('bg_order_id', '?')})"

            o["hours_ago"] = hours_ago
            results.append(o)
            continue

        # 5. CANCEL — blocklisted ISBN (only for unfulfilled, non-shipped orders)
        #    Checked AFTER fulfillment signals so orders already placed on BG
        #    don't get incorrectly flagged
        if isbn and isbn in BLOCKLIST:
            o["category"] = "CANCEL"
            o["category_detail"] = BLOCKLIST[isbn]
            o["hours_ago"] = hours_ago
            results.append(o)
            continue

        # 6. URGENT or AWAITING — no BG signal found
        if hours_ago is not None and hours_ago > URGENT_HOURS:
            o["category"] = "URGENT"
            o["category_detail"] = f"{hours_ago:.0f}h since eBay sale, no BG order found"
            # Debug: log raw cancel/payment fields for investigation
            log.warning(f"  URGENT order {order_id}: cancel_state={o.get('_cancel_state')}, "
                        f"payment={o.get('_payment_status')}, "
                        f"cancel_requests={o.get('_cancel_requests_count')}, "
                        f"fulfillment={o['status']}")
        else:
            o["category"] = "AWAITING"
            o["category_detail"] = f"{hours_ago:.0f}h since sale" if hours_ago else "Unknown age"

        o["hours_ago"] = hours_ago
        results.append(o)

    # Log classification summary
    cat_counts = {}
    for o in results:
        cat_counts[o["category"]] = cat_counts.get(o["category"], 0) + 1
    log.info(f"Classification: {cat_counts}")

    return results


# ── HTML Report ──────────────────────────────────────────────
def _time_str(hours: float | None) -> str:
    if hours is None:
        return "?"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.1f}d"


def generate_html_report(classified: list[dict]) -> tuple[str, str]:
    """Generate HTML email and plain-text version."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A %b %d, %Y")

    # Count by category
    cats = {}
    for o in classified:
        cats.setdefault(o["category"], []).append(o)

    urgent = cats.get("URGENT", [])
    awaiting = cats.get("AWAITING", [])
    bg_ordered = cats.get("BG_ORDERED", [])
    shipped = cats.get("SHIPPED", [])
    fulfilled = cats.get("FULFILLED", [])
    cancel = cats.get("CANCEL", [])
    canceled = cats.get("CANCELED", [])

    # Sort urgent by hours_ago descending (oldest first = most urgent)
    urgent.sort(key=lambda x: -(x.get("hours_ago") or 0))
    awaiting.sort(key=lambda x: -(x.get("hours_ago") or 0))

    # Active orders = everything except canceled
    active_count = len(classified) - len(canceled)

    # ── HTML ──
    h = []
    h.append("""
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 700px; margin: 0 auto; color: #1a1a1a;">
""")

    # Header
    h.append(f"""
<div style="background: #1a1a2e; color: white; padding: 20px 24px; border-radius: 8px 8px 0 0;">
  <h1 style="margin: 0; font-size: 20px; font-weight: 600;">📋 Order Status Report</h1>
  <p style="margin: 4px 0 0; font-size: 13px; color: #a0a0c0;">{date_str} | Lookback: {LOOKBACK_DAYS} days | {active_count} active orders ({len(canceled)} canceled)</p>
</div>
""")

    # Summary bar
    h.append(f"""
<div style="display: flex; gap: 0; border: 1px solid #e0e0e0; border-top: none; font-size: 13px;">
  <div style="flex: 1; padding: 10px; text-align: center; background: #fdeaea; border-right: 1px solid #e0e0e0;">
    <div style="font-size: 22px; font-weight: 700; color: #c0392b;">{len(urgent)}</div>
    <div style="font-size: 10px; color: #c0392b;">🔴 URGENT</div>
  </div>
  <div style="flex: 1; padding: 10px; text-align: center; background: #fff8e1; border-right: 1px solid #e0e0e0;">
    <div style="font-size: 22px; font-weight: 700; color: #e65100;">{len(awaiting)}</div>
    <div style="font-size: 10px; color: #e65100;">🟡 Awaiting</div>
  </div>
  <div style="flex: 1; padding: 10px; text-align: center; background: #e8f5e9; border-right: 1px solid #e0e0e0;">
    <div style="font-size: 22px; font-weight: 700; color: #2e7d32;">{len(bg_ordered)}</div>
    <div style="font-size: 10px; color: #2e7d32;">📦 BG Ordered</div>
  </div>
  <div style="flex: 1; padding: 10px; text-align: center; background: #e3f2fd; border-right: 1px solid #e0e0e0;">
    <div style="font-size: 22px; font-weight: 700; color: #1565c0;">{len(shipped) + len(fulfilled)}</div>
    <div style="font-size: 10px; color: #1565c0;">✅ Shipped</div>
  </div>
  <div style="flex: 1; padding: 10px; text-align: center; background: #fce4ec; border-right: 1px solid #e0e0e0;">
    <div style="font-size: 22px; font-weight: 700; color: #880e4f;">{len(cancel)}</div>
    <div style="font-size: 10px; color: #880e4f;">🚫 Cancel</div>
  </div>
  <div style="flex: 1; padding: 10px; text-align: center; background: #f5f5f5;">
    <div style="font-size: 22px; font-weight: 700; color: #999;">{len(canceled)}</div>
    <div style="font-size: 10px; color: #999;">⊘ Canceled</div>
  </div>
</div>
""")

    # Section helper
    def _section(title: str, emoji: str, orders: list, bg_color: str,
                 border_color: str, text_color: str):
        if not orders:
            return
        h.append(f"""
<div style="padding: 14px 24px; border: 1px solid #e0e0e0; border-top: none; background: {bg_color};">
  <h2 style="margin: 0 0 8px; font-size: 14px; color: {text_color};">
    {emoji} {title} ({len(orders)})
  </h2>
  <table style="width: 100%; font-size: 11px; border-collapse: collapse;">
    <tr>
      <th style="padding: 4px 6px; text-align: left; color: #888; border-bottom: 1px solid {border_color};">Order</th>
      <th style="padding: 4px 6px; text-align: left; color: #888; border-bottom: 1px solid {border_color};">Age</th>
      <th style="padding: 4px 6px; text-align: left; color: #888; border-bottom: 1px solid {border_color};">Title</th>
      <th style="padding: 4px 6px; text-align: left; color: #888; border-bottom: 1px solid {border_color};">ISBN</th>
      <th style="padding: 4px 6px; text-align: right; color: #888; border-bottom: 1px solid {border_color};">Price</th>
      <th style="padding: 4px 6px; text-align: left; color: #888; border-bottom: 1px solid {border_color};">Detail</th>
    </tr>""")
        for o in orders:
            age = _time_str(o.get("hours_ago"))
            detail = o.get("category_detail", "")[:40]
            h.append(f"""
    <tr style="border-bottom: 1px solid {border_color};">
      <td style="padding: 4px 6px; font-family: monospace; font-size: 10px;">{o['order_id']}</td>
      <td style="padding: 4px 6px; font-weight: 600;">{age}</td>
      <td style="padding: 4px 6px;">{o['title'][:40]}</td>
      <td style="padding: 4px 6px; font-family: monospace; font-size: 10px;">{o['isbn']}</td>
      <td style="padding: 4px 6px; text-align: right;">${o['price']:.2f}</td>
      <td style="padding: 4px 6px; font-size: 10px; color: #888;">{detail}</td>
    </tr>""")
        h.append("</table></div>")

    # Sections in priority order
    _section("CANCEL — Cannot Fulfill", "🚫", cancel,
             "#fce4ec", "#f8bbd0", "#880e4f")
    _section("URGENT — No BG Order (>48h)", "🔴", urgent,
             "#fdeaea", "#f5c6c6", "#c0392b")
    _section("Awaiting BG Order (recent)", "🟡", awaiting,
             "#fff8e1", "#ffe0b2", "#e65100")
    _section("BG Ordered — Awaiting Shipment", "📦", bg_ordered,
             "#e8f5e9", "#c8e6c9", "#2e7d32")
    _section("Shipped / Fulfilled", "✅", shipped + fulfilled,
             "#e3f2fd", "#bbdefb", "#1565c0")
    _section("Canceled / Refunded", "⊘", canceled,
             "#f5f5f5", "#e0e0e0", "#999")

    # Footer
    h.append(f"""
<div style="padding: 12px 24px; font-size: 11px; color: #999; border: 1px solid #e0e0e0;
            border-top: none; border-radius: 0 0 8px 8px;">
  Generated {now.strftime('%Y-%m-%d %H:%M UTC')} | atlas_commerce
  <br>Action: Place BG orders for URGENT items. Cancel eBay orders for CANCEL items.
</div>
</div>""")

    html = "\n".join(h)

    # ── Plain text ──
    t = []
    t.append(f"ORDER STATUS REPORT — {date_str}")
    t.append(f"Lookback: {LOOKBACK_DAYS} days | Total orders: {len(classified)}")
    t.append("=" * 60)
    t.append("")

    def _text_section(title, orders):
        if not orders:
            return
        t.append(f"{title}: {len(orders)}")
        for o in orders:
            age = _time_str(o.get("hours_ago"))
            t.append(f"  Order {o['order_id']} | {age} ago | {o.get('created', '')[:19]}")
            t.append(f"    {o['title']}")
            t.append(f"    ISBN: {o['isbn']}  ${o['price']:.2f}")
            if o.get("category_detail"):
                t.append(f"    → {o['category_detail']}")
        t.append("")

    _text_section("🚫 CANCEL — Cannot Fulfill", cancel)
    _text_section("🔴 URGENT — No BG Order (>48h)", urgent)
    _text_section("🟡 Awaiting BG order (recent)", awaiting)
    _text_section("📦 BG Ordered", bg_ordered)
    _text_section("✅ Shipped/Fulfilled", shipped + fulfilled)
    _text_section("⊘ Canceled/Refunded", canceled)

    t.append("=" * 60)
    t.append("Action: Place BG orders for URGENT. Cancel eBay orders for CANCEL.")

    text = "\n".join(t)
    return html, text


# ── Email ────────────────────────────────────────────────────
def send_report(subject: str, html_body: str, text_body: str):
    if not SMTP_USER or not SMTP_PASSWORD:
        log.warning("SMTP not configured — printing to stdout")
        print(text_body)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
    log.info("Report email sent")


# ── Main ─────────────────────────────────────────────────────
def main() -> int:
    log.info("=" * 60)
    log.info("order_status_report.py v2 started")

    token = get_ebay_token()
    ebay_orders_raw = fetch_ebay_orders(token)

    bg_emails = fetch_bg_emails()
    shipping_state = load_shipping_state()

    classified = classify_orders(ebay_orders_raw, bg_emails, shipping_state)

    html, text = generate_html_report(classified)
    print(text)

    # Subject with urgency count
    urgent_count = sum(1 for o in classified if o["category"] == "URGENT")
    cancel_count = sum(1 for o in classified if o["category"] == "CANCEL")
    canceled_count = sum(1 for o in classified if o["category"] == "CANCELED")
    active_count = len(classified) - canceled_count

    urgency = ""
    if cancel_count:
        urgency += f" | {cancel_count} CANCEL"
    if urgent_count:
        urgency += f" | {urgent_count} URGENT"

    emoji = "🔴" if urgent_count > 0 or cancel_count > 0 else "✅"
    subject = (f"{emoji} Order Status: {active_count} active orders{urgency} — "
               f"{datetime.now().strftime('%a %b %d')}")

    send_report(subject, html, text)

    log.info("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
