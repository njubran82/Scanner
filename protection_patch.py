"""
protection_patch.py
───────────────────────────────────────────────────────────────────────────────
Drop-in protection logic for the BooksGoat repricer and order monitor.

This file is imported by repricer.py and order_monitor.py. It provides:
  - is_protected(row)         → bool
  - should_delist(row, profit) → bool  (use this instead of raw profit check)
  - increment_sales(csv_path, isbn, isbn_col="isbn13")

INTEGRATION — repricer.py
─────────────────────────
At the top of repricer.py, add:
    from protection_patch import should_delist

Find the delist decision block. It currently looks something like:
    if profit < MIN_PROFIT:
        delist_item(offer_id)

Replace with:
    if should_delist(row, profit):
        delist_item(offer_id)

INTEGRATION — order_monitor.py
───────────────────────────────
At the top of order_monitor.py, add:
    from protection_patch import increment_sales

When a sale is detected for an ISBN, add:
    increment_sales(LISTER_CSV, isbn)
    increment_sales(SCANNER_CSV, isbn)

That's it. No other changes needed.

INTEGRATION — ebay_lister.py / scanner
───────────────────────────────────────
When checking if a book is "already listed" and should be skipped, add:

    from protection_patch import is_protected

    # Never skip a protected book — always attempt to relist
    if row.get("status") == "active" and not is_protected(row):
        continue  # skip already-listed non-protected books
"""

import csv
import os
from datetime import datetime, timezone

# ─── CONFIG ──────────────────────────────────────────────────────────────────

PROTECTION_THRESHOLD = 3    # sales_count >= this → protected
PROTECTED_MIN_PROFIT = 0.0  # protected books only delist below this (selling at a loss)
NORMAL_MIN_PROFIT    = 5.0  # normal books delist below this


# ─── CORE LOGIC ──────────────────────────────────────────────────────────────

def is_protected(row: dict) -> bool:
    """
    Returns True if this CSV row has protected=true.
    Case-insensitive, handles missing column gracefully.
    """
    val = str(row.get("protected", "")).strip().lower()
    return val == "true"


def should_delist(row: dict, profit: float) -> bool:
    """
    Central delist decision gate. Use this everywhere instead of
    raw profit comparisons.

    Rules:
      - Protected book  → delist only if profit < $0 (selling at a loss)
      - Normal book     → delist if profit < $12 (existing floor)

    Args:
      row    : CSV row dict for this book
      profit : calculated profit for current pricing

    Returns:
      True  → go ahead and delist
      False → do NOT delist (either profitable enough, or protected)
    """
    if is_protected(row):
        decision = profit < PROTECTED_MIN_PROFIT
        if decision:
            _log(row, profit, "PROTECTED delist — selling at a loss")
        else:
            _log(row, profit, f"PROTECTED hold — profit ${profit:.2f} above loss floor")
        return decision
    else:
        decision = profit < NORMAL_MIN_PROFIT
        if decision:
            _log(row, profit, f"Normal delist — profit ${profit:.2f} below ${NORMAL_MIN_PROFIT} floor")
        return decision


def _log(row, profit, reason):
    isbn  = row.get("isbn13") or row.get("isbn") or row.get("sku") or "?"
    title = str(row.get("title", ""))[:40]
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"  [{ts}] {isbn} | ${profit:.2f} | {reason} | {title}")


# ─── SALES COUNT ─────────────────────────────────────────────────────────────

def increment_sales(csv_path: str, isbn: str, isbn_col: str = "isbn13") -> bool:
    """
    Find the row matching isbn in csv_path, increment sales_count by 1,
    and auto-set protected=true if sales_count reaches PROTECTION_THRESHOLD.
    Writes the file in place. Returns True on success, False if ISBN not found.

    Call this once per CSV path when a sale is detected:
        increment_sales(LISTER_CSV, isbn)
        increment_sales(SCANNER_CSV, isbn)
    """
    if not os.path.exists(csv_path):
        print(f"  increment_sales: CSV not found: {csv_path}")
        return False

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader    = csv.DictReader(f)
        rows      = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if not rows:
        return False

    # Ensure columns exist
    if "sales_count" not in fieldnames:
        fieldnames.append("sales_count")
    if "protected" not in fieldnames:
        fieldnames.append("protected")

    found = False
    for row in rows:
        row_isbn = str(row.get(isbn_col, "")).strip()
        if row_isbn == str(isbn).strip():
            # Increment
            current = int(row.get("sales_count") or 0)
            new_count = current + 1
            row["sales_count"] = str(new_count)

            # Auto-protect
            if new_count >= PROTECTION_THRESHOLD:
                if row.get("protected") != "true":
                    row["protected"] = "true"
                    print(f"  AUTO-PROTECTED: {isbn} reached {new_count} sales")

            found = True
            break

    if not found:
        print(f"  increment_sales: ISBN {isbn} not found in {csv_path}")
        return False

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return True


# ─── BULK SYNC ───────────────────────────────────────────────────────────────

def sync_protection_flags(csv_path: str, isbn_col: str = "isbn13") -> int:
    """
    Utility: scan entire CSV and ensure protected=true for any row
    where sales_count >= PROTECTION_THRESHOLD. Returns count of rows updated.

    Run this anytime you manually edit sales_count values.
    """
    if not os.path.exists(csv_path):
        return 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader     = csv.DictReader(f)
        rows       = list(reader)
        fieldnames = list(reader.fieldnames or [])

    updated = 0
    for row in rows:
        count = int(row.get("sales_count") or 0)
        if count >= PROTECTION_THRESHOLD and row.get("protected") != "true":
            row["protected"] = "true"
            updated += 1
        elif count < PROTECTION_THRESHOLD and row.get("protected") == "true":
            row["protected"] = "false"
            updated += 1

    if updated:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    return updated
