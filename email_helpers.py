"""
email_helpers.py — Shared HTML email templates for BooksGoat pipeline.

Location: repo root (njubran82/Scanner/email_helpers.py)
Also copy to: E:\\Book\\Lister\\email_helpers.py (for fix_listings.py if needed)

All pipeline scripts import from this module instead of building
their own plain-text emails.

Design:
  - Inline CSS only (Gmail strips <style> blocks)
  - Max-width 600px, mobile-friendly font sizes (≥13px)
  - Header: #1a1a2e dark bar with white text
  - Summary: colored stat boxes
  - Tables: alternating row colors
  - Footer: Jubran Industries LLC branding
"""

import os
import smtplib
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# BUILDING BLOCKS
# ═══════════════════════════════════════════════════════════════════════════════

def _email_wrapper(header_title, summary_html, body_html, footer_extra=""):
    """Wrap content in the standard Jubran Industries email shell."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;">
<tr><td align="center" style="padding:20px 10px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

<!-- HEADER -->
<tr><td style="background:#1a1a2e;padding:20px 24px;">
  <h1 style="margin:0;color:#ffffff;font-size:16px;font-weight:600;letter-spacing:0.5px;">{header_title}</h1>
  <p style="margin:6px 0 0;color:#a0a0b8;font-size:12px;">{ts}</p>
</td></tr>

<!-- SUMMARY BAR -->
<tr><td style="padding:16px 24px;background:#fafafa;border-bottom:1px solid #eee;">
  {summary_html}
</td></tr>

<!-- BODY -->
<tr><td style="padding:20px 24px;">
  {body_html}
</td></tr>

<!-- FOOTER -->
<tr><td style="background:#f9f9fb;padding:16px 24px;border-top:1px solid #eee;">
  {f'<p style="margin:0 0 10px;font-size:12px;color:#666;">{footer_extra}</p>' if footer_extra else ''}
  <p style="margin:0;font-size:11px;color:#999;text-align:center;">
    Jubran Industries LLC &middot; atlas_commerce &middot;
    <a href="mailto:jubran.industries@gmail.com" style="color:#999;">jubran.industries@gmail.com</a>
  </p>
</td></tr>

</table>
</td></tr></table>
</body></html>"""


def _stat_box(label, value, color):
    bg = {"green":"#e8f5e9","orange":"#fff3e0","red":"#ffebee","blue":"#e3f2fd","gray":"#f5f5f5"}
    fg = {"green":"#2e7d32","orange":"#e65100","red":"#c62828","blue":"#1565c0","gray":"#616161"}
    return (
        f'<td style="padding:8px 12px;background:{bg.get(color,"#f5f5f5")};'
        f'border-radius:6px;text-align:center;min-width:80px;">'
        f'<div style="font-size:20px;font-weight:700;color:{fg.get(color,"#333")};">{value}</div>'
        f'<div style="font-size:11px;color:{fg.get(color,"#666")};margin-top:2px;">{label}</div>'
        f'</td>'
    )


def _summary_bar(stats):
    """stats = list of (label, value, color)."""
    cells = "".join(_stat_box(l, v, c) for l, v, c in stats)
    return f'<table role="presentation" cellpadding="0" cellspacing="6" style="width:100%;"><tr>{cells}</tr></table>'


def _table_header(columns):
    cells = "".join(
        f'<th style="padding:8px 10px;text-align:left;font-size:12px;font-weight:600;'
        f'color:#666;background:#f5f5f5;border-bottom:2px solid #ddd;'
        f'white-space:nowrap;">{c}</th>'
        for c in columns
    )
    return f'<tr>{cells}</tr>'


def _table_row(cells, idx=0):
    bg = "#ffffff" if idx % 2 == 0 else "#f9f9f9"
    tds = "".join(
        f'<td style="padding:7px 10px;font-size:13px;color:#333;border-bottom:1px solid #eee;'
        f'background:{bg};vertical-align:top;">{c}</td>'
        for c in cells
    )
    return f'<tr>{tds}</tr>'


def _badge(text, color):
    bg = {"green":"#e8f5e9","orange":"#fff3e0","red":"#ffebee","blue":"#e3f2fd"}
    fg = {"green":"#2e7d32","orange":"#e65100","red":"#c62828","blue":"#1565c0"}
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;'
        f'font-weight:600;background:{bg.get(color,"#f5f5f5")};'
        f'color:{fg.get(color,"#333")};">{text}</span>'
    )


def send_html_email(subject, html_body):
    """Send HTML email using standard pipeline SMTP env vars."""
    smtp_host = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USER', '')
    smtp_pass = os.environ.get('SMTP_PASSWORD', '')
    email_from = os.environ.get('EMAIL_FROM', smtp_user)
    email_to = os.environ.get('EMAIL_TO', '')

    if not all([smtp_user, smtp_pass, email_to]):
        log.warning("Email not configured — skipping")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = email_from
        msg["To"] = email_to
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(email_from, [email_to], msg.as_string())
        log.info(f"Email sent: {subject}")
        return True
    except Exception as e:
        log.error(f"Email failed: {e}")
        return False


def send_html_email_with_attachment(subject, html_body, attachment_text, attachment_filename):
    """Send HTML email with a .txt attachment."""
    smtp_host = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USER', '')
    smtp_pass = os.environ.get('SMTP_PASSWORD', '')
    email_from = os.environ.get('EMAIL_FROM', smtp_user)
    email_to = os.environ.get('EMAIL_TO', '')

    if not all([smtp_user, smtp_pass, email_to]):
        log.warning("Email not configured — skipping")
        return False
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = email_from
        msg["To"] = email_to

        html_part = MIMEMultipart("alternative")
        html_part.attach(MIMEText(html_body, "html"))
        msg.attach(html_part)

        if attachment_text:
            att = MIMEBase("application", "octet-stream")
            att.set_payload(attachment_text.encode("utf-8"))
            encoders.encode_base64(att)
            att.add_header("Content-Disposition", f'attachment; filename="{attachment_filename}"')
            msg.attach(att)

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(email_from, [email_to], msg.as_string())
        log.info(f"Email sent: {subject}")
        return True
    except Exception as e:
        log.error(f"Email failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# 1. LISTER.PY — Success + Failure alerts
# ═══════════════════════════════════════════════════════════════════════════════

def build_lister_success_email(listed_books):
    """Build HTML for successfully listed books."""
    summary = _summary_bar([
        ("Listed", str(len(listed_books)), "green"),
        ("Total Profit", f"${sum(b.get('profit',0) for b in listed_books):.2f}", "blue"),
    ])
    rows = ""
    for i, b in enumerate(listed_books):
        conf = b.get("confidence", "")
        conf_color = "green" if conf == "HIGH" else "orange" if conf == "MEDIUM" else "gray"
        rows += _table_row([
            str(i + 1),
            f'<code style="font-size:11px;">{b.get("isbn","")}</code>',
            b.get("title", "")[:50],
            f'${b.get("listing_price", 0):.2f}',
            f'${b.get("profit", 0):.2f}',
            _badge(conf, conf_color),
        ], i)
    body = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
        f'{_table_header(["#", "ISBN", "Title", "Price", "Profit", "Confidence"])}'
        f'{rows}</table>'
    )
    return _email_wrapper("LISTER — NEW LISTINGS", summary, body)


def build_lister_failure_email(failed_books):
    """Build HTML for books that failed to list."""
    summary = _summary_bar([
        ("Failed", str(len(failed_books)), "red"),
        ("Action Required", "Yes", "orange"),
    ])
    rows = ""
    for i, b in enumerate(failed_books):
        rows += _table_row([
            str(i + 1),
            f'<code style="font-size:11px;">{b.get("isbn","")}</code>',
            b.get("title", "")[:45],
            f'${b.get("listing_price", 0):.2f}',
            f'${b.get("profit", 0):.2f}',
            _badge(b.get("stage", "unknown"), "red"),
            f'<span style="font-size:11px;color:#c62828;">{b.get("error","")[:60]}</span>',
            f'<span style="font-size:11px;color:#e65100;">{b.get("action","Check Seller Hub")}</span>',
        ], i)
    body = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
        f'{_table_header(["#", "ISBN", "Title", "Price", "Profit", "Stage", "Error", "Action"])}'
        f'{rows}</table>'
    )
    footer = (
        "<strong>Manual steps:</strong> Seller Hub &rarr; Listings &rarr; search ISBN &rarr; "
        "Edit &rarr; fix the issue described above &rarr; Save &amp; Publish"
    )
    return _email_wrapper("LISTER FAILURE ALERT", summary, body, footer)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. FIX_IMAGES.PY — Run report
# ═══════════════════════════════════════════════════════════════════════════════

def build_fix_images_email(upgraded, still_missing):
    """Build HTML report for fix_images.py run."""
    total = len(upgraded) + len(still_missing)
    summary = _summary_bar([
        ("Upgraded", str(len(upgraded)), "green"),
        ("Still Missing", str(len(still_missing)), "orange" if still_missing else "green"),
        ("Total Checked", str(total), "blue"),
    ])
    parts = []

    if upgraded:
        rows = ""
        for i, u in enumerate(upgraded):
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{u["isbn"]}</code>',
                u["title"][:50],
                "&#10003; Upgraded",
            ], i)
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#2e7d32;">Images Upgraded ({len(upgraded)})</h3>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
            f'{_table_header(["#", "ISBN", "Title", "Status"])}{rows}</table>'
        )

    if still_missing:
        rows = ""
        for i, m in enumerate(still_missing):
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{m["isbn"]}</code>',
                m["title"][:50],
                _badge(f'was: {m.get("flag","missing")}', "orange"),
            ], i)
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#e65100;">Still Need Manual Photo ({len(still_missing)})</h3>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
            f'{_table_header(["#", "ISBN", "Title", "Previous Flag"])}{rows}</table>'
        )

    if not parts:
        parts.append('<p style="font-size:14px;color:#666;">No image candidates found this run.</p>')

    footer = "Manual upload: Seller Hub &rarr; Active &rarr; search ISBN &rarr; Edit &rarr; add photo &rarr; Save" if still_missing else ""
    return _email_wrapper("FIX IMAGES — RUN REPORT", summary, "\n".join(parts), footer)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. FIX_DESCRIPTIONS.PY — Run report
# ═══════════════════════════════════════════════════════════════════════════════

def build_fix_descriptions_email(updated, failed):
    """Build HTML report for fix_descriptions.py run."""
    total = len(updated) + len(failed)
    summary = _summary_bar([
        ("Updated", str(len(updated)), "green"),
        ("Failed", str(len(failed)), "red" if failed else "green"),
        ("Total Processed", str(total), "blue"),
    ])
    parts = []

    if updated:
        rows = ""
        for i, u in enumerate(updated):
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{u["isbn"]}</code>',
                u["title"][:50],
                "&#10003; Description added",
            ], i)
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#2e7d32;">Descriptions Added ({len(updated)})</h3>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
            f'{_table_header(["#", "ISBN", "Title", "Status"])}{rows}</table>'
        )

    if failed:
        rows = ""
        for i, f_item in enumerate(failed):
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{f_item["isbn"]}</code>',
                f_item["title"][:50],
                _badge("eBay PUT failed", "red"),
            ], i)
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#c62828;">eBay Update Failed ({len(failed)})</h3>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
            f'{_table_header(["#", "ISBN", "Title", "Status"])}{rows}</table>'
        )

    if not parts:
        parts.append('<p style="font-size:14px;color:#666;">No descriptions needed updating this run.</p>')

    return _email_wrapper("FIX DESCRIPTIONS — RUN REPORT", summary, "\n".join(parts))


# ═══════════════════════════════════════════════════════════════════════════════
# 4. SHIPPING_TRACKER.PY — Run report
# ═══════════════════════════════════════════════════════════════════════════════

def build_shipping_tracker_email(shipped, skipped, failed, tracking_enabled):
    """Build HTML report for shipping_tracker.py run."""
    summary = _summary_bar([
        ("Shipped", str(len(shipped)), "green"),
        ("Skipped", str(len(skipped)), "orange" if skipped else "gray"),
        ("Failed", str(len(failed)), "red" if failed else "gray"),
    ])

    if not tracking_enabled:
        banner_bg = "#ffebee"
        banner_border = "#ffcdd2"
        banner_color = "#c62828"
        banner_text = "&#9888;&#65039; TRACKING DISABLED &#8212; Marking shipped without tracking numbers. Re-enable after auditing matching logic."
    else:
        banner_bg = "#e8f5e9"
        banner_border = "#c8e6c9"
        banner_color = "#2e7d32"
        banner_text = "&#10003; Tracking enabled &#8212; posting tracking numbers to eBay."

    tracking_banner = (
        f'<div style="padding:10px 14px;border-radius:6px;margin-bottom:12px;'
        f'background:{banner_bg};border:1px solid {banner_border};'
        f'font-size:13px;color:{banner_color};font-weight:600;">'
        f'{banner_text}</div>'
    )

    parts = [tracking_banner]

    if shipped:
        rows = ""
        for i, s in enumerate(shipped):
            tracking_val = s.get("tracking", "N/A") if tracking_enabled else "&mdash;"
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{s.get("isbn","")}</code>',
                s.get("title", "")[:45],
                s.get("order_id", ""),
                tracking_val,
                "&#10003; Shipped",
            ], i)
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#2e7d32;">Marked Shipped ({len(shipped)})</h3>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
            f'{_table_header(["#", "ISBN", "Title", "Order ID", "Tracking", "Status"])}{rows}</table>'
        )

    if skipped:
        rows = ""
        for i, s in enumerate(skipped):
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{s.get("isbn","")}</code>',
                s.get("title", "")[:45],
                s.get("reason", ""),
            ], i)
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#e65100;">Skipped ({len(skipped)})</h3>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
            f'{_table_header(["#", "ISBN", "Title", "Reason"])}{rows}</table>'
        )

    if failed:
        rows = ""
        for i, f_item in enumerate(failed):
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{f_item.get("isbn","")}</code>',
                f_item.get("title", "")[:45],
                f_item.get("order_id", ""),
                f'<span style="color:#c62828;font-size:11px;">{f_item.get("error","")[:50]}</span>',
            ], i)
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#c62828;">Failed ({len(failed)})</h3>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
            f'{_table_header(["#", "ISBN", "Title", "Order ID", "Error"])}{rows}</table>'
        )

    if not shipped and not skipped and not failed:
        parts.append('<p style="font-size:14px;color:#666;">No shipping emails found this run.</p>')

    return _email_wrapper("SHIPPING TRACKER — RUN REPORT", summary, "\n".join(parts))


# ═══════════════════════════════════════════════════════════════════════════════
# 5. WEEKLY_SUMMARY.PY — Full pipeline report
# ═══════════════════════════════════════════════════════════════════════════════

def build_weekly_summary_email(
    active_count, pending_count, total_csv,
    opportunities,   # list of dicts: isbn, title, cost, sell_price, profit, confidence
    repriced,        # list of dicts: isbn, title, old_price, new_price, profit
    delisted,        # list of dicts: isbn, title, reason, profit
    scanner_errors=0, lister_errors=0, repricer_errors=0,
):
    """Build full HTML body for the weekly pipeline summary."""
    opp_count = len(opportunities)
    repriced_count = len(repriced)
    delist_count = len(delisted)

    summary = _summary_bar([
        ("Active", str(active_count), "green"),
        ("Opportunities", str(opp_count), "blue" if opp_count else "gray"),
        ("Repriced", str(repriced_count), "green" if repriced_count else "gray"),
        ("Delisted", str(delist_count), "orange" if delist_count else "gray"),
    ])

    parts = []

    # ── Inventory snapshot ──
    parts.append(
        f'<h3 style="margin:0 0 8px;font-size:14px;color:#1a1a2e;">Inventory Snapshot</h3>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
        f'{_table_header(["Metric", "Count"])}'
        f'{_table_row(["Active listings", str(active_count)], 0)}'
        f'{_table_row(["Pending", str(pending_count)], 1)}'
        f'{_table_row(["Total in CSV", str(total_csv)], 2)}'
        f'</table>'
    )

    # ── Scanner opportunities ──
    parts.append(f'<h3 style="margin:0 0 8px;font-size:14px;color:#1565c0;">Scanner — {opp_count} Opportunities</h3>')
    if opportunities:
        rows = ""
        for i, o in enumerate(opportunities):
            c = o.get("confidence", "")
            cc = "green" if c == "HIGH" else "orange" if c == "MEDIUM" else "gray"
            rows += _table_row([
                str(i + 1),
                f'<code style="font-size:11px;">{o.get("isbn","")}</code>',
                o.get("title", "")[:40],
                f'${o.get("cost", 0):.2f}',
                f'${o.get("sell_price", 0):.2f}',
                f'<strong style="color:#2e7d32;">${o.get("profit", 0):.2f}</strong>',
                _badge(c, cc),
            ], i)
        parts.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
            f'{_table_header(["#", "ISBN", "Title", "Cost", "List", "Profit", "Conf."])}{rows}</table>'
        )
    else:
        parts.append('<p style="font-size:13px;color:#666;margin:0 0 16px;">No new opportunities found.</p>')

    # ── Repricer ──
    parts.append(f'<h3 style="margin:0 0 8px;font-size:14px;color:#2e7d32;">Repricer — {repriced_count} Price Updates</h3>')
    if repriced:
        rows = ""
        for i, r in enumerate(repriced):
            delta = r.get("new_price", 0) - r.get("old_price", 0)
            arrow = "&uarr;" if delta > 0 else "&darr;"
            dc = "#2e7d32" if delta > 0 else "#c62828"
            rows += _table_row([
                str(i + 1),
                r.get("title", "")[:40],
                f'${r.get("old_price", 0):.2f}',
                f'${r.get("new_price", 0):.2f}',
                f'<span style="color:{dc};font-weight:600;">{arrow} ${abs(delta):.2f}</span>',
                f'${r.get("profit", 0):.2f}',
            ], i)
        parts.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
            f'{_table_header(["#", "Title", "Old", "New", "Change", "Profit"])}{rows}</table>'
        )
    else:
        parts.append('<p style="font-size:13px;color:#666;margin:0 0 16px;">No repricing needed.</p>')

    # ── Delistings ──
    if delisted:
        parts.append(f'<h3 style="margin:0 0 8px;font-size:14px;color:#e65100;">Delisted — {delist_count} Books</h3>')
        rows = ""
        for i, d in enumerate(delisted):
            reason = d.get("reason", "")
            rc = "orange" if reason == "unprofitable" else "red"
            rows += _table_row([
                str(i + 1),
                d.get("title", "")[:45],
                _badge(reason, rc),
                f'${d.get("profit", 0):.2f}',
            ], i)
        parts.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
            f'{_table_header(["#", "Title", "Reason", "Last Profit"])}{rows}</table>'
        )

    # ── Errors ──
    total_errors = scanner_errors + lister_errors + repricer_errors
    if total_errors > 0:
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:14px;color:#c62828;">Errors — {total_errors} Total</h3>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;margin-bottom:16px;">'
            f'{_table_header(["Component", "Errors"])}'
            f'{_table_row(["Scanner", str(scanner_errors)], 0)}'
            f'{_table_row(["Lister", str(lister_errors)], 1)}'
            f'{_table_row(["Repricer", str(repricer_errors)], 2)}'
            f'</table>'
        )

    # ── Workflow reference table ──
    parts.append(
        f'<h3 style="margin:0 0 8px;font-size:14px;color:#1a1a2e;">Workflow Reference</h3>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid #eee;border-radius:6px;overflow:hidden;">'
        f'{_table_header(["Workflow", "Schedule", "Purpose"])}'
        f'{_table_row(["Weekly Scanner", "Mon 9AM EST", "Scan + list + reprice + this email"], 0)}'
        f'{_table_row(["Order Monitor", "Every 2h", "Detect sales, email fulfillment details"], 1)}'
        f'{_table_row(["Fix Images", "Wed 11AM EST", "Upgrade thumbnail images to full quality"], 2)}'
        f'{_table_row(["Fix Descriptions", "Manual", "Generate AI descriptions for missing books"], 3)}'
        f'{_table_row(["Shipping Tracker", "Every 2h", "Parse shipping emails, mark shipped on eBay"], 4)}'
        f'{_table_row(["Local Tracker", "Daily 7AM (Win)", "OOS detection + price monitoring"], 5)}'
        f'{_table_row(["Scraper", "Mon 6AM (Win)", "Discover new books from BooksGoat categories"], 6)}'
        f'</table>'
    )

    return _email_wrapper("WEEKLY PIPELINE SUMMARY", summary, "\n".join(parts))
