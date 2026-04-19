#!/usr/bin/env python3
"""
order_monitor.py — eBay order detection and fulfillment alert
Location: E:\\Book\\Scanner\\order_monitor.py (runs on GitHub Actions)

Polls eBay Orders API for new orders, sends email with:
- Buyer name + shipping address
- ISBN + title
- BooksGoat product URL (from lister_state.json)
- Suggested order price (from lister_state.json cost)

Tracks processed orders in orders_processed.json to avoid duplicate alerts.
"""

import os, base64, json, smtplib, requests
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# ── Config ────────────────────────────────────────────────────
EBAY_APP_ID        = os.environ["EBAY_CLIENT_ID"]
EBAY_CERT_ID       = os.environ["EBAY_CLIENT_SECRET"]
EBAY_REFRESH_TOKEN = os.environ["EBAY_REFRESH_TOKEN"]

SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM    = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO      = os.environ.get("EMAIL_TO", SMTP_USER)

STATE_FILE    = Path("orders_processed.json")
STATE_FILE_LS = Path("lister_state.json")

# ── Auth ──────────────────────────────────────────────────────
def get_token():
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=(
            "grant_type=refresh_token"
            f"&refresh_token={EBAY_REFRESH_TOKEN}"
            "&scope=https://api.ebay.com/oauth/api_scope/sell.fulfillment"
            " https://api.ebay.com/oauth/api_scope/sell.inventory"
        ),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]

# ── State ─────────────────────────────────────────────────────
def load_processed():
    if STATE_FILE.exists():
        return set(json.load(STATE_FILE.open()))
    return set()

def save_processed(ids: set):
    json.dump(list(ids), STATE_FILE.open("w"), indent=2)

def load_lister_state():
    if STATE_FILE_LS.exists():
        return json.load(STATE_FILE_LS.open())
    return {}

# ── eBay Orders API ───────────────────────────────────────────
def fetch_recent_orders(token: str) -> list:
    """Fetch orders from last 24 hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    orders = []
    offset = 0
    while True:
        r = requests.get(
            "https://api.ebay.com/sell/fulfillment/v1/order",
            headers=headers,
            params={
                "filter": f"creationdate:[{since}]",
                "limit": 50,
                "offset": offset,
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"Orders API error {r.status_code}: {r.text[:200]}")
            break
        data = r.json()
        batch = data.get("orders", [])
        orders.extend(batch)
        if len(orders) >= data.get("total", 0) or not batch:
            break
        offset += 50
    return orders

# ── Email ─────────────────────────────────────────────────────
def send_order_alert(orders: list, lister_state: dict):
    if not orders:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📦 BooksGoat Order Alert — {len(orders)} new order{'s' if len(orders) > 1 else ''}"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO

    text_parts = []
    for order in orders:
        order_id   = order.get("orderId", "N/A")
        created    = order.get("creationDate", "")[:10]
        total      = order.get("pricingSummary", {}).get("total", {})
        sale_price = f"${float(total.get('value', 0)):.2f}" if total else "N/A"

        # Buyer shipping info
        ship = order.get("fulfillmentStartInstructions", [{}])[0]
        ship_to = ship.get("shippingStep", {}).get("shipTo", {})
        buyer_name = ship_to.get("fullName", "N/A")
        phone      = ship_to.get("primaryPhone", {}).get("phoneNumber", "N/A")
        addr       = ship_to.get("contactAddress", {})
        address_lines = "\n    ".join(filter(None, [
            addr.get("addressLine1", ""),
            addr.get("addressLine2", ""),
            f"{addr.get('city', '')}, {addr.get('stateOrProvince', '')} {addr.get('postalCode', '')}",
            addr.get("countryCode", ""),
        ]))

        # Line items
        items_text = []
        for item in order.get("lineItems", []):
            title  = item.get("title", "N/A")
            sku    = item.get("sku", "")
            qty    = item.get("quantity", 1)
            price  = item.get("lineItemCost", {}).get("value", "N/A")

            # Look up BooksGoat URL and cost from lister_state
            state_entry = lister_state.get(sku, {})
            bg_url  = state_entry.get("product_url", "Not found in state")
            bg_cost = state_entry.get("cost", "N/A")

            items_text.append(f"""
  ITEM: {title}
  SKU/ISBN:    {sku}
  Qty:         {qty}
  Sale price:  ${price}
  
  ── ORDER ON BOOKSGOAT ──
  URL:         {bg_url}
  Your cost:   ${bg_cost}
  Ship to:     (see below)
""")

        block = f"""
{'='*60}
ORDER ID:   {order_id}
Date:       {created}
Sale total: {sale_price}
{'='*60}

SHIP TO:
  {buyer_name}
  Phone: {phone}
  {address_lines}

{''.join(items_text)}
"""
        text_parts.append(block)

    body = f"""BooksGoat New Order Alert
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}
{''.join(text_parts)}

---
To fulfill: visit the BooksGoat URL above, add to cart, 
enter the buyer's shipping address, and pay with your card.
"""

    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    print(f"Alert sent: {len(orders)} order(s)")

# ── Main ──────────────────────────────────────────────────────
def run():
    print(f"Order monitor started | {datetime.now().isoformat()}")
    token        = get_token()
    processed    = load_processed()
    lister_state = load_lister_state()

    orders = fetch_recent_orders(token)
    print(f"Fetched {len(orders)} orders from last 24h")

    new_orders = [o for o in orders if o["orderId"] not in processed]
    print(f"New unprocessed orders: {len(new_orders)}")

    if new_orders:
        send_order_alert(new_orders, lister_state)
        for o in new_orders:
            processed.add(o["orderId"])
        save_processed(processed)
    else:
        print("No new orders — no alert sent")

if __name__ == "__main__":
    run()
