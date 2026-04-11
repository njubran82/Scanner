"""
create_location.py — One-time setup script
Creates the DEFAULT merchant location in your eBay account.
Run this ONCE from D:\Book\Lister before using the lister.

Usage:
    python create_location.py
"""

import requests, base64, os
from dotenv import load_dotenv

load_dotenv()

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')

def get_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={
            'Authorization': f'Basic {creds}',
            'Content-Type': 'application/x-www-form-urlencoded'
        },
        data={
            'grant_type': 'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': (
                'https://api.ebay.com/oauth/api_scope '
                'https://api.ebay.com/oauth/api_scope/sell.inventory '
                'https://api.ebay.com/oauth/api_scope/sell.account'
            )
        }
    )
    return r.json()['access_token']

def create_location(token):
    payload = {
        "location": {
            "address": {
                "country": "US"
            }
        },
        "locationEnabled": True,
        "locationTypes": ["WAREHOUSE"],
        "name": "Default Warehouse",
        "merchantLocationStatus": "ENABLED"
    }

    r = requests.post(
        'https://api.ebay.com/sell/inventory/v1/location/DEFAULT',
        headers={
            'Authorization':    f'Bearer {token}',
            'Content-Type':     'application/json',
            'Content-Language': 'en-US'
        },
        json=payload
    )

    if r.status_code in [200, 201, 204]:
        print("✅ Merchant location 'DEFAULT' created successfully.")
    elif r.status_code == 409 or 'already exists' in r.text.lower():
        print("✅ Merchant location 'DEFAULT' already exists — you're good.")
    else:
        print(f"❌ Failed ({r.status_code}): {r.text}")

if __name__ == '__main__':
    print("Creating eBay merchant location...")
    token = get_token()
    create_location(token)
