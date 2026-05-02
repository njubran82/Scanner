#!/usr/bin/env python3
"""
scan_duplicates.py — finds ISBNs with 2+ active eBay listings
Run from E:\Book\Scanner or E:\Book\Lister
Prints duplicate groups with listing IDs, titles, prices
"""

import os, base64, requests, csv
from pathlib import Path

EBAY_APP_ID = os.environ["EBAY_APP_ID"]
EBAY_CERT_ID = os.environ["EBAY_CERT_ID"]
EBAY_REFRESH_TOKEN = os.environ["EBAY_REFRESH_TOKEN"]

def get_user_token():
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=(f"grant_type=refresh_token&refresh_token={EBAY_REFRESH_TOKEN}"
              "&scope=https://api.ebay.com/oauth/api_scope/sell.inventory"),
        timeout=15,
    )
    return r.json()["access_token"]

def get_app_token():
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        timeout=15,
    )
    return r.json()["access_token"]

def fetch_all_listings(app_token):
    """Fetch all active listings for atlas_commerce via Browse API."""
    headers = {"Authorization": f"Bearer {app_token}",
               "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    listings = []
    offset = 0
    while True:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers,
            params={"q": "book", "filter": "sellers:{atlas_commerce}",
                    "limit": 200, "offset": offset},
            timeout=20,
        )
        data = r.json()
        batch = data.get("itemSummaries", [])
        listings.extend(batch)
        total = data.get("total", 0)
        print(f"  Fetched {len(listings)}/{total}...")
        if len(listings) >= total or not batch:
            break
        offset += len(batch)
    return listings

def fetch_all_offers(user_token):
    """Fetch all offers via Inventory API — most reliable for SKU/ISBN."""
    headers = {"Authorization": f"Bearer {user_token}"}
    offers = []
    offset = 0
    while True:
        r = requests.get(
            "https://api.ebay.com/sell/inventory/v1/offer",
            headers=headers,
            params={"limit": 100, "offset": offset},
            timeout=20,
        )
        if r.status_code != 200:
            print(f"  Offers API error: {r.text[:200]}")
            break
        data = r.json()
        batch = data.get("offers", [])
        offers.extend(batch)
        total = data.get("total", 0)
        print(f"  Fetched {len(offers)}/{total} offers...")
        if len(offers) >= total or not batch:
            break
        offset += len(batch)
    return offers

def run():
    print("Acquiring tokens...")
    user_token = get_user_token()
    app_token  = get_app_token()

    print("Fetching all offers via Inventory API...")
    offers = fetch_all_offers(user_token)
    print(f"Total offers: {len(offers)}")

    # Group by SKU (ISBN)
    by_sku = {}
    for offer in offers:
        sku    = offer.get("sku", "").strip()
        status = offer.get("status", "")
        if not sku or status != "PUBLISHED":
            continue
        by_sku.setdefault(sku, []).append({
            "offer_id":   offer.get("offerId", ""),
            "listing_id": offer.get("listing", {}).get("listingId", ""),
            "price":      offer.get("pricingSummary", {}).get("price", {}).get("value", "?"),
            "sku":        sku,
        })

    duplicates = {sku: entries for sku, entries in by_sku.items() if len(entries) > 1}

    if not duplicates:
        print("\nNo duplicates found via Inventory API.")
        print("Checking Browse API for manual listings without inventory items...")

        print("Fetching all Browse API listings...")
        listings = fetch_all_listings(app_token)

        # Group by ISBN extracted from title/sku
        import re
        by_isbn = {}
        for item in listings:
            item_id = item.get("itemId", "")
            title   = item.get("title", "")
            price   = item.get("price", {}).get("value", "?")
            # Try to find ISBN in title
            isbn_match = re.search(r'\b(97[89]\d{10})\b', title)
            isbn = isbn_match.group(1) if isbn_match else None
            by_isbn.setdefault(isbn or f"no_isbn_{item_id}", []).append({
                "item_id": item_id,
                "title":   title[:70],
                "price":   price,
            })

        duplicates_browse = {k: v for k, v in by_isbn.items()
                             if len(v) > 1 and not k.startswith("no_isbn")}
        if duplicates_browse:
            print(f"\nFound {len(duplicates_browse)} ISBNs with duplicate listings (Browse API):\n")
            for isbn, items in sorted(duplicates_browse.items()):
                print(f"ISBN: {isbn} — {len(items)} listings")
                for item in items:
                    print(f"  {item['item_id']} | ${item['price']} | {item['title']}")
                print()
        else:
            print("No duplicates found.")
        return

    print(f"\nFound {len(duplicates)} ISBNs with duplicate offers:\n")
    for sku, entries in sorted(duplicates.items()):
        print(f"ISBN: {sku} — {len(entries)} offers")
        for e in entries:
            print(f"  offer_id={e['offer_id']} | listing={e['listing_id']} | ${e['price']}")
        print()

    # Save to CSV
    out = []
    for sku, entries in duplicates.items():
        for e in entries:
            out.append({"isbn13": sku, **e})
    if out:
        with open("duplicate_listings.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=out[0].keys())
            w.writeheader()
            w.writerows(out)
        print(f"Saved to duplicate_listings.csv")

if __name__ == "__main__":
    run()
