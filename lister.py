# ============================================================
# lister.py
# Reads scanner_results.csv, filters profitable opportunities,
# and creates eBay listings via the Inventory API.
#
# HOW TO USE:
#   1. Run ebay_auth_setup.py once to authorize your account
#   2. Fill in your policy IDs in lister_config.py
#   3. Set DRY_RUN = True in lister_config.py to preview first
#   4. Run: python lister.py
#   5. When happy, set DRY_RUN = False and run again to go live
# ============================================================

import csv
import json
import os
import sys
import time
import requests
from datetime import datetime

# Fix Windows console encoding for emoji characters
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import lister_config as cfg
from lister_auth import get_auth_headers

# eBay Inventory API base URL
BASE_URL = "https://api.ebay.com/sell/inventory/v1"


# ==============================================================
# SECTION 1: Load & filter scanner CSV
# ==============================================================

def load_opportunities(csv_path):
    """
    Reads scanner_results.csv and returns a list of rows
    that pass the profit, margin, confidence, and concern filters.
    """
    if not os.path.exists(csv_path):
        print(f"❌ Scanner CSV not found: {csv_path}")
        return []

    opportunities = []
    skipped = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            skip_reason = should_skip(row)
            if skip_reason:
                skipped.append((row.get("Title", "Unknown"), skip_reason))
            else:
                opportunities.append(row)

    print(f"\n📋 Scanner CSV loaded: {len(opportunities)} opportunities pass filters, {len(skipped)} skipped.")
    if skipped:
        print(f"\n⏭️  Skipped:")
        for title, reason in skipped[:10]:  # Show first 10 to avoid spam
            print(f"   - {title[:55]}: {reason}")
        if len(skipped) > 10:
            print(f"   ... and {len(skipped) - 10} more.")

    return opportunities


def should_skip(row):
    """
    Returns a skip reason string if this row should be excluded,
    or None if it should be listed.
    """
    # --- Confidence filter ---
    raw_confidence = row.get("Confidence", "").strip()
    # Strip emoji prefixes like "🟠 LOW" → "LOW"
    confidence = raw_confidence.split()[-1] if raw_confidence else ""
    if confidence not in cfg.ALLOWED_CONFIDENCE:
        return f"Confidence '{confidence}' not in allowed list"

    # --- Profit filter ---
    try:
        profit = float(row.get("Profit", "0").replace("$", "").replace(",", ""))
        if profit < cfg.MIN_PROFIT:
            return f"Profit ${profit:.2f} below minimum ${cfg.MIN_PROFIT}"
    except ValueError:
        return "Could not parse Profit value"

    # --- Margin filter ---
    try:
        margin_str = row.get("Margin", "0").replace("%", "").strip()
        margin = float(margin_str)
        if margin < cfg.MIN_MARGIN:
            return f"Margin {margin:.1f}% below minimum {cfg.MIN_MARGIN}%"
    except ValueError:
        return "Could not parse Margin value"

    # --- Concern flags filter ---
    concerns = row.get("Concerns", "")
    for bad_flag in cfg.SKIP_IF_CONCERNS:
        if bad_flag in concerns:
            return f"Has concern flag: {bad_flag}"

    # --- ISBN required ---
    isbn = row.get("ISBN-13", "").strip()
    if not isbn or len(isbn) < 10:
        return "Missing or invalid ISBN-13"

    return None  # All good — include this one


# ==============================================================
# SECTION 2: Load state (to avoid re-listing duplicates)
# ==============================================================

def load_state():
    """
    Loads lister state from disk.
    'listed_isbns' = flat list for quick duplicate checks.
    'listings'     = dict of ISBN → offer/listing details (used by auto_delist).
    """
    if os.path.exists(cfg.LISTER_STATE):
        with open(cfg.LISTER_STATE, "r") as f:
            state = json.load(f)
        # Ensure listings dict exists for older state files
        if "listings" not in state:
            state["listings"] = {}
        return state
    return {"listed_isbns": [], "listings": {}, "last_run": None}



def save_state(state):
    """Saves updated state back to disk."""
    with open(cfg.LISTER_STATE, "w") as f:
        json.dump(state, f, indent=2)


# ==============================================================
# SECTION 3: Build eBay API payloads
# ==============================================================

def calculate_price(row):
    """
    Determine the listing price based on PRICING_MODE in config.
    Either matches the scanner revenue price or undercuts slightly.
    """
    try:
        revenue = float(row.get("Revenue", "0").replace("$", "").replace(",", ""))
    except ValueError:
        revenue = 0.0

    if cfg.PRICING_MODE == "UNDERCUT":
        return max(round(revenue - cfg.UNDERCUT_AMOUNT, 2), 1.00)
    else:
        return round(revenue, 2)


def get_cover_image_url(isbn):
    """
    Looks up a book cover image URL using Open Library's cover API.
    Returns a URL string if found, or None if not available.
    Open Library serves cover images directly by ISBN — no API key needed.
    """
    # Open Library cover URL — returns the image directly
    # We check if it resolves to a real image (not a placeholder)
    url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
    try:
        response = requests.head(url, timeout=5, allow_redirects=True)
        # Open Library returns a 1x1 placeholder if no cover exists
        # Real covers are much larger — check Content-Length
        content_length = int(response.headers.get("Content-Length", 0))
        if response.status_code == 200 and content_length > 1000:
            return url
    except Exception:
        pass

    # Fallback: Google Books thumbnail
    try:
        gb_url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}"
        response = requests.get(gb_url, timeout=5)
        if response.status_code == 200:
            items = response.json().get("items", [])
            if items:
                image_links = items[0].get("volumeInfo", {}).get("imageLinks", {})
                # Prefer large thumbnail
                img = image_links.get("large") or image_links.get("thumbnail")
                if img:
                    # Convert to https and remove curl parameter
                    img = img.replace("http://", "https://").replace("&edge=curl", "")
                    return img
    except Exception:
        pass

    return None


def build_inventory_item_payload(row):
    """
    Builds the JSON payload for PUT /inventory_item/{sku}
    This tells eBay what the product IS (title, description, ISBN).
    Automatically fetches cover image from Open Library or Google Books.
    """
    title = row.get("Title", "").strip()
    # eBay title max = 80 chars
    if len(title) > 80:
        title = title[:77] + "..."

    isbn = row.get("ISBN-13", "").strip()

    # Try to get cover image automatically
    cover_url = get_cover_image_url(isbn)
    if cover_url:
        print(f"  🖼️  Cover image found.")
    else:
        print(f"  ⚠️  No cover image found — listing may require manual photo.")

    payload = {
        "product": {
            "title": title,
            "description": (
                f"{title}\n\n"
                f"ISBN-13: {isbn}\n\n"
                f"Brand new. "
                f"Ships directly from our supplier. "
                f"Typically dispatches within {cfg.DISPATCH_TIME_DAYS} business days."
            ),
            "isbn": [isbn],
        },
        "condition": cfg.CONDITION_ID,
        "conditionDescription": cfg.CONDITION_DESCRIPTION,
        "availability": {
            "shipToLocationAvailability": {
                "quantity": cfg.QUANTITY
            }
        }
    }

    if cover_url:
        payload["product"]["imageUrls"] = [cover_url]

    return payload


def build_offer_payload(row, sku):
    """
    Builds the JSON payload for POST /offer
    This tells eBay the PRICE, CATEGORY, and POLICIES for the listing.
    """
    price = calculate_price(row)

    return {
        "sku":            sku,
        "marketplaceId":  cfg.MARKETPLACE_ID,
        "format":         "FIXED_PRICE",
        "availableQuantity": cfg.QUANTITY,
        "categoryId":     cfg.DEFAULT_CATEGORY_ID,
        "listingDescription": (
            f"{row.get('Title', '').strip()}\n\n"
            f"ISBN-13: {row.get('ISBN-13', '').strip()}\n\n"
            f"Brand new. Ships within {cfg.DISPATCH_TIME_DAYS} business days."
        ),
        "pricingSummary": {
            "price": {
                "currency": cfg.CURRENCY,
                "value":    str(price)
            }
        },
        "listingDuration": cfg.LISTING_DURATION,
        "merchantLocationKey": cfg.MERCHANT_LOCATION_KEY,
        "listingPolicies": {
            "fulfillmentPolicyId": cfg.FULFILLMENT_POLICY_ID,
            "paymentPolicyId":     cfg.PAYMENT_POLICY_ID,
            "returnPolicyId":      cfg.RETURN_POLICY_ID,
        },
    }


# ==============================================================
# SECTION 4: eBay API calls
# ==============================================================

def create_inventory_item(sku, payload):
    """PUT /inventory_item/{sku} — creates or updates a product record."""
    url = f"{BASE_URL}/inventory_item/{sku}"
    headers = get_auth_headers()
    headers["Content-Language"] = "en-US"
    response = requests.put(url, headers=headers, json=payload)
    return response


def create_offer(payload):
    """POST /offer — creates a listing offer (not yet published/live)."""
    url = f"{BASE_URL}/offer"
    headers = get_auth_headers()
    headers["Content-Language"] = "en-US"
    response = requests.post(url, headers=headers, json=payload)
    return response


def bulk_publish_offers(offer_ids):
    """POST /bulk_publish_offer — makes up to 25 offers go live at once."""
    url = f"{BASE_URL}/bulk_publish_offer"
    payload = {
        "requests": [{"offerId": oid} for oid in offer_ids]
    }
    headers = get_auth_headers()
    headers["Content-Language"] = "en-US"
    response = requests.post(url, headers=headers, json=payload)
    return response


# ==============================================================
# SECTION 5: Logging
# ==============================================================

def append_log(log_rows):
    """Appends results to lister_log.csv for your records."""
    file_exists = os.path.exists(cfg.LISTER_LOG)
    with open(cfg.LISTER_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "isbn", "title", "price", "offer_id", "status", "notes"
        ])
        if not file_exists:
            writer.writeheader()
        writer.writerows(log_rows)


# ==============================================================
# SECTION 6: Main listing loop
# ==============================================================

def run_lister():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"📦 eBay Lister — {timestamp}")
    print(f"{'='*60}")

    if cfg.DRY_RUN:
        print("🟡 DRY RUN MODE — no real listings will be created.")
        print("   Set DRY_RUN = False in lister_config.py to go live.\n")

    # Validate required config values before doing anything
    if not cfg.DRY_RUN:
        missing = []
        for field in ["FULFILLMENT_POLICY_ID", "PAYMENT_POLICY_ID", "RETURN_POLICY_ID", "MERCHANT_LOCATION_KEY"]:
            val = getattr(cfg, field, "")
            if not val or val.startswith("YOUR_"):
                missing.append(field)
        if missing:
            print(f"❌ Config incomplete. Fill in these values in lister_config.py first:")
            for m in missing:
                print(f"   - {m}")
            return

    # Load what we've already listed (avoid duplicates)
    state = load_state()
    already_listed = set(state.get("listed_isbns", []))

    # Load and filter scanner CSV
    opportunities = load_opportunities(cfg.SCANNER_CSV)
    if not opportunities:
        print("No opportunities to list. Exiting.")
        return

    # Remove already-listed ISBNs
    new_opps = [r for r in opportunities if r.get("ISBN-13", "") not in already_listed]
    print(f"\n🆕 New (not yet listed): {len(new_opps)} | Already listed: {len(opportunities) - len(new_opps)}")

    if not new_opps:
        print("Nothing new to list. All opportunities already active.")
        return

    # Preview table
    print(f"\n{'─'*80}")
    print(f"{'#':<4} {'Title':<50} {'ISBN':<15} {'Price':>8} {'Profit':>8}")
    print(f"{'─'*80}")
    for i, row in enumerate(new_opps, 1):
        title  = row.get("Title", "")[:48]
        isbn   = row.get("ISBN-13", "")
        price  = calculate_price(row)
        profit = row.get("Profit", "")
        print(f"{i:<4} {title:<50} {isbn:<15} ${price:>7.2f} {profit:>8}")
    print(f"{'─'*80}")

    if cfg.DRY_RUN:
        print(f"\n✅ Dry run complete. {len(new_opps)} listings would be created.")
        print("Set DRY_RUN = False in lister_config.py when ready to go live.")
        return

    # ---- LIVE MODE: Create listings ----
    print(f"\n🚀 Creating {len(new_opps)} eBay listings...\n")

    log_rows      = []
    offer_ids     = []
    success_isbns = []
    pending_rows  = []  # Full row data kept alongside offer_ids for state saving

    for i, row in enumerate(new_opps, 1):
        isbn  = row.get("ISBN-13", "").strip()
        title = row.get("Title", "Unknown")[:55]
        sku   = f"ISBN-{isbn}"
        price = calculate_price(row)

        print(f"[{i}/{len(new_opps)}] {title}...")

        # Step A: Create inventory item
        inv_payload = build_inventory_item_payload(row)
        inv_response = create_inventory_item(sku, inv_payload)

        if inv_response.status_code not in (200, 204):
            print(f"  ❌ Inventory item failed ({inv_response.status_code}): {inv_response.text[:100]}")
            log_rows.append({
                "timestamp": timestamp, "isbn": isbn, "title": title,
                "price": price, "offer_id": "", "status": "FAILED_INVENTORY",
                "notes": inv_response.text[:150]
            })
            continue

        print(f"  ✅ Inventory item created.")

        # Step B: Create offer
        offer_payload = build_offer_payload(row, sku)
        offer_response = create_offer(offer_payload)

        if offer_response.status_code not in (200, 201):
            print(f"  ❌ Offer creation failed ({offer_response.status_code}): {offer_response.text[:100]}")
            log_rows.append({
                "timestamp": timestamp, "isbn": isbn, "title": title,
                "price": price, "offer_id": "", "status": "FAILED_OFFER",
                "notes": offer_response.text[:150]
            })
            continue

        offer_id = offer_response.json().get("offerId")
        offer_ids.append(offer_id)
        success_isbns.append(isbn)
        pending_rows.append(row)
        print(f"  ✅ Offer created. Offer ID: {offer_id}")

        # Small delay to be polite to the API
        time.sleep(0.3)

        # Publish in batches of 25 (eBay limit)
        if len(offer_ids) >= cfg.BATCH_SIZE:
            publish_batch(offer_ids, success_isbns, pending_rows, log_rows, timestamp, state)
            offer_ids     = []
            success_isbns = []
            pending_rows  = []

    # Publish any remaining offers
    if offer_ids:
        publish_batch(offer_ids, success_isbns, pending_rows, log_rows, timestamp, state)

    # Update state — published_isbns already added inside publish_batch
    state["last_run"] = timestamp
    save_state(state)

    # Save log
    append_log(log_rows)

    # Summary
    published = sum(1 for r in log_rows if r["status"] == "PUBLISHED")
    failed    = len(log_rows) - published
    print(f"\n{'='*60}")
    print(f"✅ Done! {published} listed | {failed} failed")
    print(f"📄 Log saved to: {cfg.LISTER_LOG}")
    print(f"{'='*60}\n")


def publish_batch(offer_ids, isbns, rows, log_rows, timestamp, state):
    """
    Publishes a batch of up to 25 offers and logs results.
    Also saves offer_id + listing_id into state['listings'] per ISBN
    so that auto_delist.py can find and end listings later.
    """
    print(f"\n  📤 Publishing batch of {len(offer_ids)} offers...")
    pub_response = bulk_publish_offers(offer_ids)

    if pub_response.status_code not in (200, 207):
        print(f"  ❌ Bulk publish failed ({pub_response.status_code}): {pub_response.text[:100]}")
        for oid, isbn in zip(offer_ids, isbns):
            log_rows.append({
                "timestamp": timestamp, "isbn": isbn, "title": "",
                "price": "", "offer_id": oid, "status": "FAILED_PUBLISH",
                "notes": pub_response.text[:100]
            })
        return

    results = pub_response.json().get("responses", [])
    for result, isbn, row in zip(results, isbns, rows):
        status_code = result.get("statusCode", 0)
        listing_id  = result.get("listingId", "")
        offer_id    = result.get("offerId", "")
        errors      = result.get("errors", [])
        error_msg   = errors[0].get("message", "") if errors else ""

        if status_code == 200:
            print(f"  🟢 LIVE — Listing ID: {listing_id} | ISBN: {isbn}")
            log_rows.append({
                "timestamp": timestamp, "isbn": isbn,
                "title": row.get("Title", "")[:55],
                "price": calculate_price(row), "offer_id": offer_id,
                "status": "PUBLISHED", "notes": f"listingId={listing_id}"
            })
            # Save rich state so auto_delist can find this listing later
            state["listed_isbns"] = list(set(state.get("listed_isbns", [])) | {isbn})
            state["listings"][isbn] = {
                "offer_id":   offer_id,
                "listing_id": listing_id,
                "title":      row.get("Title", "")[:80],
                "price":      calculate_price(row),
                "listed_at":  timestamp,
                "status":     "ACTIVE"
            }
        else:
            print(f"  🔴 Failed — {error_msg} | ISBN: {isbn}")
            log_rows.append({
                "timestamp": timestamp, "isbn": isbn,
                "title": row.get("Title", "")[:55],
                "price": "", "offer_id": offer_id,
                "status": "FAILED_PUBLISH", "notes": error_msg
            })


# ==============================================================
# Entry point
# ==============================================================

if __name__ == "__main__":
    run_lister()
