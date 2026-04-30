"""
ship_deadline_guard.py — Emergency mark-as-shipped safety net.

Runs on GitHub Actions (every 6 hours). For every eBay order whose
ship-by date is tomorrow or earlier AND has not been marked shipped,
creates a shipping_fulfillment WITHOUT a tracking number.

When shipping_tracker.py later finds real tracking from BooksGoat
emails, it creates a SECOND fulfillment with the actual tracking.
eBay accepts multiple fulfillments per order.

DOES NOT purchase shipping labels. Only marks status as shipped.

Env vars required:
    EBAY_APP_ID, EBAY_CERT_ID, EBAY_REFRESH_TOKEN
    SMTP_USER, SMTP_PASSWORD (for alert emails)
"""
import base64
import json
import logging
import os
import smtplib
import ssl
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────
EBAY_APP_ID = os.environ.get("EBAY_APP_ID", "")
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID", "")
EBAY_REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN", "")
EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_FULFILL_API = "https://api.ebay.com/sell/fulfillment/v1"
SCOPES = "https://api.ebay.com/oauth/api_scope/sell.fulfillment"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "jubran.industries@gmail.com")

GUARD_DAYS_BEFORE = 1  # Mark shipped this many days before deadline

# State file — committed to repo so it persists between runs
STATE_FILE = Path(__file__).parent / "guard_state.json"


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
        "Accept": "application/json",
    }


# ── eBay Order Fetch ─────────────────────────────────────────
def fetch_awaiting_orders(token: str) -> list[dict]:
    """Fetch all orders not yet fully shipped."""
    results = []
    url = f"{EBAY_FULFILL_API}/order"
    params = {
        "filter": "orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}",
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


def parse_ship_by(order: dict) -> date | None:
    """Extract the ship-by date from an eBay order."""
    instructions = order.get("fulfillmentStartInstructions", [])
    if not instructions:
        return None

    # Try shipByDate first, then fall back to maxEstimatedDeliveryDate
    ship_step = instructions[0].get("shippingStep", {})
    ship_by_str = ship_step.get("shipByDate")
    if not ship_by_str:
        ship_by_str = instructions[0].get("maxEstimatedDeliveryDate")
    if not ship_by_str:
        return None

    try:
        return datetime.fromisoformat(ship_by_str.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        # Try plain date
        try:
            return date.fromisoformat(ship_by_str[:10])
        except ValueError:
            return None


def extract_line_item_ids(order: dict) -> list[str]:
    return [li["lineItemId"] for li in order.get("lineItems", []) if "lineItemId" in li]


def extract_isbn(order: dict) -> str:
    items = order.get("lineItems", [])
    if items:
        sku = items[0].get("sku", "")
        if sku.isdigit() and len(sku) == 13:
            return sku
    return ""


def extract_title(order: dict) -> str:
    items = order.get("lineItems", [])
    return items[0].get("title", "") if items else ""


# ── Mark Shipped (no tracking) ───────────────────────────────
def mark_shipped_no_tracking(token: str, order_id: str, line_item_ids: list[str]) -> dict:
    """
    Creates a shipping_fulfillment WITHOUT tracking number or carrier.
    eBay accepts this — buyer sees 'Shipped' with no tracking info.
    DOES NOT purchase any shipping label.
    """
    url = f"{EBAY_FULFILL_API}/order/{order_id}/shipping_fulfillment"
    body = {
        "lineItems": [{"lineItemId": lid, "quantity": 1} for lid in line_item_ids],
        "shippedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    r = requests.post(url, headers=ebay_headers(token), json=body, timeout=30)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"mark_shipped failed for {order_id}: {r.status_code} {r.text}")

    fulfillment_id = None
    loc = r.headers.get("Location", "")
    if loc:
        fulfillment_id = loc.rstrip("/").rsplit("/", 1)[-1]

    return {"fulfillment_id": fulfillment_id, "status": r.status_code}


# ── State persistence ────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"shipped_orders": {}}


def save_state(st: dict):
    STATE_FILE.write_text(json.dumps(st, indent=2))


# ── Email alerts ─────────────────────────────────────────────
def send_alert(subject: str, body: str):
    if not SMTP_USER or not SMTP_PASSWORD:
        log.warning("SMTP not configured — skipping alert email")
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = EMAIL_TO
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        log.error(f"Failed to send alert: {e}")


# ── Main ─────────────────────────────────────────────────────
def main() -> int:
    log.info("=" * 60)
    log.info("ship_deadline_guard.py started")

    if not EBAY_APP_ID or not EBAY_CERT_ID or not EBAY_REFRESH_TOKEN:
        log.error("Missing eBay credentials")
        return 1

    token = get_ebay_token()
    orders = fetch_awaiting_orders(token)
    log.info(f"Found {len(orders)} awaiting-shipment orders")

    today = date.today()
    cutoff = today + timedelta(days=GUARD_DAYS_BEFORE)
    state = load_state()
    fired = []
    skipped = []
    errors = []

    for order in orders:
        order_id = order.get("orderId", "")
        ship_by = parse_ship_by(order)

        if not ship_by:
            continue

        # Already handled by this guard in a previous run
        if order_id in state["shipped_orders"]:
            skipped.append(order_id)
            continue

        # Not yet at deadline
        if ship_by > cutoff:
            continue

        isbn = extract_isbn(order)
        title = extract_title(order)
        line_item_ids = extract_line_item_ids(order)

        if not line_item_ids:
            log.warning(f"Order {order_id}: no line items — skipping")
            continue

        try:
            resp = mark_shipped_no_tracking(token, order_id, line_item_ids)
            state["shipped_orders"][order_id] = {
                "isbn": isbn,
                "title": title[:80],
                "ship_by": ship_by.isoformat(),
                "fired_at": datetime.now(timezone.utc).isoformat(),
                "fulfillment_id": resp.get("fulfillment_id"),
            }
            fired.append({
                "order_id": order_id,
                "isbn": isbn,
                "title": title[:50],
                "ship_by": ship_by.isoformat(),
            })
            log.info(f"EMERGENCY SHIPPED: {order_id} (ISBN {isbn}, ship_by={ship_by})")
        except Exception as e:
            log.error(f"FAILED to mark shipped {order_id}: {e}")
            errors.append({"order_id": order_id, "isbn": isbn, "error": str(e)})

    save_state(state)

    # Summary
    log.info("=" * 60)
    log.info(f"DONE: {len(fired)} emergency-shipped | {len(skipped)} already done | {len(errors)} errors")
    log.info("=" * 60)

    # Alert email if any orders were emergency-shipped
    if fired:
        lines = [
            "EMERGENCY MARK-SHIPPED FIRED",
            f"Date: {today.isoformat()}",
            f"Cutoff: ship_by <= {cutoff.isoformat()}",
            "",
            "The following orders were marked shipped on eBay WITHOUT tracking:",
            "",
        ]
        for f in fired:
            lines.append(f"  Order: {f['order_id']}")
            lines.append(f"  ISBN:  {f['isbn']}")
            lines.append(f"  Title: {f['title']}")
            lines.append(f"  Ship by: {f['ship_by']}")
            lines.append("")

        lines.extend([
            "NEXT STEPS:",
            "  1. Verify each order was placed on BooksGoat.",
            "  2. shipping_tracker.py will add real tracking when BooksGoat ships.",
            "  3. If BooksGoat never ships, refund the buyer manually.",
            "",
            "No shipping labels were purchased. Only eBay status was updated.",
        ])
        send_alert(
            f"[EMERGENCY] {len(fired)} order(s) marked shipped without tracking",
            "\n".join(lines),
        )

    if errors:
        err_lines = ["DEADLINE GUARD ERRORS", ""]
        for e in errors:
            err_lines.append(f"  {e['order_id']} (ISBN {e['isbn']}): {e['error']}")
        send_alert(
            f"[ERROR] ship_deadline_guard had {len(errors)} failures",
            "\n".join(err_lines),
        )

    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
