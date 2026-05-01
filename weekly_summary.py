#!/usr/bin/env python3
"""
weekly_summary.py v4
- Parses only the MOST RECENT run block from each log file
- Short summary in email body
- Full detail as nicely formatted .txt attachment with tables
- v4: Added FAILED LISTINGS section with error detail and corrective actions
"""

import os, re, smtplib
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


def extract_latest_block(log: str, start_marker: str, end_marker: str) -> str:
    """Extract only the most recent run block between start and end markers."""
    lines = log.splitlines()
    # Find the LAST occurrence of start_marker
    start_idx = None
    for i, line in enumerate(lines):
        if start_marker in line:
            start_idx = i
    if start_idx is None:
        return ""
    # From start, find the next end_marker
    block_lines = []
    for line in lines[start_idx:]:
        block_lines.append(line)
        if end_marker in line and len(block_lines) > 1:
            break
    return "\n".join(block_lines)


def parse_scanner(log: str) -> dict:
    result = {"opportunities": 0, "already_listed": 0, "unprofitable": 0,
              "amazon_fallbacks": 0, "blocklist_skip": 0, "books": []}

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
        if "Blocklist skip:" in line:
            m = re.search(r":\s*(\d+)", line)
            if m: result["blocklist_skip"] = int(m.group(1))
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
    result = {"listed": 0, "failed": 0, "books": [], "failures": []}
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
        # Parse FAILED_DETAIL lines (v2.4+ format)
        if "FAILED_DETAIL" in line:
            m = re.search(
                r"FAILED_DETAIL\s+(\d{13})\s*\|\s*(.+?)\s*\|\s*stage=(\w+)\s*\|\s*image=(True|False)\s*\|\s*(.+)",
                line
            )
            if m:
                result["failures"].append({
                    "isbn":      m.group(1),
                    "title":     m.group(2).strip(),
                    "stage":     m.group(3),
                    "had_image": m.group(4) == "True",
                    "error":     m.group(5).strip(),
                })
        # Also parse old-style FAIL lines for backward compatibility
        elif "FAIL " in line and "FAILED_DETAIL" not in line and "FAIL:" not in line:
            m = re.search(r"FAIL\s+(\d{13}):\s*(.+)", line)
            if m:
                isbn = m.group(1)
                err  = m.group(2).strip()
                # Avoid duplicates if FAILED_DETAIL already captured this ISBN
                if not any(f["isbn"] == isbn for f in result["failures"]):
                    result["failures"].append({
                        "isbn":      isbn,
                        "title":     "",
                        "stage":     "unknown",
                        "had_image": False,
                        "error":     err,
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
    """Build a plain-text table."""
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

    scanner_log  = read_log("scanner_log.txt")
    lister_log   = read_log("lister_log.txt")
    repricer_log = read_log("repricer_log.txt")

    scanner  = parse_scanner(scanner_log)
    lister   = parse_lister(lister_log)
    repricer = parse_repricer(repricer_log)

    # ── Email body (short summary only) ───────────────────────────
    body_lines = [
        f"BooksGoat Weekly Pipeline Summary",
        f"Run: {now}",
        "=" * 45,
        "",
        f"  Opportunities found : {scanner['opportunities']}",
        f"  Already listed      : {scanner['already_listed']}",
        f"  Unprofitable        : {scanner['unprofitable']}",
        f"  Blocklist skipped   : {scanner['blocklist_skip']}",
        f"  New listings        : {lister['listed']}",
        f"  Listing failures    : {lister['failed']}",
        f"  Prices updated      : {repricer['repriced']}",
        f"  Auto-delisted       : {repricer['delisted']}",
        f"  Unchanged           : {repricer['unchanged']}",
        f"  Errors              : {repricer['errors']}",
        "",
    ]

    if scanner['books']:
        body_lines.append(f"NEW OPPORTUNITIES ({len(scanner['books'])}):")
        for b in scanner['books']:
            body_lines.append(f"  * {b['title']}")
            body_lines.append(f"    Cost ${b['cost']} | List ${b['price']} | Profit ${b['profit']} | {b['conf']}")
        body_lines.append("")

    if lister['failures']:
        body_lines.append(f"LISTING FAILURES ({len(lister['failures'])}) — ACTION NEEDED:")
        for f in lister['failures']:
            title_str = f['title'] or f['isbn']
            body_lines.append(f"  * {title_str}")
            body_lines.append(f"    ISBN: {f['isbn']} | Stage: {f['stage']} | Error: {f['error'][:80]}")
        body_lines.append("")

    if repricer['delisted_books']:
        body_lines.append(f"AUTO-DELISTED ({len(repricer['delisted_books'])}):")
        for b in repricer['delisted_books']:
            body_lines.append(f"  * {b['title']} (profit was ${b['profit']})")
        body_lines.append("")

    body_lines.append("See attachment for full detail.")
    body = "\n".join(body_lines)

    # ── Attachment (full detail with tables) ──────────────────────
    att_lines = [
        "BooksGoat Weekly Pipeline — Full Detail",
        f"Generated: {now}",
        "=" * 70,
        "",
    ]

    # Opportunities table
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

    # New listings table
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

    # Failed listings table — NEW in v4
    att_lines += ["FAILED LISTINGS — ACTION NEEDED", "-" * 70]
    if lister['failures']:
        rows = [[f['isbn'], (f['title'] or 'N/A')[:35], f['stage'],
                 'Yes' if f['had_image'] else 'No', f['error'][:40]]
                for f in lister['failures']]
        att_lines.append(table(
            ["ISBN", "Title", "Stage", "Image?", "Error"],
            rows, [15, 37, 12, 8, 42]
        ))
        att_lines.append("")
        att_lines.append("Corrective actions:")
        for i, f in enumerate(lister['failures'], 1):
            title_str = f['title'] or f['isbn']
            att_lines.append(f"  {i}. {title_str} ({f['isbn']})")
            err_lower = f['error'].lower()
            if 'photo' in err_lower or 'picture' in err_lower:
                if 'resolution' in err_lower:
                    att_lines.append("     -> Image too low-res. Upload higher-res cover in Seller Hub.")
                else:
                    att_lines.append("     -> No image found. Upload cover photo in Seller Hub.")
                att_lines.append("     -> Search Google Images for ISBN to find suitable cover.")
            elif 'description' in err_lower:
                att_lines.append("     -> Run fix_listings.py locally to generate AI description.")
            elif '25002' in f['error']:
                att_lines.append("     -> eBay catalog conflict. List manually via Seller Hub.")
            else:
                att_lines.append(f"     -> Review error: {f['error'][:80]}")
            att_lines.append("")
    else:
        att_lines.append("  No listing failures this run.\n")

    # Repriced table
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

    # Delisted table
    att_lines += ["AUTO-DELISTED LISTINGS", "-" * 70]
    if repricer['delisted_books']:
        rows = [[b['title'][:55], f"${b['profit']}"] for b in repricer['delisted_books']]
        att_lines.append(table(
            ["Title", "Profit at Delist"],
            rows, [57, 18]
        ))
    else:
        att_lines.append("  No delistings this run.\n")

    detail = "\n".join(att_lines)

    # ── Send ──────────────────────────────────────────────────────
    fail_tag = f" | {lister['failed']} FAILED" if lister['failed'] > 0 else ""
    subject = (f"BooksGoat Weekly — {datetime.utcnow().strftime('%Y-%m-%d')} | "
               f"{scanner['opportunities']} opps | {lister['listed']} listed{fail_tag} | "
               f"{repricer['repriced']} repriced | {repricer['delisted']} delisted")

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "plain"))

    att = MIMEBase("application", "octet-stream")
    att.set_payload(detail.encode("utf-8"))
    encoders.encode_base64(att)
    fname = f"booksgoat_weekly_{datetime.utcnow().strftime('%Y%m%d')}.txt"
    att.add_header("Content-Disposition", f'attachment; filename="{fname}"')
    msg.attach(att)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

    print(f"Sent: {scanner['opportunities']} opps | {lister['listed']} listed | "
          f"{lister['failed']} failed | "
          f"{repricer['repriced']} repriced | {repricer['delisted']} delisted")


if __name__ == "__main__":
    run()
