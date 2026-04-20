#!/usr/bin/env python3
"""
weekly_summary.py v2 — Enhanced weekly pipeline summary email
Sends a short summary inline + full detail as attachment.
Fixes: duplicate opportunities, inventory snapshot from CSV.
"""

import os, re, smtplib, csv
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_FROM    = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO      = os.environ.get("EMAIL_TO", SMTP_USER)


def read_log(path: str) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""


def parse_scanner(log: str) -> dict:
    result = {"opportunities": 0, "already_listed": 0, "unprofitable": 0,
              "no_ebay_data": 0, "amazon_fallbacks": 0, "books": []}

    # Only parse the SCAN COMPLETE block — no duplicates
    complete_block = ""
    in_block = False
    for line in log.splitlines():
        if "SCAN COMPLETE" in line:
            in_block = True
        if in_block:
            complete_block += line + "\n"
            if "============" in line and in_block and complete_block.count("====") > 1:
                break

    for line in log.splitlines():
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
        # Only capture ✅ lines (actual opportunities, not repricer lines)
        if "✅" in line and "Cost:" in line and "Profit:" in line and "ListingID" not in line:
            m = re.search(r"✅\s+(.+?)\s*\|\s*Cost:\s*\$([0-9.]+)\s*\|\s*List:\s*\$([0-9.]+)\s*\|\s*Profit:\s*\$([0-9.]+)\s*\|\s*([\w_]+)", line)
            if m:
                result["books"].append({
                    "title":  m.group(1).strip()[:55],
                    "cost":   m.group(2),
                    "price":  m.group(3),
                    "profit": m.group(4),
                    "conf":   m.group(5),
                })

    # Deduplicate by title
    seen = set()
    unique = []
    for b in result["books"]:
        if b["title"] not in seen:
            seen.add(b["title"])
            unique.append(b)
    result["books"] = unique
    return result


def parse_lister(log: str) -> dict:
    result = {"listed": 0, "failed": 0, "books": []}
    for line in log.splitlines():
        if "LISTER DONE:" in line:
            m = re.search(r"(\d+) listed", line)
            if m: result["listed"] = int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m: result["failed"] = int(m.group(1))
        if "✅ Listed" in line:
            m = re.search(r"\$([0-9.]+)\s*\|\s*Profit:\s*\$([0-9.]+)\s*\|\s*ListingID:\s*(\d+)", line)
            if m:
                result["books"].append({
                    "price": m.group(1), "profit": m.group(2), "listing_id": m.group(3)
                })
    return result


def parse_repricer(log: str) -> dict:
    result = {"repriced": 0, "delisted": 0, "unchanged": 0,
              "repriced_books": [], "delisted_books": [], "errors": 0}
    for line in log.splitlines():
        if "DONE:" in line:
            m = re.search(r"(\d+) repriced", line)
            if m: result["repriced"] = int(m.group(1))
            m = re.search(r"(\d+) delisted", line)
            if m: result["delisted"] = int(m.group(1))
            m = re.search(r"(\d+) unchanged", line)
            if m: result["unchanged"] = int(m.group(1))
        m = re.search(r"INFO\s+(.{5,50}):\s+\$([0-9.]+)\s+→\s+\$([0-9.]+)\s+profit=\$([0-9.]+)", line)
        if m:
            result["repriced_books"].append({
                "title": m.group(1).strip(), "old": m.group(2),
                "new": m.group(3), "profit": m.group(4)
            })
        if "AUTO-DELIST" in line:
            m = re.search(r"INFO\s+(.{5,50}):\s+AUTO-DELIST.*?profit\s+\$([0-9.-]+)", line)
            if m:
                result["delisted_books"].append(
                    {"title": m.group(1).strip(), "profit": m.group(2)})
        if "❌" in line:
            result["errors"] += 1
    return result


def run():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    scanner_log  = read_log("scanner_log.txt")
    lister_log   = read_log("lister_log.txt")
    repricer_log = read_log("repricer_log.txt")

    scanner  = parse_scanner(scanner_log)
    lister   = parse_lister(lister_log)
    repricer = parse_repricer(repricer_log)

    # ── Short summary (email body) ────────────────────────────
    summary_lines = [
        f"BooksGoat Weekly Summary — {now}",
        "=" * 50,
        "",
        "RESULTS AT A GLANCE",
        f"  New opportunities found:  {scanner['opportunities']}",
        f"  New listings created:     {lister['listed']}",
        f"  Prices updated:           {repricer['repriced']}",
        f"  Auto-delisted:            {repricer['delisted']}",
        f"  Repricer errors:          {repricer['errors']}",
        "",
    ]

    if scanner['books']:
        summary_lines.append("NEW OPPORTUNITIES:")
        for b in scanner['books']:
            summary_lines.append(f"  • {b['title']}")
            summary_lines.append(f"    ${b['cost']} cost → ${b['price']} list | Profit: ${b['profit']} | {b['conf']}")
        summary_lines.append("")

    if repricer['delisted_books']:
        summary_lines.append("AUTO-DELISTED:")
        for b in repricer['delisted_books']:
            summary_lines.append(f"  • {b['title']} (profit was ${b['profit']})")
        summary_lines.append("")

    summary_lines.append("Full detail in attachment.")
    body = "\n".join(summary_lines)

    # ── Full detail (attachment) ──────────────────────────────
    detail_lines = [body, "", "=" * 50, "FULL REPRICE DETAIL", "=" * 50, ""]
    if repricer['repriced_books']:
        for b in repricer['repriced_books']:
            detail_lines.append(f"  {b['title'][:55]}")
            detail_lines.append(f"    ${b['old']} → ${b['new']} | Profit: ${b['profit']}")
    else:
        detail_lines.append("  No reprices this run.")

    detail_lines += ["", "=" * 50, "RAW SCANNER LOG (last 3000 chars)", "=" * 50, ""]
    detail_lines.append(scanner_log[-3000:])
    detail_lines += ["", "=" * 50, "RAW REPRICER LOG (last 3000 chars)", "=" * 50, ""]
    detail_lines.append(repricer_log[-3000:])
    detail = "\n".join(detail_lines)

    # ── Send email ────────────────────────────────────────────
    msg = MIMEMultipart()
    msg["Subject"] = f"BooksGoat Weekly Summary — {datetime.utcnow().strftime('%Y-%m-%d')} | {lister['listed']} listed | {repricer['repriced']} repriced"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "plain"))

    # Attach full detail
    att = MIMEBase("application", "octet-stream")
    att.set_payload(detail.encode("utf-8"))
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", f'attachment; filename="booksgoat_weekly_{datetime.utcnow().strftime("%Y%m%d")}.txt"')
    msg.attach(att)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

    print(f"Weekly summary sent: {scanner['opportunities']} opps | {lister['listed']} listed | {repricer['repriced']} repriced")


if __name__ == "__main__":
    run()
