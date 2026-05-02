#!/usr/bin/env python3
"""
weekly_summary.py v4
- Parses only the MOST RECENT run block from each log file
- HTML summary in email body (via email_helpers)
- Full detail as nicely formatted .txt attachment with tables
"""

import os, re, smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

try:
    from email_helpers import build_weekly_summary_email, send_html_email_with_attachment
except ImportError:
    build_weekly_summary_email = None
    send_html_email_with_attachment = None

SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM    = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO      = os.environ.get("EMAIL_TO", SMTP_USER)


def read_log(path: str) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""


def extract_latest_block(log: str, start_marker: str, end_marker: str) -> str:
    """Extract only the most recent run block between start and end markers."""
    lines = log.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if start_marker in line:
            start_idx = i
    if start_idx is None:
        return ""
    block_lines = []
    for line in lines[start_idx:]:
        block_lines.append(line)
        if end_marker in line and len(block_lines) > 1:
            break
    return "\n".join(block_lines)


def parse_scanner(log: str) -> dict:
    result = {"opportunities": 0, "already_listed": 0, "unprofitable": 0,
              "amazon_fallbacks": 0, "books": []}

    block = extract_latest_block(log, "SCANNER STARTED", "SCAN COMPLETE")
    if not block:
        return result

    for line in block.splitlines():
        if "SCAN COMPLETE:" in line:
            m = re.search(r"(\d+) opportunities", line)
            if m: result["opportunities"] = int(m.group(1))
        if "Already listed:" in line:
            m = re.search(r":\s*(\d+)", line)
            if m: result["already_listed"] = int(m.group(1))
        if "Unprofitable:" in line:
            m = re.search(r":\s*(\d+)", line)
            if m: result["unprofitable"] = int(m.group(1))
        if "Amazon fallbacks:" in line:
            m = re.search(r":\s*(\d+)", line)
            if m: result["amazon_fallbacks"] = int(m.group(1))
        if "\u2705" in line and "Cost:" in line and "Profit:" in line:
            m = re.search(r"\u2705\s+(.+?)\s*\|\s*Cost:\s*\$([0-9.]+)\s*\|\s*List:\s*\$([0-9.]+)\s*\|\s*Profit:\s*\$([0-9.]+)\s*\|\s*([\w_]+)", line)
            if m:
                result["books"].append({
                    "title":  m.group(1).strip()[:55],
                    "cost":   m.group(2),
                    "price":  m.group(3),
                    "profit": m.group(4),
                    "conf":   m.group(5),
                })
    return result


def parse_lister(log: str) -> dict:
    result = {"listed": 0, "failed": 0, "books": []}
    block = extract_latest_block(log, "LISTER STARTED", "LISTER DONE")
    if not block:
        return result
    for line in block.splitlines():
        if "LISTER DONE:" in line:
            m = re.search(r"(\d+) listed", line)
            if m: result["listed"] = int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m: result["failed"] = int(m.group(1))
        if "\u2705 Listed" in line:
            m = re.search(r"\$([0-9.]+)\s*\|\s*Profit:\s*\$([0-9.]+)\s*\|\s*ListingID:\s*(\d+)", line)
            if m:
                result["books"].append({
                    "price": m.group(1), "profit": m.group(2), "listing_id": m.group(3)
                })
    return result


def parse_repricer(log: str) -> dict:
    result = {"repriced": 0, "delisted": 0, "unchanged": 0,
              "repriced_books": [], "delisted_books": [], "errors": 0}
    block = extract_latest_block(log, "REPRICER STARTED", "DONE:")
    if not block:
        return result
    for line in block.splitlines():
        if "DONE:" in line:
            m = re.search(r"(\d+) repriced", line)
            if m: result["repriced"] = int(m.group(1))
            m = re.search(r"(\d+) delisted", line)
            if m: result["delisted"] = int(m.group(1))
            m = re.search(r"(\d+) unchanged", line)
            if m: result["unchanged"] = int(m.group(1))
        m = re.search(r"(.{5,50}):\s+\$([0-9.]+)\s+\u2192\s+\$([0-9.]+)\s+profit=\$([0-9.]+)", line)
        if m:
            result["repriced_books"].append({
                "title": m.group(1).strip(), "old": m.group(2),
                "new": m.group(3), "profit": m.group(4)
            })
        if "AUTO-DELIST" in line:
            m = re.search(r"(.{5,50}):\s+AUTO-DELIST.*?profit\s+\$([0-9.-]+)", line)
            if m:
                result["delisted_books"].append(
                    {"title": m.group(1).strip(), "profit": m.group(2)})
        if "\u274c" in line:
            result["errors"] += 1
    return result


def table(headers: list, rows: list, col_widths: list = None) -> str:
    """Build a plain-text table for the .txt attachment."""
    if not rows:
        return "  (none)\n"
    if not col_widths:
        col_widths = [max(len(str(r[i])) for r in rows + [headers]) + 2
                      for i in range(len(headers))]
    sep = "+" + "+".join("-" * w for w in col_widths) + "+"
    def row_str(r):
        return "|" + "|".join(f" {str(v):<{w-2}} " for v, w in zip(r, col_widths)) + "|"
    lines = [sep, row_str(headers), sep]
    for r in rows:
        lines.append(row_str(r))
    lines.append(sep)
    return "\n".join(lines) + "\n"


def run():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    run_date = datetime.utcnow().strftime("%Y-%m-%d")

    scanner_log  = read_log("scanner_log.txt")
    lister_log   = read_log("lister_log.txt")
    repricer_log = read_log("repricer_log.txt")

    scanner  = parse_scanner(scanner_log)
    lister   = parse_lister(lister_log)
    repricer = parse_repricer(repricer_log)

    # ── Build .txt attachment (full detail with tables) ──────────────────────
    att_lines = [
        "BooksGoat Weekly Pipeline — Full Detail",
        f"Generated: {now}",
        "=" * 70,
        "",
    ]

    att_lines += ["SCANNER OPPORTUNITIES", "-" * 70]
    if scanner['books']:
        rows = [[b['title'][:45], f"${b['cost']}", f"${b['price']}", f"${b['profit']}", b['conf']]
                for b in scanner['books']]
        att_lines.append(table(
            ["Title", "Cost", "List Price", "Profit", "Confidence"],
            rows, [47, 10, 12, 10, 18]
        ))
    else:
        att_lines.append("  No new opportunities this run.\n")

    att_lines += ["REPRICED LISTINGS", "-" * 70]
    if repricer['repriced_books']:
        rows = [[b['title'][:45], f"${b['old']}", f"${b['new']}", f"${b['profit']}"]
                for b in repricer['repriced_books']]
        att_lines.append(table(
            ["Title", "Old Price", "New Price", "New Profit"],
            rows, [47, 12, 12, 14]
        ))
    else:
        att_lines.append("  No reprices this run.\n")

    att_lines += ["AUTO-DELISTED LISTINGS", "-" * 70]
    if repricer['delisted_books']:
        rows = [[b['title'][:55], f"${b['profit']}"] for b in repricer['delisted_books']]
        att_lines.append(table(
            ["Title", "Profit at Delist"],
            rows, [57, 18]
        ))
    else:
        att_lines.append("  No delistings this run.\n")

    att_lines += ["NEW LISTINGS CREATED", "-" * 70]
    if lister['books']:
        rows = [[b['listing_id'], f"${b['price']}", f"${b['profit']}"]
                for b in lister['books']]
        att_lines.append(table(
            ["Listing ID", "Sale Price", "Profit"],
            rows, [14, 12, 12]
        ))
    else:
        att_lines.append("  No new listings this run.\n")

    detail = "\n".join(att_lines)

    # ── Build subject ────────────────────────────────────────────────────────
    subject = (f"BooksGoat Weekly — {run_date} | "
               f"{scanner['opportunities']} opps | {lister['listed']} listed | "
               f"{repricer['repriced']} repriced | {repricer['delisted']} delisted")

    # ── Send HTML email with .txt attachment ─────────────────────────────────
    if build_weekly_summary_email and send_html_email_with_attachment:
        # Convert parsed data to the format expected by email_helpers
        opportunities = [
            {
                'isbn': '', 'title': b['title'],
                'cost': float(b['cost']), 'sell_price': float(b['price']),
                'profit': float(b['profit']), 'confidence': b['conf'],
            }
            for b in scanner['books']
        ]
        repriced = [
            {
                'isbn': '', 'title': b['title'],
                'old_price': float(b['old']), 'new_price': float(b['new']),
                'profit': float(b['profit']),
            }
            for b in repricer['repriced_books']
        ]
        delisted = [
            {'isbn': '', 'title': b['title'], 'reason': 'unprofitable', 'profit': float(b['profit'])}
            for b in repricer['delisted_books']
        ]

        html_body = build_weekly_summary_email(
            active_count=scanner.get('already_listed', 0),
            pending_count=0,
            total_csv=0,
            opportunities=opportunities,
            repriced=repriced,
            delisted=delisted,
            scanner_errors=0,
            lister_errors=lister['failed'],
            repricer_errors=repricer['errors'],
        )

        fname = f"booksgoat_weekly_{run_date.replace('-','')}.txt"
        send_html_email_with_attachment(subject, html_body, detail, fname)

    else:
        # Fallback: plain text (original behavior)
        body_lines = [
            f"BooksGoat Weekly Pipeline Summary",
            f"Run: {now}",
            "=" * 45,
            "",
            f"  Opportunities found : {scanner['opportunities']}",
            f"  Already listed      : {scanner['already_listed']}",
            f"  Unprofitable        : {scanner['unprofitable']}",
            f"  New listings        : {lister['listed']}",
            f"  Prices updated      : {repricer['repriced']}",
            f"  Auto-delisted       : {repricer['delisted']}",
            f"  Unchanged           : {repricer['unchanged']}",
            f"  Errors              : {repricer['errors']}",
            "",
            "See attachment for full detail.",
        ]
        body = "\n".join(body_lines)

        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(body, "plain"))

        att = MIMEBase("application", "octet-stream")
        att.set_payload(detail.encode("utf-8"))
        encoders.encode_base64(att)
        fname = f"booksgoat_weekly_{run_date.replace('-','')}.txt"
        att.add_header("Content-Disposition", f'attachment; filename="{fname}"')
        msg.attach(att)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

    print(f"Sent: {scanner['opportunities']} opps | {lister['listed']} listed | "
          f"{repricer['repriced']} repriced | {repricer['delisted']} delisted")


if __name__ == "__main__":
    run()
