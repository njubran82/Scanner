"""
reconcile_listings.py
Compares eBay live published offers against booksgoat_enhanced.csv rows
where status=active. Any row CSV thinks is active but eBay no longer has
published is marked status=delisted with a delisted_at timestamp.

WHY PER-OFFER LOOKUP:
  The eBay Inventory API has no "list all offers" endpoint. The base
  GET /sell/inventory/v1/offer call requires a sku parameter and returns
  error 25707 without one. We check each active offer_id from the CSV
  individually via GET /sell/inventory/v1/offer/{offerId}.

USAGE
  python reconcile_listings.py                  # dry run, no changes written
  python reconcile_listings.py --commit         # write updated CSVs to disk
  python reconcile_listings.py --commit --push  # write CSVs + git push repo copy

PATHS (edit below if needed)
  LISTER_CSV  = E:\\Book\\Lister\\booksgoat_enhanced.csv   (master copy)
  SCANNER_CSV = E:\\Book\\Scanner\\booksgoat_enhanced.csv  (git repo copy)

ENV VARS REQUIRED
  EBAY_APP_ID
  EBAY_CERT_ID
  EBAY_REFRESH_TOKEN
"""

import os
import sys
import csv
import time
import argparse
import subprocess
import requests
from datetime import datetime, timezone

# ─── CONFIG ──────────────────────────────────────────────────────────────────

LISTER_CSV  = r"E:\Book\Lister\booksgoat_enhanced.csv"
SCANNER_CSV = r"E:\Book\Scanner\booksgoat_enhanced.csv"
SCANNER_DIR = r"E:\Book\Scanner"

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_OFFER_URL = "https://api.ebay.com/sell/inventory/v1/offer"


# ─── OAUTH ───────────────────────────────────────────────────────────────────

def get_access_token():
    """Exchange refresh token for a short-lived access token."""
    app_id      = os.environ["EBAY_APP_ID"]
    cert_id     = os.environ["EBAY_CERT_ID"]
    refresh_tok = os.environ["EBAY_REFRESH_TOKEN"]

    resp = requests.post(
        EBAY_OAUTH_URL,
        auth=(app_id, cert_id),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_tok,
            "scope":         "https://api.ebay.com/oauth/api_scope/sell.inventory",
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"OAuth failed: {resp.text}")
    return token


# ─── EBAY INVENTORY API ───────────────────────────────────────────────────────

def check_offers_by_id(access_token, offer_ids):
    """
    GET /sell/inventory/v1/offer/{offerId} for each offer_id.

    The Inventory API GET /sell/inventory/v1/offer base endpoint requires
    a sku query parameter (error 25707 without it). There is no bulk
    "list all offers" call. We check each active CSV offer_id directly.

    Returns:
      live_offer_ids  -- set of offer_ids confirmed PUBLISHED on eBay
      missing_ids     -- set of offer_ids that returned 404 (fully gone)
      ended_ids       -- set of offer_ids present but status != PUBLISHED
    """
    headers = {
        "Authorization":           f"Bearer {access_token}",
        "Content-Type":            "application/json",
        "Accept":                  "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    live_offer_ids = set()
    missing_ids    = set()
    ended_ids      = set()
    total          = len(offer_ids)

    print(f"  Checking {total} active offer IDs against eBay...")

    for i, offer_id in enumerate(sorted(offer_ids), 1):
        url  = f"{EBAY_OFFER_URL}/{offer_id}"
        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code == 200:
            status = resp.json().get("status", "").upper()
            if status == "PUBLISHED":
                live_offer_ids.add(offer_id)
            else:
                ended_ids.add(offer_id)

        elif resp.status_code == 404:
            missing_ids.add(offer_id)

        else:
            print(f"  ! {resp.status_code} for offer {offer_id}: {resp.text[:120]}")

        if i % 25 == 0 or i == total:
            print(f"  Progress: {i} / {total}")
        if i % 50 == 0:
            time.sleep(1)  # eBay rate limit: ~5000 Inventory API calls/day

    print(
        f"  Done -- {len(live_offer_ids)} PUBLISHED, "
        f"{len(ended_ids)} ended/non-published, "
        f"{len(missing_ids)} not found (404).\n"
    )
    return live_offer_ids, missing_ids, ended_ids


# ─── CSV HELPERS ──────────────────────────────────────────────────────────────

def load_csv(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV not found: {path}")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"CSV is empty: {path}")
    required = {"offer_id", "status"}
    missing  = required - set(rows[0].keys())
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")
    return rows


def write_csv(path, rows):
    fieldnames = list(rows[0].keys())
    if "delisted_at" not in fieldnames:
        fieldnames.append("delisted_at")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ─── RECONCILE LOGIC ─────────────────────────────────────────────────────────

def reconcile(rows, live_offer_ids, missing_ids, ended_ids):
    now_ts     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mismatches = []
    dead_ids   = missing_ids | ended_ids

    for row in rows:
        if row.get("status", "").strip().lower() != "active":
            continue
        offer_id = str(row.get("offer_id", "")).strip()
        if not offer_id:
            continue
        if offer_id in dead_ids:
            reason = "404 not found" if offer_id in missing_ids else "not PUBLISHED"
            mismatches.append({
                "isbn":     row.get("isbn", ""),
                "title":    row.get("title", "")[:45],
                "offer_id": offer_id,
                "reason":   reason,
            })
            row["status"]      = "delisted"
            row["delisted_at"] = now_ts

    return rows, mismatches


# ─── GIT PUSH ─────────────────────────────────────────────────────────────────

def git_push(scanner_dir):
    ts_label   = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit_msg = (
        f"reconcile: fix stale active status post-canceled repricer run [{ts_label}]"
    )
    cmds = [
        ["git", "-C", scanner_dir, "add",    "booksgoat_enhanced.csv"],
        ["git", "-C", scanner_dir, "commit", "-m", commit_msg],
        ["git", "-C", scanner_dir, "push",   "origin"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Git failed: {' '.join(cmd[2:])}\n"
                f"{result.stdout}\n{result.stderr}"
            )
        print(f"  OK: {' '.join(cmd[2:])}")


# ─── REPORT ──────────────────────────────────────────────────────────────────

def print_report(mismatches, csv_active_count, live_count,
                 missing_count, ended_count, dry_run):
    sep  = "-" * 88
    mode = "DRY RUN -- no changes written" if dry_run else "CHANGES WRITTEN TO DISK"

    print()
    print("=" * 88)
    print(f"  RECONCILIATION REPORT  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]  --  {mode}")
    print("=" * 88)
    print(f"  CSV active rows checked   : {csv_active_count}")
    print(f"  Confirmed PUBLISHED       : {live_count}")
    print(f"  404 not found             : {missing_count}")
    print(f"  Ended / not PUBLISHED     : {ended_count}")
    print(sep)

    if not mismatches:
        print("  No mismatches -- CSV is already in sync with eBay.")
    else:
        print(f"  Mismatches (active in CSV, not live on eBay): {len(mismatches)}")
        print()
        print(f"  {'ISBN':<18}  {'offer_id':<16}  {'Reason':<18}  {'Title'}")
        print(f"  {'-'*18}  {'-'*16}  {'-'*18}  {'-'*36}")
        for m in mismatches:
            print(
                f"  {m['isbn']:<18}  {m['offer_id']:<16}  "
                f"{m['reason']:<18}  {m['title']}"
            )
        print()
        if dry_run:
            print("  Re-run with --commit to write status=delisted to both CSV files.")
            print("  Re-run with --commit --push to also push to GitHub.")

    print("=" * 88)
    print()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="Write updated CSVs to disk.")
    parser.add_argument("--push",   action="store_true",
                        help="Push Scanner CSV to GitHub (requires --commit).")
    args = parser.parse_args()

    if args.push and not args.commit:
        print("ERROR: --push requires --commit.")
        sys.exit(1)

    dry_run = not args.commit

    print()
    print("=" * 60)
    print("  BooksGoat -- eBay Listing Reconciliation")
    print("=" * 60)
    print()

    # Step 1 — Load CSV (need offer_ids before hitting eBay)
    print("Step 1: Loading CSV...")
    try:
        rows             = load_csv(LISTER_CSV)
        active_rows      = [r for r in rows
                            if r.get("status", "").strip().lower() == "active"]
        csv_active_count = len(active_rows)
        active_offer_ids = {
            str(r["offer_id"]).strip()
            for r in active_rows
            if r.get("offer_id", "").strip()
        }
        print(f"  {len(rows)} total rows, {csv_active_count} active, "
              f"{len(active_offer_ids)} unique offer IDs.\n")
    except Exception as e:
        print(f"  FAILED: {e}")
        sys.exit(1)

    if not active_offer_ids:
        print("  No active rows found. Nothing to reconcile.")
        sys.exit(0)

    # Step 2 — OAuth
    print("Step 2: Authenticating with eBay...")
    try:
        token = get_access_token()
        print("  Access token obtained.\n")
    except Exception as e:
        print(f"  OAuth failed: {e}")
        sys.exit(1)

    # Step 3 — Check each offer_id
    print("Step 3: Checking offer IDs against eBay Inventory API...")
    try:
        live_ids, missing_ids, ended_ids = check_offers_by_id(
            token, active_offer_ids
        )
    except Exception as e:
        print(f"  eBay API error: {e}")
        sys.exit(1)

    # Step 4 — Reconcile
    print("Step 4: Building mismatch list...")
    updated_rows, mismatches = reconcile(rows, live_ids, missing_ids, ended_ids)

    # Step 5 — Report
    print_report(
        mismatches, csv_active_count,
        len(live_ids), len(missing_ids), len(ended_ids),
        dry_run
    )

    if dry_run:
        sys.exit(0)

    # Step 6 — Write CSVs
    print("Step 5: Writing updated CSVs...")
    try:
        write_csv(LISTER_CSV, updated_rows)
        print(f"  Written: {LISTER_CSV}")
    except Exception as e:
        print(f"  Failed to write Lister CSV: {e}")
        sys.exit(1)

    try:
        write_csv(SCANNER_CSV, updated_rows)
        print(f"  Written: {SCANNER_CSV}\n")
    except Exception as e:
        print(f"  Failed to write Scanner CSV: {e}")
        sys.exit(1)

    # Step 7 — Git push (optional)
    if args.push:
        print("Step 6: Pushing to GitHub...")
        try:
            git_push(SCANNER_DIR)
            print("  Pushed to github.com/njubran82/Scanner\n")
        except Exception as e:
            print(f"  Git push failed: {e}")
            print("  CSVs were written -- push manually if needed.")
            sys.exit(1)

    print("Reconciliation complete.\n")


if __name__ == "__main__":
    main()
