#!/usr/bin/env python3
"""Test eBay token exchange and show which scopes are available."""
import os, base64, requests

app_id = os.environ['EBAY_APP_ID']
cert_id = os.environ['EBAY_CERT_ID']
refresh = os.environ['EBAY_REFRESH_TOKEN']

creds = base64.b64encode(f'{app_id}:{cert_id}'.encode()).decode()

print(f'Token length: {len(refresh)}')
print(f'Token start: {refresh[:20]}')

# Test with fulfillment scope
r = requests.post(
    'https://api.ebay.com/identity/v1/oauth2/token',
    headers={'Authorization': f'Basic {creds}',
             'Content-Type': 'application/x-www-form-urlencoded'},
    data={
        'grant_type': 'refresh_token',
        'refresh_token': refresh,
        'scope': 'https://api.ebay.com/oauth/api_scope '
                 'https://api.ebay.com/oauth/api_scope/sell.inventory '
                 'https://api.ebay.com/oauth/api_scope/sell.fulfillment'
    }
)
print(f'Status: {r.status_code}')
data = r.json()
if 'access_token' in data:
    print('✅ Token exchange succeeded with sell.fulfillment scope')
else:
    print(f'❌ Failed: {data}')
    # Try without fulfillment
    r2 = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}',
                 'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh,
            'scope': 'https://api.ebay.com/oauth/api_scope '
                     'https://api.ebay.com/oauth/api_scope/sell.inventory'
        }
    )
    print(f'Without fulfillment scope: {r2.status_code}')
    if 'access_token' in r2.json():
        print('✅ Token works WITHOUT sell.fulfillment — scope not granted')
    else:
        print(f'❌ Also failed without fulfillment: {r2.json()}')
