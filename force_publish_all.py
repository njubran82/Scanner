import os, base64, requests, csv, time, json
from pathlib import Path

app_id  = os.environ['EBAY_APP_ID']
cert_id = os.environ['EBAY_CERT_ID']
refresh = os.environ['EBAY_REFRESH_TOKEN']
creds   = base64.b64encode(f'{app_id}:{cert_id}'.encode()).decode()

def get_token():
    r = requests.post('https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'},
        data=f'grant_type=refresh_token&refresh_token={refresh}&scope=https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.account')
    return r.json()['access_token']

token = get_token()
CSV_PATH = Path(r'E:\Book\Lister\booksgoat_enhanced.csv')
rows = list(csv.DictReader(CSV_PATH.open(encoding='utf-8')))
row_index = {r['isbn13']: r for r in rows}
pending = [r for r in rows if r.get('status') == 'pending' and r.get('isbn13')]
print(f'Processing {len(pending)} pending books...')

published = 0
failed = 0

for i, row in enumerate(pending):
    isbn  = row['isbn13']
    price = row.get('sell_price') or str(round(float(row.get('cost', 50)) * 1.3, 2))

    if (i+1) % 50 == 0:
        print(f'  {i+1}/{len(pending)} — refreshing token...')
        token = get_token()

    hdrs = {'Authorization': f'Bearer {token}'}
    hdrs_j = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Content-Language': 'en-US'}

    # Step 1: Force delete any existing offer
    r0 = requests.get('https://api.ebay.com/sell/inventory/v1/offer', headers=hdrs, params={'sku': isbn})
    for o in r0.json().get('offers', []):
        requests.delete(f'https://api.ebay.com/sell/inventory/v1/offer/{o["offerId"]}', headers=hdrs)
        time.sleep(0.1)

    # Step 2: POST fresh offer WITH merchantLocationKey
    r2 = requests.post('https://api.ebay.com/sell/inventory/v1/offer', headers=hdrs_j,
        json={'sku': isbn, 'marketplaceId': 'EBAY_US', 'format': 'FIXED_PRICE',
              'availableQuantity': 10, 'categoryId': '261186',
              'merchantLocationKey': 'home1',
              'listingPolicies': {'fulfillmentPolicyId': '391308514023',
                                  'paymentPolicyId': '391308491023',
                                  'returnPolicyId': '391308498023'},
              'pricingSummary': {'price': {'value': str(price), 'currency': 'USD'}}})

    if r2.status_code not in (200, 201):
        print(f'  [{i+1}] POST failed {isbn}: {r2.text[:80]}')
        failed += 1
        continue

    offer_id = r2.json().get('offerId')

    # Step 3: Verify merchantLocationKey stuck
    r_check = requests.get(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}', headers=hdrs)
    loc_key = r_check.json().get('merchantLocationKey', 'MISSING')

    # Step 4: Publish
    r3 = requests.post(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})

    if r3.status_code == 200:
        listing_id = r3.json().get('listingId')
        print(f'  [{i+1}] OK {isbn} listing={listing_id} loc={loc_key}')
        row_index[isbn]['status']     = 'active'
        row_index[isbn]['offer_id']   = offer_id
        row_index[isbn]['sell_price'] = str(price)
        published += 1
    else:
        err = r3.json().get('errors', [{}])[0].get('message', '')[:80]
        print(f'  [{i+1}] PUBLISH FAILED {isbn} loc={loc_key}: {err}')
        row_index[isbn]['offer_id'] = offer_id
        failed += 1

    time.sleep(0.3)

# Save CSV
all_fields = list(dict.fromkeys(k for r in rows for k in r))
tmp = CSV_PATH.with_suffix('.tmp')
with tmp.open('w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=all_fields, extrasaction='ignore')
    w.writeheader()
    w.writerows(list(row_index.values()))
tmp.replace(CSV_PATH)
print(f'Done: {published} published | {failed} failed')
