#!/usr/bin/env python3
"""
full_publish.py — Creates inventory item + offer + publishes for all pending books.
Handles both 25702 (no inventory item) and existing offer cases correctly.
Includes duplicate guard: checks eBay for existing listings before creating new ones.
"""
import os, base64, requests, csv, time, json
from pathlib import Path
from datetime import datetime

app_id  = os.environ['EBAY_APP_ID']
cert_id = os.environ['EBAY_CERT_ID']
refresh = os.environ['EBAY_REFRESH_TOKEN']
creds   = base64.b64encode(f'{app_id}:{cert_id}'.encode()).decode()

MIN_QTY_BLOCKLIST = {
    '9781260460445',  # Lange Q&A Radiography Examination — min qty 5
    '9780990873853',  # Overcoming Gravity: Gymnastics — min qty 5
    '9781119826798',  # Architect's Studio Companion — PDF only on BooksGoat
    '9780357622957',  # Theory and Practice of Group Counseling — min qty 5
    '9781466516946',  # American Herbal Botanical Safety Handbook — counterfeit flag
}

POLICIES = {
    'fulfillmentPolicyId': '391308514023',
    'paymentPolicyId':     '391308491023',
    'returnPolicyId':      '391308498023',
}

def get_token():
    r = requests.post('https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'},
        data=f'grant_type=refresh_token&refresh_token={refresh}&scope=https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.account https://api.ebay.com/oauth/api_scope/buy.browse')
    return r.json()['access_token']

def hdrs(token):
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Content-Language': 'en-US'}

# ──────────────────────────────────────────────────────────
# DUPLICATE GUARD — prevents creating listings for books
# that already exist on eBay under atlas_commerce
# ──────────────────────────────────────────────────────────
def check_existing_listing(isbn, token, seller='atlas_commerce'):
    """
    Query eBay Browse API for active listings matching this ISBN
    under our seller account. Returns match info dict or None.
    """
    try:
        r = requests.get(
            'https://api.ebay.com/buy/browse/v1/item_summary/search',
            headers={
                'Authorization': f'Bearer {token}',
                'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
            },
            params={
                'q': isbn,
                'filter': f'sellers:{{{seller}}}',
                'limit': 5,
            },
            timeout=15)
        r.raise_for_status()
        items = r.json().get('itemSummaries', [])
        if items:
            item = items[0]
            return {
                'item_id': item.get('itemId', ''),
                'title':   item.get('title', ''),
                'price':   float(item.get('price', {}).get('value', 0)),
            }
        return None
    except Exception as e:
        print(f'  WARNING: duplicate check failed for {isbn}: {e}')
        return None  # fail open — allow listing if check errors

# ──────────────────────────────────────────────────────────

def ensure_inventory_item(isbn, title, fmt, price, token):
    """Create or update inventory item."""
    condition = "NEW"
    payload = {
        "sku": isbn,
        "product": {
            "title": title[:80],
            "isbn": [isbn],
            "aspects": {"Format": [fmt or "Paperback"]},
        },
        "condition": condition,
        "availability": {"shipToLocationAvailability": {"quantity": 20}},
    }
    r = requests.put(
        f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn}',
        headers=hdrs(token), json=payload)
    return r.status_code in (200, 204)

def delete_existing_offers(isbn, token):
    r = requests.get('https://api.ebay.com/sell/inventory/v1/offer',
        headers={'Authorization': f'Bearer {token}'}, params={'sku': isbn})
    for o in r.json().get('offers', []):
        requests.delete(f'https://api.ebay.com/sell/inventory/v1/offer/{o["offerId"]}',
            headers={'Authorization': f'Bearer {token}'})
        time.sleep(0.1)

def create_offer(isbn, price, token):
    payload = {
        'sku': isbn,
        'marketplaceId': 'EBAY_US',
        'format': 'FIXED_PRICE',
        'availableQuantity': 20,
        'categoryId': '261186',
        'merchantLocationKey': 'home1',
        'listingPolicies': POLICIES,
        'pricingSummary': {'price': {'value': str(round(float(price), 2)), 'currency': 'USD'}},
        'includeCatalogProductDetails': True,
    }
    r = requests.post('https://api.ebay.com/sell/inventory/v1/offer',
        headers=hdrs(token), json=payload)
    if r.status_code in (200, 201):
        return r.json().get('offerId')
    return None

def publish_offer(offer_id, token):
    r = requests.post(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    if r.status_code == 200:
        return r.json().get('listingId'), None
    err = r.json().get('errors', [{}])[0].get('message', '')[:100]
    return None, err

CSV_PATH = Path(r'E:\Book\Lister\booksgoat_enhanced.csv')
rows = list(csv.DictReader(CSV_PATH.open(encoding='utf-8')))
row_index = {r['isbn13']: r for r in rows}
pending = [r for r in rows if r.get('status') == 'pending' and r.get('isbn13')
           and r.get('isbn13') not in MIN_QTY_BLOCKLIST]

print(f'Processing {len(pending)} pending books...')
token = get_token()
published = failed = skipped = 0

for i, row in enumerate(pending):
    isbn  = row['isbn13']
    title = row.get('title', f'Book {isbn}')
    fmt   = row.get('format', 'Paperback')
    price = row.get('sell_price') or str(round(float(row.get('cost', 50)) * 1.3, 2))

    if (i+1) % 50 == 0:
        token = get_token()
        print(f'  {i+1}/{len(pending)} - token refreshed')

    # --- DUPLICATE GUARD ---
    existing = check_existing_listing(isbn, token)
    if existing:
        print(f'  [{i+1}] SKIP {isbn} — already listed: #{existing["item_id"]} @ ${existing["price"]:.2f}')
        skipped += 1
        time.sleep(0.3)
        continue
    # --- END DUPLICATE GUARD ---

    # Step 1: ensure inventory item exists
    ok = ensure_inventory_item(isbn, title, fmt, price, token)
    if not ok:
        print(f'  [{i+1}] INV FAIL {isbn}')
        failed += 1
        time.sleep(0.5)
        continue

    # Step 2: delete existing offers
    delete_existing_offers(isbn, token)
    time.sleep(0.2)

    # Step 3: create fresh offer
    offer_id = create_offer(isbn, price, token)
    if not offer_id:
        print(f'  [{i+1}] OFFER FAIL {isbn}')
        failed += 1
        time.sleep(0.5)
        continue

    # Step 4: publish
    listing_id, err = publish_offer(offer_id, token)
    if listing_id:
        print(f'  [{i+1}] OK {isbn} listing={listing_id}')
        row_index[isbn]['status']     = 'active'
        row_index[isbn]['offer_id']   = offer_id
        row_index[isbn]['sell_price'] = str(price)
        row_index[isbn]['listed_at']  = datetime.now().isoformat()
        published += 1
    else:
        print(f'  [{i+1}] PUBLISH FAIL {isbn}: {err}')
        row_index[isbn]['offer_id'] = offer_id
        failed += 1

    # Save every 10 books
    if (i+1) % 10 == 0:
        all_fields = list(dict.fromkeys(k for r in rows for k in r))
        tmp = CSV_PATH.with_suffix('.tmp')
        with tmp.open('w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=all_fields, extrasaction='ignore')
            w.writeheader()
            w.writerows(list(row_index.values()))
        tmp.replace(CSV_PATH)

    time.sleep(0.5)

# Final save
all_fields = list(dict.fromkeys(k for r in rows for k in r))
tmp = CSV_PATH.with_suffix('.tmp')
with tmp.open('w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=all_fields, extrasaction='ignore')
    w.writeheader()
    w.writerows(list(row_index.values()))
tmp.replace(CSV_PATH)

print(f'\nDone: {published} published | {failed} failed | {skipped} skipped')
