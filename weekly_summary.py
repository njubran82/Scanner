#!/usr/bin/env python3
"""
weekly_summary.py — Enhanced weekly pipeline summary email
Runs on GitHub Actions after scanner/lister/repricer

Parses log files and CSV to produce a structured summary email.
"""

import os, re, smtplib, csv
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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
    result = {
        "opportunities": 0,
        "already_listed": 0,
        "unprofitable": 0,
        "no_ebay_data": 0,
        "books": []
    }
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
        if "No eBay data:" in line:
            m = re.search(r":\s*(\d+)", line)
            if m: result["no_ebay_data"] = int(m.group(1))
        # Capture opportunity lines: ✅ Title | Cost: $X | List: $Y | Profit: $Z | CONF
        if "✅" in line and "Cost:" in line and "Profit:" in line:
            m = re.search(r"✅\s+(.+?)\s*\|\s*Cost:\s*\$([0-9.]+)\s*\|\s*List:\s*\$([0-9.]+)\s*\|\s*Profit:\s*\$([0-9.]+)\s*\|\s*(\w+)", line)
            if m:
                result["books"].append({
                    "title": m.group(1).strip(),
                    "cost": m.group(2),
                    "price": m.group(3),
                    "profit": m.group(4),
                    "conf": m.group(5),
                })
    return result


def parse_lister(log: str) -> dict:
    result = {"listed": 0, "failed": 0, "books": []}
    for line in log.splitlines():
        if "LISTER DONE:" in line:
            m = re.search(r"(\d+) listed", line)
            if m: result["listed"] = int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m: result["failed"] = int(m.group(1))
        # ✅ Listed | $X | Profit: $Y | ListingID: Z
        if "✅ Listed" in line:
            m = re.search(r"\$([0-9.]+)\s*\|\s*Profit:\s*\$([0-9.]+)\s*\|\s*ListingID:\s*(\d+)", line)
            if m:
                result["books"].append({
                    "price": m.group(1),
                    "profit": m.group(2),
                    "listing_id": m.group(3),
                })
    return result


def parse_repricer(log: str) -> dict:
    result = {
        "repriced": 0,
        "delisted": 0,
        "unchanged": 0,
        "repriced_books": [],
        "delisted_books": [],
        "errors": 0,
    }
    current_title = ""
    for line in log.splitlines():
        if "DONE:" in line:
            m = re.search(r"(\d+) repriced", line)
            if m: result["repriced"] = int(m.group(1))
            m = re.search(r"(\d+) delisted", line)
            if m: result["delisted"] = int(m.group(1))
            m = re.search(r"(\d+) unchanged", line)
            if m: result["unchanged"] = int(m.group(1))
        # Title line: "  BookTitle: $X.XX → $Y.YY profit=$Z method=..."
        m = re.search(r"INFO\s+(.+?):\s+\$([0-9.]+)\s+→\s+\$([0-9.]+)\s+profit=\$([0-9.]+)", line)
        if m:
            result["repriced_books"].append({
                "title": m.group(1).strip(),
                "old_price": m.group(2),
                "new_price": m.group(3),
                "profit": m.group(4),
            })
        # Delist line
        if "AUTO-DELIST" in line:
            m = re.search(r"INFO\s+(.+?):\s+AUTO-DELIST.*?profit\s+\$([0-9.-]+)", line)
            if m:
                result["delisted_books"].append({
                    "title": m.group(1).strip(),
                    "profit": m.group(2),
                })
        if "❌" in line:
            result["errors"] += 1
    return result


def parse_csv_snapshot() -> dict:
    path = Path("booksgoat_enhanced.csv")
    if not path.exists():
        return {"active": 0, "pending": 0, "delisted": 0, "total": 0}
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    return {
        "active":   len([r for r in rows if r.get("status") == "active"]),
        "pending":  len([r for r in rows if r.get("status") == "pending"]),
        "delisted": len([r for r in rows if r.get("status") == "delisted"]),
        "total":    len(rows),
    }


def build_email(scanner: dict, lister: dict, repricer: dict, snap: dict) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    a = lines.append

    a("=" * 60)
    a(f"BooksGoat Weekly Pipeline Summary")
    a(f"Run date: {now}")
    a("=" * 60)

    # Inventory snapshot
    a("")
    a("INVENTORY SNAPSHOT")
    a("-" * 40)
    a(f"  Active listings:   {snap['active']}")
    a(f"  Pending (unlisted): {snap['pending']}")
    a(f"  Delisted:          {snap['delisted']}")
    a(f"  Total in CSV:      {snap['total']}")

    # Scanner
    a("")
    a("SCANNER")
    a("-" * 40)
    a(f"  Opportunities found: {scanner['opportunities']}")
    a(f"  Already listed:      {scanner['already_listed']}")
    a(f"  Unprofitable:        {scanner['unprofitable']}")
    a(f"  No eBay data:        {scanner['no_ebay_data']}")
    if scanner["books"]:
        a("")
        a("  Opportunities:")
        for b in scanner["books"]:
            a(f"    • {b['title'][:50]}")
            a(f"      Cost: ${b['cost']} | List: ${b['price']} | Profit: ${b['profit']} | {b['conf']}")

    # Lister
    a("")
    a("LISTER")
    a("-" * 40)
    a(f"  Listed:  {lister['listed']}")
    a(f"  Failed:  {lister['failed']}")
    if lister["books"]:
        a("")
        a("  New listings:")
        for b in lister["books"]:
            a(f"    • ${b['price']} | Profit: ${b['profit']} | ID: {b['listing_id']}")

    # Repricer
    a("")
    a("REPRICER")
    a("-" * 40)
    a(f"  Repriced:  {repricer['repriced']}")
    a(f"  Delisted:  {repricer['delisted']}")
    a(f"  Unchanged: {repricer['unchanged']}")
    a(f"  Errors:    {repricer['errors']}")

    if repricer["repriced_books"]:
        a("")
        a("  Price updates:")
        for b in repricer["repriced_books"]:
            a(f"    • {b['title'][:45]}")
            a(f"      ${b['old_price']} → ${b['new_price']} | Profit: ${b['profit']}")

    if repricer["delisted_books"]:
        a("")
        a("  Auto-delisted (unprofitable):")
        for b in repricer["delisted_books"]:
            a(f"    • {b['title'][:50]} (profit was ${b['profit']})")

    a("")
    a("=" * 60)
    return "\n".join(lines)


def send_email(body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"BooksGoat Weekly Summary — {datetime.utcnow().strftime('%Y-%m-%d')}"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    print("Weekly summary sent")


def run():
    scanner_log  = read_log("scanner_log.txt")
    lister_log   = read_log("lister_log.txt")
    repricer_log = read_log("repricer_log.txt")

    scanner  = parse_scanner(scanner_log)
    lister   = parse_lister(lister_log)
    repricer = parse_repricer(repricer_log)
    snap     = parse_csv_snapshot()

    body = build_email(scanner, lister, repricer, snap)
    print(body)
    send_email(body)


if __name__ == "__main__":
    run()
