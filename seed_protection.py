"""
seed_protection.py
───────────────────────────────────────────────────────────────────────────────
ONE-TIME migration script. Run once to:
  1. Add sales_count and protected columns to booksgoat_enhanced.csv
  2. Seed known high-velocity books from order history
  3. Auto-set protected=true for any row with sales_count >= 3
  4. Write both CSV paths and push to GitHub

USAGE
  python seed_protection.py          # dry run — print changes, write nothing
  python seed_protection.py --commit # write both CSVs
  python seed_protection.py --commit --push  # write + git push

CSV PATHS
  Master : E:\\Book\\Lister\\booksgoat_enhanced.csv
  Repo   : E:\\Book\\Scanner\\booksgoat_enhanced.csv
"""

import os
import sys
import csv
import argparse
import subprocess
from datetime import datetime

# ─── CONFIG ──────────────────────────────────────────────────────────────────

LISTER_CSV  = r"E:\Book\Lister\booksgoat_enhanced.csv"
SCANNER_CSV = r"E:\Book\Scanner\booksgoat_enhanced.csv"
SCANNER_DIR = r"E:\Book\Scanner"

PROTECTION_THRESHOLD = 3  # sales_count >= this → protected = true

# Known high-velocity books seeded from eBay order history.
# Key: isbn13 (string). Value: known minimum sales count.
# Update these counts anytime you have better data.
SEED_SALES = {
    "9780521809269": 40,  # Art of Electronics, 3rd Ed
    "9780899705538": 8,   # Guides to Evaluation of Permanent Impairment 6th Ed
    "9781560915263": 3,   # Race Car Vehicle Dynamics
    "9781285080475": 2,   # Milady Standard Nail Technology, 7th Ed
    # Sterile Processing CRCST — add ISBN when confirmed
    # ACI 318-19 Building Code — add ISBN when confirmed
    # Zero Bone Loss Concepts — add ISBN when confirmed
}


# ─── CSV HELPERS ─────────────────────────────────────────────────────────────

def load_csv(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV not found: {path}")
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)
        fields = list(reader.fieldnames or [])
    if not rows:
        raise ValueError(f"CSV is empty: {path}")
    return rows, fields


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ─── GIT PUSH ─────────────────────────────────────────────────────────────────

def git_push(scanner_dir):
    ts_label   = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit_msg = f"feat: add sales_count + protected columns, seed high-velocity books [{ts_label}]"
    cmds = [
        ["git", "-C", scanner_dir, "add",    "booksgoat_enhanced.csv"],
        ["git", "-C", scanner_dir, "commit", "-m", commit_msg],
        ["git", "-C", scanner_dir, "push",   "origin"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Git failed: {' '.join(cmd[2:])}\n{result.stdout}\n{result.stderr}"
            )
        print(f"  OK: {' '.join(cmd[2:])}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="Write updated CSVs.")
    parser.add_argument("--push",   action="store_true", help="Git push (requires --commit).")
    args = parser.parse_args()

    if args.push and not args.commit:
        print("ERROR: --push requires --commit.")
        sys.exit(1)

    dry_run = not args.commit

    print()
    print("=" * 60)
    print("  BooksGoat -- Seed Protection Columns")
    print("=" * 60)
    print()

    # Load
    print("Step 1: Loading CSV...")
    rows, fieldnames = load_csv(LISTER_CSV)
    print(f"  {len(rows)} rows loaded.\n")

    # Add columns if missing
    if "sales_count" not in fieldnames:
        fieldnames.append("sales_count")
        print("  Added column: sales_count")
    if "protected" not in fieldnames:
        fieldnames.append("protected")
        print("  Added column: protected")

    # Find isbn column
    lower_map = {f.lower(): f for f in fieldnames}
    isbn_col  = lower_map.get("isbn13") or lower_map.get("isbn") or lower_map.get("sku")
    if not isbn_col:
        print("  ERROR: Cannot find isbn/isbn13/sku column.")
        sys.exit(1)

    # Apply seeds and protection logic
    seeded    = []
    protected = []

    for row in rows:
        isbn = str(row.get(isbn_col, "")).strip()

        # Initialize missing columns
        if not row.get("sales_count", "").strip():
            row["sales_count"] = "0"
        if not row.get("protected", "").strip():
            row["protected"] = "false"

        # Apply seed override if this ISBN has known sales
        if isbn in SEED_SALES:
            existing = int(row.get("sales_count") or 0)
            seeded_val = SEED_SALES[isbn]
            if seeded_val > existing:
                row["sales_count"] = str(seeded_val)
                seeded.append((isbn, row.get("title", "")[:45], seeded_val))

        # Apply protection threshold
        count = int(row.get("sales_count") or 0)
        if count >= PROTECTION_THRESHOLD:
            if row.get("protected") != "true":
                row["protected"] = "true"
                protected.append((isbn, row.get("title", "")[:45], count))
        else:
            row["protected"] = "false"

    # Report
    print()
    print("=" * 70)
    print(f"  MIGRATION REPORT  --  {'DRY RUN' if dry_run else 'COMMITTING'}")
    print("=" * 70)
    print(f"  Columns ensured  : sales_count, protected")
    print(f"  Seed overrides   : {len(seeded)}")
    print(f"  Books protected  : {len(protected)}")

    if seeded:
        print()
        print(f"  {'ISBN':<18}  {'sales_count':>11}  {'Title'}")
        print(f"  {'-'*18}  {'-'*11}  {'-'*45}")
        for isbn, title, count in seeded:
            print(f"  {isbn:<18}  {count:>11}  {title}")

    if protected:
        print()
        print(f"  Protected (sales_count >= {PROTECTION_THRESHOLD}):")
        print(f"  {'ISBN':<18}  {'sales_count':>11}  {'Title'}")
        print(f"  {'-'*18}  {'-'*11}  {'-'*45}")
        for isbn, title, count in protected:
            print(f"  {isbn:<18}  {count:>11}  {title}")

    if dry_run:
        print()
        print("  Re-run with --commit to write changes.")
        print("  Re-run with --commit --push to also push to GitHub.")
    print("=" * 70)
    print()

    if dry_run:
        sys.exit(0)

    # Write
    print("Step 2: Writing CSVs...")
    write_csv(LISTER_CSV, rows, fieldnames)
    print(f"  Written: {LISTER_CSV}")
    write_csv(SCANNER_CSV, rows, fieldnames)
    print(f"  Written: {SCANNER_CSV}\n")

    # Push
    if args.push:
        print("Step 3: Pushing to GitHub...")
        git_push(SCANNER_DIR)
        print("  Pushed to github.com/njubran82/Scanner\n")

    print("Migration complete.\n")


if __name__ == "__main__":
    main()
