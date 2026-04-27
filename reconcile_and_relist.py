"""
reconcile_and_relist.py
───────────────────────────────────────────────────────────────────────────────
Finds books that were delisted on eBay but still show status=active in CSV,
then relists them using the full Inventory API publish pipeline.

NOTE ON DETECTION METHOD:
  GET /sell/inventory/v1/offer (base, no sku) returns error 25707 regardless
  of headers or pagination params — confirmed across 3 attempts. Detection
  uses GET /sell/inventory/v1/offer/{offerId} per active CSV row instead.

USAGE
  python reconcile_and_relist.py               # dry run: print mismatches only
  python reconcile_and_relist.py --relist      # relist all mismatches on eBay
  python reconcile_and_relist.py --relist --push  # relist + git push both CSVs

EBAY POLICIES
  Fulfillment : 391308514023
  Payment     : 391308491023
  Return      : 391308498023
  Location    : home1
  Category    : 261186
  Quantity    : 20

CSV PATHS
  Master : E:\\Book\\Lister\\booksgoat_enhanced.csv
  Repo   : E:\\Book\\Scanner\\booksgoat_enhanced.csv

ENV VARS REQUIRED
  EBAY_APP_ID  EBAY_CERT_ID  EBAY_REFRESH_TOKEN
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

FULFILLMENT_POLICY = "391308514023"
PAYMENT_POLICY     = "391308491023"
RETURN_POLICY      = "391308498023"
MERCHANT_LOCATION  = "home1"
CATEGORY_ID        = "261186"
QUANTITY           = 20
MARKETPLACE_ID     = "EBAY_US"

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_OFFER_URL = "https://api.ebay.com/sell/inventory/v1/offer"
EBAY_INV_URL   = "https://api.ebay.com/sell/inventory/v1/inventory_item"

# ─── CONDITION MAP ───────────────────────────────────────────────────────────

CONDITION_MAP = {
    "new":          "NEW",
    "like new":     "LIKE_NEW",
    "like_new":     "LIKE_NEW",
    "very good":    "VERY_GOOD",
    "very_good":    "VERY_GOOD",
    "good":         "GOOD",
    "acceptable":   "ACCEPTABLE",
    "poor":         "FOR_PARTS_OR_NOT_WORKING",
}

def map_condition(raw):
    return CONDITION_MAP.get(str(raw).lower().strip(), "VERY_GOOD")


# ─── COLUMN RESOLVER ─────────────────────────────────────────────────────────

def resolve_columns(fieldnames):
    """
    Build a case-insensitive column map from actual CSV headers.
    Tries common aliases for isbn and price so the script works
    regardless of exact column naming in booksgoat_enhanced.csv.
    """
    lower_map = {f.lower(): f for f in fieldnames}

    def find(candidates):
        for c in candidates:
            if c.lower() in lower_map:
                return lower_map[c.lower()]
        return None

    cols = {
        "isbn":      find(["isbn13", "isbn", "sku", "ISBN13", "ISBN", "SKU"]),
        "offer_id":  find(["offer_id", "offerId", "offer id"]),
        "status":    find(["status"]),
        "title":     find(["title", "book_title", "name"]),
        "price":     find(["price", "list_price", "listing_price", "sell_price"]),
        "condition": find(["condition", "book_condition"]),
        "description": find(["description", "desc", "ai_description"]),
    }

    # Required columns — abort if missing
    for req in ("isbn", "offer_id", "status"):
        if not cols[req]:
            raise ValueError(
                f"Required column '{req}' not found in CSV. "
                f"Available columns: {fieldnames}"
            )

    return cols


def get(row, cols, key, default=""):
    """Read a value from row using resolved column name."""
    col = cols.get(key)
    if col and col in row:
        return row[col]
    return default


# ─── OAUTH ───────────────────────────────────────────────────────────────────

def get_access_token():
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


def make_headers(token):
    return {
        "Authorization":           f"Bearer {token}",
        "Content-Type":            "application/json",
        "Accept":                  "application/json",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
        "Content-Language":        "en-US",
    }


# ─── DETECTION ───────────────────────────────────────────────────────────────

def check_offers_by_id(token, offer_ids):
    """
    Per-offer-id check — only reliable Inventory API detection method.
    Returns (live_ids, dead_ids) as sets of offer_id strings.
    """
    headers  = make_headers(token)
    live_ids = set()
    dead_ids = set()
    total    = len(offer_ids)

    print(f"  Checking {total} active offer IDs against eBay...")

    for i, offer_id in enumerate(sorted(offer_ids), 1):
        resp = requests.get(
            f"{EBAY_OFFER_URL}/{offer_id}",
            headers=headers, timeout=30
        )

        if resp.status_code == 200:
            status = resp.json().get("status", "").upper()
            if status == "PUBLISHED":
                live_ids.add(offer_id)
            else:
                dead_ids.add(offer_id)
        elif resp.status_code == 404:
            dead_ids.add(offer_id)
        else:
            print(f"  ! {resp.status_code} for offer {offer_id} — skipping")

        if i % 25 == 0 or i == total:
            print(f"  {i} / {total} checked...")
        if i % 50 == 0:
            time.sleep(1)

    print(f"  {len(live_ids)} live, {len(dead_ids)} dead/missing.\n")
    return live_ids, dead_ids


# ─── RELIST PIPELINE ─────────────────────────────────────────────────────────

def put_inventory_item(token, row, cols):
    sku   = get(row, cols, "isbn").strip()
    title = get(row, cols, "title").strip()
    desc  = get(row, cols, "description") or title

    # condition omitted -- CSV has no condition column and sending
    # "VERY_GOOD" causes error 2004 "Could not serialize field [condition]"
    # on all books. eBay may auto-assign condition from ISBN catalog match.
    payload = {
        "product": {
            "title":       title,
            "description": desc,
            "isbn":        [sku],
        },
        "availability": {
            "shipToLocationAvailability": {
                "quantity": QUANTITY
            }
        },
    }

    resp = requests.put(
        f"{EBAY_INV_URL}/{sku}",
        headers=make_headers(token),
        json=payload,
        timeout=30,
    )
    if resp.status_code in (200, 204):
        return True, None
    return False, f"PUT inventory_item {resp.status_code}: {resp.text}"


def delete_old_offer(token, offer_id):
    resp = requests.delete(
        f"{EBAY_OFFER_URL}/{offer_id}",
        headers=make_headers(token),
        timeout=30,
    )
    if resp.status_code in (200, 204, 404):
        return True, None
    return False, f"DELETE offer {resp.status_code}: {resp.text[:200]}"


def post_offer(token, row, cols):
    sku   = get(row, cols, "isbn").strip()
    price = get(row, cols, "price") or "0"

    payload = {
        "sku":               sku,
        "marketplaceId":     MARKETPLACE_ID,
        "format":            "FIXED_PRICE",
        "availableQuantity": QUANTITY,
        "categoryId":        CATEGORY_ID,
        "merchantLocationKey": MERCHANT_LOCATION,
        "pricingSummary": {
            "price": {"currency": "USD", "value": str(price)}
        },
        "listingPolicies": {
            "fulfillmentPolicyId": FULFILLMENT_POLICY,
            "paymentPolicyId":     PAYMENT_POLICY,
            "returnPolicyId":      RETURN_POLICY,
        },
        "includeCatalogProductDetails": True,
    }

    resp = requests.post(
        EBAY_OFFER_URL,
        headers=make_headers(token),
        json=payload,
        timeout=30,
    )
    if resp.status_code in (200, 201):
        return resp.json().get("offerId", ""), None
    return None, f"POST offer {resp.status_code}: {resp.text[:200]}"


def publish_offer(token, offer_id):
    resp = requests.post(
        f"{EBAY_OFFER_URL}/{offer_id}/publish",
        headers=make_headers(token),
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json().get("listingId", ""), None
    return None, f"publish {resp.status_code}: {resp.text[:200]}"


def relist_book(token, row, cols):
    """Full relist pipeline. Returns (new_offer_id, error_message)."""
    old_offer_id = get(row, cols, "offer_id").strip()

    ok, err = put_inventory_item(token, row, cols)
    if not ok:
        return None, f"Step 1 PUT failed: {err}"

    ok, err = delete_old_offer(token, old_offer_id)
    if not ok:
        return None, f"Step 2 DELETE failed: {err}"

    new_offer_id, err = post_offer(token, row, cols)
    if not new_offer_id:
        return None, f"Step 3 POST offer failed: {err}"

    listing_id, err = publish_offer(token, new_offer_id)
    if not listing_id:
        return new_offer_id, f"Step 4 publish failed: {err}"

    return new_offer_id, None


# ─── CSV HELPERS ──────────────────────────────────────────────────────────────

def load_csv(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV not found: {path}")
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)
        fields = reader.fieldnames or []
    if not rows:
        raise ValueError(f"CSV is empty: {path}")
    return rows, fields


def write_csv(path, rows, fieldnames):
    for col in ("delisted_at", "relisted_at"):
        if col not in fieldnames:
            fieldnames.append(col)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ─── GIT PUSH ─────────────────────────────────────────────────────────────────

def git_push(scanner_dir):
    ts_label   = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit_msg = f"reconcile: relist delisted books post-canceled repricer run [{ts_label}]"
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

def print_mismatch_table(mismatches, cols, dry_run):
    mode = "DRY RUN" if dry_run else "RELIST MODE"
    print()
    print("=" * 90)
    print(f"  RECONCILE REPORT  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]  --  {mode}")
    print("=" * 90)

    if not mismatches:
        print("  No mismatches -- CSV is in sync with eBay.")
    else:
        print(f"  Books active in CSV but NOT live on eBay: {len(mismatches)}")
        print()
        print(f"  {'SKU/ISBN':<18}  {'offer_id':<16}  {'Price':>7}  {'Title'}")
        print(f"  {'-'*18}  {'-'*16}  {'-'*7}  {'-'*38}")
        for m in mismatches:
            isbn     = get(m, cols, "isbn")
            offer_id = get(m, cols, "offer_id")
            price    = get(m, cols, "price") or "0"
            title    = get(m, cols, "title")[:38]
            try:
                price_fmt = f"${float(price):>6.2f}"
            except (ValueError, TypeError):
                price_fmt = f"{'?':>7}"
            print(f"  {isbn:<18}  {offer_id:<16}  {price_fmt}  {title}")

        if dry_run:
            print()
            print("  Re-run with --relist to relist all of the above on eBay.")
            print("  Add --push to also commit and push both CSVs to GitHub.")
    print("=" * 90)
    print()


def print_relist_results(results):
    succeeded = [r for r in results if r["success"]]
    failed    = [r for r in results if not r["success"]]

    print()
    print("=" * 90)
    print(f"  RELIST RESULTS  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
    print("=" * 90)
    print(f"  Attempted : {len(results)}")
    print(f"  Succeeded : {len(succeeded)}")
    print(f"  Failed    : {len(failed)}")

    if succeeded:
        print()
        print(f"  {'SKU/ISBN':<18}  {'new offer_id':<16}  {'Title'}")
        print(f"  {'-'*18}  {'-'*16}  {'-'*38}")
        for r in succeeded:
            print(f"  {r['isbn']:<18}  {r['new_offer_id']:<16}  {r['title'][:38]}")

    if failed:
        print()
        print("  FAILURES:")
        for r in failed:
            print(f"  {r['isbn']} -- {r['error']}")

    print("=" * 90)
    print()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--relist", action="store_true",
                        help="Relist all mismatched books on eBay and update CSV.")
    parser.add_argument("--push",   action="store_true",
                        help="Git push both CSVs after relist (requires --relist).")
    args = parser.parse_args()

    if args.push and not args.relist:
        print("ERROR: --push requires --relist.")
        sys.exit(1)

    dry_run = not args.relist

    print()
    print("=" * 64)
    print("  BooksGoat -- Reconcile & Relist")
    print("=" * 64)
    print()

    # Step 1 — Load CSV
    print("Step 1: Loading CSV...")
    try:
        rows, fieldnames = load_csv(LISTER_CSV)
        cols             = resolve_columns(fieldnames)
    except Exception as e:
        print(f"  FAILED: {e}")
        sys.exit(1)

    active_rows = [
        r for r in rows
        if get(r, cols, "status").strip().lower() == "active"
    ]
    active_offer_ids = {
        get(r, cols, "offer_id").strip()
        for r in active_rows
        if get(r, cols, "offer_id").strip()
    }

    print(f"  {len(rows)} total rows, {len(active_rows)} active, "
          f"{len(active_offer_ids)} unique offer IDs.")
    print(f"  Column map: isbn={cols['isbn']}, offer_id={cols['offer_id']}, "
          f"price={cols['price']}, status={cols['status']}\n")

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

    # Step 3 — Detect dead offers
    print("Step 3: Checking offer IDs against eBay...")
    try:
        live_ids, dead_ids = check_offers_by_id(token, active_offer_ids)
    except Exception as e:
        print(f"  eBay API error: {e}")
        sys.exit(1)

    # Step 4 — Build mismatch list
    mismatches = [
        r for r in active_rows
        if get(r, cols, "offer_id").strip() in dead_ids
    ]

    print_mismatch_table(mismatches, cols, dry_run)

    if dry_run or not mismatches:
        sys.exit(0)

    # Step 5 — Relist
    print(f"Step 4: Relisting {len(mismatches)} books on eBay...")
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build lookup by offer_id to patch original rows in place
    offer_col = cols["offer_id"]
    row_by_offer_id = {r.get(offer_col, "").strip(): r for r in rows}

    results = []
    for i, mismatch in enumerate(mismatches, 1):
        isbn         = get(mismatch, cols, "isbn")
        title        = get(mismatch, cols, "title")[:40]
        old_offer_id = get(mismatch, cols, "offer_id").strip()

        print(f"  [{i}/{len(mismatches)}] {isbn} -- {title}")

        new_offer_id, err = relist_book(token, mismatch, cols)

        target = row_by_offer_id.get(old_offer_id)

        if new_offer_id and not err:
            if target:
                target[cols["status"]]   = "active"
                target[offer_col]        = new_offer_id
                target["relisted_at"]    = now_ts
                target.pop("delisted_at", None)
            results.append({"success": True,  "isbn": isbn, "title": title,
                            "new_offer_id": new_offer_id, "error": None})
            print(f"    OK -- new offer_id: {new_offer_id}")

        elif new_offer_id and err:
            if target:
                target[offer_col] = new_offer_id
            results.append({"success": False, "isbn": isbn, "title": title,
                            "new_offer_id": new_offer_id, "error": err})
            print(f"    PARTIAL -- offer created, publish failed: {err}")

        else:
            results.append({"success": False, "isbn": isbn, "title": title,
                            "new_offer_id": "", "error": err})
            print(f"    FAILED -- {err}")

        if i < len(mismatches):
            time.sleep(0.5)

    print_relist_results(results)

    # Step 6 — Write CSVs
    print("Step 5: Writing updated CSVs...")
    try:
        write_csv(LISTER_CSV, rows, list(fieldnames))
        print(f"  Written: {LISTER_CSV}")
    except Exception as e:
        print(f"  FAILED to write Lister CSV: {e}")
        sys.exit(1)
    try:
        write_csv(SCANNER_CSV, rows, list(fieldnames))
        print(f"  Written: {SCANNER_CSV}\n")
    except Exception as e:
        print(f"  FAILED to write Scanner CSV: {e}")
        sys.exit(1)

    # Step 7 — Git push
    if args.push:
        print("Step 6: Pushing to GitHub...")
        try:
            git_push(SCANNER_DIR)
            print("  Pushed to github.com/njubran82/Scanner\n")
        except Exception as e:
            print(f"  Git push failed: {e}")
            print("  CSVs written successfully -- push manually if needed.")
            sys.exit(1)

    succeeded_count = sum(1 for r in results if r["success"])
    print(f"Done. {succeeded_count} / {len(mismatches)} books relisted successfully.\n")


if __name__ == "__main__":
    main()
