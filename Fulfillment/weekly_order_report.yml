#!/usr/bin/env python3
"""
weekly_order_report.py — Weekly eBay order summary report.

Fetches orders from the last 7 days via eBay Orders API.
Cross-references with BooksGoat merchant sheet for cost/profit data.
Sends an HTML email summary.

Schedule: Monday 2PM UTC (10AM EST) via GitHub Actions
          weekly_order_report.yml

Requires: EBAY_APP_ID, EBAY_CERT_ID, EBAY_REFRESH_TOKEN,
          BOOKSGOAT_CSV_URL, SMTP_*, EMAIL_TO
"""

import os, csv, base64, logging, requests, io
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from email_helpers import (
        _email_wrapper, _summary_bar, _table_header, _table_row,
        _badge, send_html_email
    )
    HAS_EMAIL_HELPERS = True
except ImportError:
    HAS_EMAIL_HELPERS = False

EBAY_APP_ID        = os.environ.get('EBAY_APP_ID', '')
EBAY_CERT_ID       = os.environ.get('EBAY_CERT_ID', '')
EBAY_REFRESH_TOKEN = os.environ.get('EBAY_REFRESH_TOKEN', '')
BOOKSGOAT_CSV_URL  = os.environ.get('BOOKSGOAT_CSV_URL', '')

EBAY_FEE_RATE      = 0.153
LOOKBACK_DAYS      = 7
LOG_FILE           = 'weekly_order_report.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


# ── eBay OAuth ───────────────────────────────────────────────────────────────
def get_ebay_token() -> str:
    creds = base64.b64encode(f'{EBAY_APP_ID}:{EBAY_CERT_ID}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type': 'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': ('https://api.ebay.com/oauth/api_scope '
                      'https://api.ebay.com/oauth/api_scope/sell.fulfillment'),
        }
    )
    data = r.json()
    if 'access_token' not in data:
        raise RuntimeError(f'Token error: {data}')
    return data['access_token']


# ── BooksGoat cost lookup ────────────────────────────────────────────────────
def load_booksgoat_costs() -> dict:
    """Load ISBN -> cost mapping from BooksGoat merchant sheet (5-qty price)."""
    costs = {}
    if not BOOKSGOAT_CSV_URL:
        log.warning('BOOKSGOAT_CSV_URL not set — profit calculations will be unavailable')
        return costs
    try:
        r = requests.get(BOOKSGOAT_CSV_URL, timeout=15)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            isbn = row.get('ISBN-13', '').strip().replace('-', '')
            price_5 = row.get('5 Qty', '').strip().replace('$', '').replace(',', '')
            if isbn and price_5:
                try:
                    costs[isbn] = float(price_5)
                except ValueError:
                    pass
        log.info(f'Loaded {len(costs)} costs from BooksGoat sheet')
    except Exception as e:
        log.error(f'Failed to load BooksGoat sheet: {e}')
    return costs


# ── Also load from local CSV if available ────────────────────────────────────
def load_csv_costs() -> dict:
    """Load ISBN -> cost from booksgoat_enhanced.csv as fallback."""
    costs = {}
    csv_path = Path('booksgoat_enhanced.csv')
    if not csv_path.exists():
        return costs
    try:
        with csv_path.open(encoding='utf-8') as f:
            for row in csv.DictReader(f):
                isbn = row.get('isbn13', '').strip()
                cost = row.get('cost', '').strip()
                if isbn and cost:
                    try:
                        costs[isbn] = float(cost)
                    except ValueError:
                        pass
    except Exception:
        pass
    return costs


# ── Fetch eBay orders ────────────────────────────────────────────────────────
def fetch_recent_orders(token: str) -> list:
    """Fetch all orders from the last LOOKBACK_DAYS days."""
    headers = {'Authorization': f'Bearer {token}'}
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%dT00:00:00.000Z')

    all_orders = []
    offset = 0
    while True:
        params = {
            'filter': f'creationdate:[{since}..]',
            'limit': 200,
            'offset': offset,
        }
        try:
            r = requests.get(
                'https://api.ebay.com/sell/fulfillment/v1/order',
                headers=headers, params=params, timeout=15
            )
        except Exception as e:
            log.error(f'Orders API request failed: {e}')
            break

        if r.status_code != 200:
            log.error(f'Orders API error: {r.status_code} {r.text[:200]}')
            break

        data = r.json()
        orders = data.get('orders', [])
        all_orders.extend(orders)

        total = data.get('total', 0)
        offset += len(orders)
        if offset >= total or not orders:
            break

    log.info(f'Fetched {len(all_orders)} orders from last {LOOKBACK_DAYS} days')
    return all_orders


# ── Process orders ───────────────────────────────────────────────────────────
def process_orders(orders: list, costs: dict) -> dict:
    """Extract order details and compute profit."""
    processed = []
    total_revenue = 0.0
    total_profit = 0.0
    total_cost = 0.0
    fulfilled_count = 0
    unfulfilled_count = 0

    for order in orders:
        order_id = order.get('orderId', '')
        creation = order.get('creationDate', '')[:10]
        status = order.get('orderFulfillmentStatus', 'UNKNOWN')

        if status in ('FULFILLED',):
            fulfilled_count += 1
        else:
            unfulfilled_count += 1

        for item in order.get('lineItems', []):
            title = item.get('title', '')[:45]
            sku = item.get('sku', '')
            qty = item.get('quantity', 1)
            price_str = item.get('lineItemCost', {}).get('value', '0')
            try:
                sale_price = float(price_str)
            except ValueError:
                sale_price = 0.0

            cost = costs.get(sku, 0.0)
            ebay_fee = sale_price * EBAY_FEE_RATE
            profit = sale_price - cost - ebay_fee if cost > 0 else 0.0

            total_revenue += sale_price
            total_cost += cost
            total_profit += profit

            processed.append({
                'order_id': order_id,
                'date': creation,
                'isbn': sku,
                'title': title,
                'qty': qty,
                'sale_price': sale_price,
                'cost': cost,
                'profit': profit,
                'status': status,
            })

    return {
        'items': processed,
        'total_revenue': total_revenue,
        'total_cost': total_cost,
        'total_profit': total_profit,
        'fulfilled': fulfilled_count,
        'unfulfilled': unfulfilled_count,
        'order_count': len(orders),
    }


# ── Build HTML email ─────────────────────────────────────────────────────────
def build_order_report_html(data: dict) -> str:
    """Build HTML email body for the weekly order report."""
    items = data['items']

    summary = _summary_bar([
        ("Orders", str(data['order_count']), "blue"),
        ("Revenue", f"${data['total_revenue']:.2f}", "green"),
        ("Profit", f"${data['total_profit']:.2f}", "green" if data['total_profit'] > 0 else "red"),
        ("Unfulfilled", str(data['unfulfilled']), "orange" if data['unfulfilled'] > 0 else "gray"),
    ])

    parts = []

    # Revenue summary table
    parts.append(
        f'<h3 style="margin:0 0 8px;font-size:14px;color:#1a1a2e;">Financial Summary (Last {LOOKBACK_DAYS} Days)</h3>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
        f'{_table_header(["Metric", "Amount"])}'
        f'{_table_row(["Gross Revenue", f"${data["total_revenue"]:.2f}"], 0)}'
        f'{_table_row(["BooksGoat Cost", f"${data["total_cost"]:.2f}"], 1)}'
        f'{_table_row(["eBay Fees (15.3%)", f"${data["total_revenue"] * EBAY_FEE_RATE:.2f}"], 2)}'
        f'{_table_row(["Net Profit", f"${data["total_profit"]:.2f}"], 3)}'
        f'{_table_row(["Fulfilled", str(data["fulfilled"])], 4)}'
        f'{_table_row(["Unfulfilled", str(data["unfulfilled"])], 5)}'
        f'</table>'
    )

    # Order detail table
    if items:
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#1565c0;">Order Details ({len(items)} Line Items)</h3>'
        )
        rows = ""
        for i, item in enumerate(items):
            status_color = "green" if item['status'] == 'FULFILLED' else "orange"
            profit_display = f"${item['profit']:.2f}" if item['cost'] > 0 else "N/A"
            rows += _table_row([
                item['date'],
                f'<code style="font-size:11px;">{item["isbn"]}</code>',
                item['title'],
                f'${item["sale_price"]:.2f}',
                f'${item["cost"]:.2f}' if item['cost'] > 0 else "N/A",
                profit_display,
                _badge(item['status'].replace('_', ' '), status_color),
            ], i)
        parts.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
            f'{_table_header(["Date", "ISBN", "Title", "Sale", "Cost", "Profit", "Status"])}'
            f'{rows}</table>'
        )
    else:
        parts.append('<p style="font-size:14px;color:#666;">No orders in the last 7 days.</p>')

    return _email_wrapper(
        "WEEKLY ORDER REPORT",
        summary,
        "\n".join(parts),
    )


# ── Fallback plain text ─────────────────────────────────────────────────────
def build_plain_text_report(data: dict) -> str:
    """Plain text fallback if email_helpers is unavailable."""
    lines = [
        f"Weekly Order Report — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        "=" * 55,
        f"  Orders:      {data['order_count']}",
        f"  Revenue:     ${data['total_revenue']:.2f}",
        f"  Cost:        ${data['total_cost']:.2f}",
        f"  eBay Fees:   ${data['total_revenue'] * EBAY_FEE_RATE:.2f}",
        f"  Net Profit:  ${data['total_profit']:.2f}",
        f"  Fulfilled:   {data['fulfilled']}",
        f"  Unfulfilled: {data['unfulfilled']}",
        "",
    ]
    for item in data['items']:
        profit_str = f"${item['profit']:.2f}" if item['cost'] > 0 else "N/A"
        lines.append(
            f"  {item['date']} | {item['isbn']} | {item['title'][:35]} | "
            f"${item['sale_price']:.2f} | Profit: {profit_str} | {item['status']}"
        )
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────
def run():
    log.info('=' * 60)
    log.info(f'WEEKLY ORDER REPORT — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}')
    log.info('=' * 60)

    # Load costs from both sources, merchant sheet takes priority
    csv_costs = load_csv_costs()
    bg_costs = load_booksgoat_costs()
    costs = {**csv_costs, **bg_costs}  # merchant sheet overwrites CSV
    log.info(f'Total cost entries: {len(costs)}')

    token = get_ebay_token()
    orders = fetch_recent_orders(token)

    data = process_orders(orders, costs)

    log.info(f'Revenue: ${data["total_revenue"]:.2f} | '
             f'Profit: ${data["total_profit"]:.2f} | '
             f'Orders: {data["order_count"]}')

    # Send email
    run_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    subject = (f"[Order Report] {run_date} | {data['order_count']} orders | "
               f"${data['total_revenue']:.2f} revenue | ${data['total_profit']:.2f} profit")

    if HAS_EMAIL_HELPERS:
        html = build_order_report_html(data)
        send_html_email(subject, html)
    else:
        # Fallback plain text
        import smtplib
        from email.mime.text import MIMEText
        smtp_user = os.environ.get('SMTP_USER', '')
        smtp_pass = os.environ.get('SMTP_PASSWORD', '')
        email_to = os.environ.get('EMAIL_TO', '')
        if all([smtp_user, smtp_pass, email_to]):
            body = build_plain_text_report(data)
            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = os.environ.get('EMAIL_FROM', smtp_user)
            msg['To'] = email_to
            try:
                with smtplib.SMTP(os.environ.get('SMTP_HOST', 'smtp.gmail.com'),
                                  int(os.environ.get('SMTP_PORT', '587'))) as s:
                    s.starttls()
                    s.login(smtp_user, smtp_pass)
                    s.sendmail(msg['From'], [email_to], msg.as_string())
                log.info('Report email sent (plain text fallback)')
            except Exception as e:
                log.error(f'Email failed: {e}')

    log.info('=' * 60)
    log.info('DONE')


if __name__ == '__main__':
    run()
