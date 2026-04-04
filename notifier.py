"""
notifier.py — Two distinct email types + SMS.

IMMEDIATE ALERT  (send_immediate_alert)
    Fires only when new or significantly improved opportunities appear.
    Subject flags NEW / SIGNIFICANT clearly.
    CSV attached.
    Can fire multiple times per day if warranted — but only when
    the scanner genuinely finds something worth acting on.

DAILY SUMMARY    (send_daily_summary)
    Fires once per day (gated by state_tracker.should_send_daily_summary).
    Shows ALL current opportunities grouped by confidence level.
    Top 5 by profit highlighted.
    Count breakdown: HIGH / MEDIUM / LOW / FALLBACK.
    CSV attached.

SMS              (send_sms)
    Compact alert for new/significant only.
    Confidence tag and profit per book.
    Capped at 10 books to avoid Twilio length errors.
"""

import smtplib
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Optional
from datetime import datetime
from collections import Counter

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    Opportunity,
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
    CONFIDENCE_FALLBACK, CONFIDENCE_NONE,
)
import config

logger = logging.getLogger(__name__)


# ── Styling constants ─────────────────────────────────────────────────────────

_CONF_STYLE = {
    CONFIDENCE_HIGH:     ("#dcfce7", "#15803d", "🟢 HIGH"),
    CONFIDENCE_MEDIUM:   ("#fef9c3", "#854d0e", "🟡 MEDIUM"),
    CONFIDENCE_LOW:      ("#ffedd5", "#9a3412", "🟠 LOW"),
    CONFIDENCE_FALLBACK: ("#fee2e2", "#991b1b", "🔴 FALLBACK"),
    CONFIDENCE_NONE:     ("#f3f4f6", "#6b7280", "⚪ NONE"),
}

_MODE_STYLE = {
    "EBAY_CONFIRMED": ("#dcfce7", "#15803d", "🟢 eBay Confirmed"),
    "MIXED":          ("#fef9c3", "#854d0e", "🟡 Mixed Mode"),
    "FALLBACK_ONLY":  ("#fee2e2", "#991b1b", "🔴 Fallback Only — check EBAY_APP_ID"),
}

_SOURCE_LABEL = {
    "ebay_sold":       "eBay sold",
    "ebay_active":     "eBay active",
    "amazon_estimate": "Amazon~",
}


# ── Shared HTML helpers ───────────────────────────────────────────────────────

def _conf_badge(conf: str) -> str:
    bg, color, label = _CONF_STYLE.get(conf, ("#f3f4f6", "#374151", conf))
    return (
        f'<span style="background:{bg};color:{color};padding:2px 6px;'
        f'border-radius:4px;font-size:10px;font-weight:bold">{label}</span>'
    )


def _mode_banner(api_health: dict) -> str:
    run_mode = api_health.get("run_mode", "UNKNOWN")
    bg, color, label = _MODE_STYLE.get(run_mode, ("#f3f4f6", "#374151", run_mode))
    reason = api_health.get("run_mode_reason", "")
    extra = ""
    if run_mode == "FALLBACK_ONLY":
        extra = (
            "<br><strong>Action required:</strong> Check EBAY_APP_ID in "
            ".env / GitHub Secrets. Results in this run are estimates only."
        )
    return (
        f'<div style="background:{bg};color:{color};padding:8px 12px;'
        f'border-radius:5px;margin-bottom:10px;font-size:12px">'
        f'<strong>{label}</strong>'
        + (f"<br>{reason}" if reason and run_mode == "MIXED" else "")
        + extra
        + '</div>'
    )


def _opp_table_row(opp: Opportunity, status_label: str, row_bg: str) -> str:
    roi    = opp.profit / opp.book.cost * 100 if opp.book.cost else 0
    src    = _SOURCE_LABEL.get(opp.revenue_source, opp.revenue_source)
    flags  = (
        f'<span style="color:#dc2626;font-size:10px">{opp.concern_str[:60]}</span>'
        if opp.concern_flags else "—"
    )
    return (
        f'<tr style="background:{row_bg}">'
        f'<td style="padding:4px 7px;font-size:11px">{status_label}</td>'
        f'<td style="padding:4px 7px">{_conf_badge(opp.confidence)}</td>'
        f'<td style="padding:4px 7px;font-size:12px">{opp.book.title[:68]}</td>'
        f'<td style="padding:4px 7px;font-size:11px;text-align:center">{opp.book.isbn13}</td>'
        f'<td style="padding:4px 7px;text-align:right">${opp.book.cost:.2f}</td>'
        f'<td style="padding:4px 7px;text-align:right">${opp.revenue_estimate:.2f}</td>'
        f'<td style="padding:4px 7px;font-size:11px;text-align:center">{src}</td>'
        f'<td style="padding:4px 7px;text-align:right;font-weight:bold;color:#15803d">'
        f'${opp.profit:.2f}</td>'
        f'<td style="padding:4px 7px;text-align:right">{opp.margin_pct*100:.0f}%</td>'
        f'<td style="padding:4px 7px;text-align:right">{roi:.0f}%</td>'
        f'<td style="padding:4px 7px;text-align:center">{opp.ebay_sold_count}</td>'
        f'<td style="padding:4px 7px;font-size:10px">{flags}</td>'
        f'</tr>'
    )


def _table_wrap(rows_html: str) -> str:
    thead = (
        '<thead><tr style="background:#1e40af;color:white;font-size:11px">'
        + "".join(
            f'<th style="padding:5px 7px;text-align:{"left" if i < 2 else "right"}">{h}</th>'
            for i, h in enumerate([
                "Status","Confidence","Title","ISBN-13","Cost","Revenue",
                "Source","Profit","Margin","ROI","Sold #","Concerns"
            ])
        )
        + '</tr></thead>'
    )
    return (
        f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;'
        f'width:100%;font-size:12px;border:1px solid #e5e7eb">'
        f'{thead}<tbody>{rows_html}</tbody></table>'
    )


def _attach_csv(msg: MIMEMultipart, csv_path: Optional[str]) -> None:
    if not csv_path or not os.path.exists(csv_path):
        logger.warning(f"CSV not attached — file not found: {csv_path}")
        return
    with open(csv_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", 'attachment; filename="scanner_results.csv"')
    msg.attach(part)
    logger.info(f"CSV attached: {csv_path}")


def _send(msg: MIMEMultipart) -> bool:
    """Shared SMTP send. Returns True on success."""
    missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")
               if not getattr(config, k, "")]
    if missing:
        logger.warning(f"Email skipped — missing config: {missing}")
        return False
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
            s.ehlo(); s.starttls()
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.sendmail(config.EMAIL_FROM, config.EMAIL_TO, msg.as_string())
        logger.info(f"Email sent → {config.EMAIL_TO} | {msg['Subject']}")
        return True
    except Exception as e:
        logger.error(f"Email failed: {e}")
        return False


def _base_msg(subject: str) -> MIMEMultipart:
    msg             = MIMEMultipart("mixed")
    msg["Subject"]  = subject
    msg["From"]     = config.EMAIL_FROM
    msg["To"]       = config.EMAIL_TO
    return msg


# ── 1. Immediate alert ────────────────────────────────────────────────────────

def send_immediate_alert(
    new_opps:      List[Opportunity],
    significant:   List[Opportunity],
    total_scanned: int,
    api_health:    dict,
    csv_path:      Optional[str] = None,
) -> bool:
    """
    Send right away when new or significantly improved opportunities appear.
    Only called when new_opps or significant is non-empty.
    """
    if not config.EMAIL_ENABLED:
        logger.info("Email disabled")
        return False

    now        = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_new      = len(new_opps)
    n_sig      = len(significant)
    run_mode   = api_health.get("run_mode", "?")

    parts = []
    if n_new:
        parts.append(f"{n_new} new")
    if n_sig:
        parts.append(f"{n_sig} significant gain")
    subject = f"📚 BookScanner ALERT [{run_mode}]: {', '.join(parts)} — {now}"

    # Build rows
    new_rows  = "".join(_opp_table_row(o, "🆕 New",           "#f0fdf4") for o in new_opps)
    sig_rows  = "".join(_opp_table_row(o, "📈 Significant",   "#fffbeb") for o in significant)
    table_html = _table_wrap(new_rows + sig_rows) if (new_rows or sig_rows) else ""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#111827;max-width:960px">
      <h2 style="color:#1e40af;margin-bottom:4px">📚 BookScanner Alert — {now}</h2>
      {_mode_banner(api_health)}
      <p style="font-size:13px;margin:6px 0">
        <strong>{n_new}</strong> new &nbsp;|&nbsp;
        <strong>{n_sig}</strong> significant gain &nbsp;|&nbsp;
        {total_scanned} books scanned
      </p>
      {table_html}
      <p style="font-size:11px;color:#9ca3af;margin-top:16px">
        📎 scanner_results.csv attached — full results.<br>
        A daily summary will follow at {config.DAILY_SUMMARY_HOUR}:00 UTC
        with all current opportunities.
      </p>
    </body></html>
    """

    msg = _base_msg(subject)
    msg.attach(MIMEMultipart("alternative"))
    msg.attach(MIMEText(html, "html"))
    _attach_csv(msg, csv_path)
    return _send(msg)


# ── 2. Daily summary ──────────────────────────────────────────────────────────

def send_daily_summary(
    all_opportunities: List[Opportunity],
    total_scanned:     int,
    api_health:        dict,
    csv_path:          Optional[str] = None,
) -> bool:
    """
    Once-per-day digest of ALL current opportunities.
    Grouped by confidence level. Top 5 by profit highlighted.
    """
    if not config.EMAIL_ENABLED:
        logger.info("Email disabled — daily summary skipped")
        return False

    now      = datetime.now().strftime("%Y-%m-%d %H:%M")
    today    = datetime.now().strftime("%A, %B %-d")
    run_mode = api_health.get("run_mode", "?")
    subject  = (
        f"📚 BookScanner Daily Summary [{run_mode}] — {today} — "
        f"{len(all_opportunities)} opportunities from {total_scanned} books"
    )

    # Confidence breakdown
    conf_counts = Counter(o.confidence for o in all_opportunities)
    conf_rows   = "".join(
        f'<tr><td style="padding:3px 12px 3px 0">{_conf_badge(k)}</td>'
        f'<td style="padding:3px 0"><strong>{v}</strong></td></tr>'
        for k, v in sorted(conf_counts.items())
    )

    # Top 5 by profit
    top5 = sorted(all_opportunities, key=lambda o: -o.profit)[:5]
    top5_rows = "".join(
        _opp_table_row(o, f"#{i}", "#f8fafc")
        for i, o in enumerate(top5, 1)
    )

    # All opportunities by confidence group
    group_html = ""
    for conf_level in [CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
                        CONFIDENCE_FALLBACK, CONFIDENCE_NONE]:
        group = [o for o in all_opportunities if o.confidence == conf_level]
        if not group:
            continue
        bg, color, label = _CONF_STYLE.get(conf_level, ("#f3f4f6","#374151",conf_level))
        group_rows = "".join(
            _opp_table_row(o, "", "#ffffff") for o in sorted(group, key=lambda o: -o.profit)
        )
        group_html += (
            f'<h3 style="color:{color};margin:20px 0 6px">{label} ({len(group)})</h3>'
            + _table_wrap(group_rows)
        )

    # Concern summary
    all_flags   = [f for o in all_opportunities for f in o.concern_flags]
    flag_counts = Counter(all_flags)
    concern_line = " &nbsp;|&nbsp; ".join(
        f"<strong>{v}</strong>× {k.replace('_',' ').title()}"
        for k, v in flag_counts.most_common(5)
    ) if flag_counts else "None"

    ebay_conf   = sum(1 for o in all_opportunities if o.revenue_source == "ebay_sold")
    fallback_ct = sum(1 for o in all_opportunities if o.revenue_source == "amazon_estimate")

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#111827;max-width:960px">
      <h2 style="color:#1e40af;margin-bottom:4px">
        📚 BookScanner Daily Summary — {today}
      </h2>
      {_mode_banner(api_health)}

      <table style="font-size:13px;margin-bottom:14px">
        <tr><td style="padding:2px 16px 2px 0">Books scanned</td>
            <td><strong>{total_scanned}</strong></td></tr>
        <tr><td style="padding:2px 16px 2px 0">Total opportunities</td>
            <td><strong>{len(all_opportunities)}</strong></td></tr>
        <tr><td style="padding:2px 16px 2px 0">eBay confirmed</td>
            <td><strong>{ebay_conf}</strong></td></tr>
        <tr><td style="padding:2px 16px 2px 0">Fallback estimated</td>
            <td><strong {"style='color:#dc2626'" if fallback_ct else ""}>{fallback_ct}</strong></td></tr>
      </table>

      <table style="font-size:12px;margin-bottom:14px">{conf_rows}</table>

      <p style="font-size:12px;color:#6b7280">⚠ Concerns: {concern_line}</p>

      <h3 style="margin:20px 0 6px;color:#1e40af">🏆 Top 5 by Profit</h3>
      {_table_wrap(top5_rows) if top5_rows else "<p>No opportunities found.</p>"}

      <h3 style="margin:24px 0 6px;color:#374151">All Opportunities by Confidence</h3>
      {group_html if group_html else "<p style='color:#6b7280'>No opportunities found.</p>"}

      <p style="font-size:11px;color:#9ca3af;margin-top:20px">
        📎 Full results in scanner_results.csv attached.<br>
        Thresholds: profit ≥ ${config.MIN_PROFIT:.0f} | margin ≥ {config.MIN_MARGIN*100:.0f}%
        | eBay fee {config.EBAY_FEE_RATE*100:.2f}% | shipping $0 (dropship)
      </p>
    </body></html>
    """

    msg = _base_msg(subject)
    msg.attach(MIMEText(html, "html"))
    _attach_csv(msg, csv_path)
    return _send(msg)


# ── 3. SMS ────────────────────────────────────────────────────────────────────

def send_sms(
    new_opps:      List[Opportunity],
    significant:   List[Opportunity],
    total_scanned: int,
    api_health:    dict,
) -> bool:
    """SMS for new + significant only. Never fires for suppressed."""
    if not config.SMS_ENABLED:
        logger.info("SMS disabled")
        return False

    actionable = new_opps + significant
    if len(actionable) < config.SMS_MIN_OPPORTUNITIES:
        logger.info(
            f"SMS skipped — {len(actionable)} actionable "
            f"(min: {config.SMS_MIN_OPPORTUNITIES})"
        )
        return False

    missing = [k for k in (
        "TWILIO_ACCOUNT_SID","TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER","TWILIO_TO_NUMBER",
    ) if not getattr(config, k, "")]
    if missing:
        logger.warning(f"SMS skipped — missing: {missing}")
        return False

    mode = api_health.get("run_mode", "?")
    now  = datetime.now().strftime("%m/%d %H:%M")
    lines = [
        f"[{now}] BookScanner [{mode}]",
        f"{len(new_opps)} new | {len(significant)} improved | "
        f"{total_scanned} scanned\n",
    ]
    for opp in actionable[:10]:
        tag  = "NEW" if opp in new_opps else "+"
        roi  = opp.profit / opp.book.cost * 100 if opp.book.cost else 0
        src  = "eBay" if "ebay" in opp.revenue_source else "~Amzn"
        conf = opp.confidence[0]
        lines.append(
            f"[{tag}/{conf}] {opp.book.title[:34].rstrip()} "
            f"${opp.profit:.0f} ({roi:.0f}%ROI) {src}"
        )
    if len(actionable) > 10:
        lines.append(f"...+{len(actionable)-10} more — see email")

    body = "\n".join(lines)
    try:
        from twilio.rest import Client
        msg = Client(
            config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN
        ).messages.create(
            body=body, from_=config.TWILIO_FROM_NUMBER, to=config.TWILIO_TO_NUMBER
        )
        logger.info(f"SMS sent (SID: {msg.sid})")
        return True
    except ImportError:
        logger.warning("Twilio not installed — pip install twilio")
        return False
    except Exception as e:
        logger.error(f"SMS failed: {e}")
        return False


# ── 4. Error alert ────────────────────────────────────────────────────────────

def send_error_alert(error_message: str) -> None:
    """Fire-and-forget crash notification via SMS."""
    if not (config.SMS_ENABLED and getattr(config, "TWILIO_ACCOUNT_SID", "")):
        return
    try:
        from twilio.rest import Client
        Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN).messages.create(
            body  = f"[BookScanner ERROR] {error_message[:200]}",
            from_ = config.TWILIO_FROM_NUMBER,
            to    = config.TWILIO_TO_NUMBER,
        )
    except Exception as e:
        logger.error(f"Error SMS failed: {e}")
