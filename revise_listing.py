"""
revise_listing.py — Revise an auto-listed eBay book listing via API
Use this when eBay Seller Hub won't let you edit a listing directly.

Usage:
    python revise_listing.py <isbn13>

Example:
    python revise_listing.py 9781951058098

You can revise: price, description, quantity, title
"""

import sys, json, base64, os, requests
from dotenv import load_dotenv

load_dotenv()

EBAY_CLIENT_ID     = os.getenv('EBAY_CLIENT_ID')
EBAY_CLIENT_SECRET = os.getenv('EBAY_CLIENT_SECRET')
EBAY_REFRESH_TOKEN = os.getenv('EBAY_REFRESH_TOKEN')
STATE_FILE         = 'lister_state.json'


def get_token():
    creds = base64.b64encode(f'{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}'.encode()).decode()
    r = requests.post(
        'https://api.ebay.com/identity/v1/oauth2/token',
        headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'},
        data={
            'grant_type': 'refresh_token',
            'refresh_token': EBAY_REFRESH_TOKEN,
            'scope': (
                'https://api.ebay.com/oauth/api_scope '
                'https://api.ebay.com/oauth/api_scope/sell.inventory'
            )
        }
    )
    return r.json()['access_token']


def revise(isbn13):
    with open(STATE_FILE) as f:
        state = json.load(f)

    listing = state.get('listings', {}).get(isbn13)
    if not listing:
        print(f"ISBN {isbn13} not found in lister_state.json")
        return

    offer_id = listing.get('offer_id')
    if not offer_id:
        print(f"No offer_id for {isbn13} — this is a manual listing, edit directly in Seller Hub")
        return

    print(f"\nCurrent listing: {listing['title'][:70]}")
    print(f"  ISBN:          {isbn13}")
    print(f"  Offer ID:      {offer_id}")
    print(f"  Listing ID:    {listing.get('listing_id')}")
    print(f"  Current price: ${listing.get('listing_price', 'N/A')}")
    print()

    token = get_token()

    # Fetch current offer
    r = requests.get(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers={'Authorization': f'Bearer {token}'}
    )
    if r.status_code != 200:
        print(f"Could not fetch offer: {r.text[:200]}")
        return

    offer = r.json()

    print("What would you like to revise?")
    print("  1. Price")
    print("  2. Description")
    print("  3. Quantity")
    print("  4. Nothing — just show me the current offer data")
    choice = input("\nEnter choice (1-4): ").strip()

    if choice == '1':
        new_price = input(f"New price (current: ${listing.get('listing_price')}): $").strip()
        offer['pricingSummary'] = {'price': {'currency': 'USD', 'value': new_price}}
        for f in ['offerId', 'status', 'listing', 'marketplaceId', 'auditInfo', 'warnings', 'errors']:
            offer.pop(f, None)
        r2 = requests.put(
            f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Content-Language': 'en-US'},
            json=offer
        )
        if r2.status_code in [200, 204]:
            state['listings'][isbn13]['listing_price'] = float(new_price)
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
            print(f"✅ Price updated to ${new_price} and saved to state")
        else:
            print(f"❌ Failed: {r2.text[:300]}")

    elif choice == '2':
        print("Enter new description (press Enter twice when done):")
        lines = []
        while True:
            line = input()
            if line == '' and lines and lines[-1] == '':
                break
            lines.append(line)
        new_desc = '\n'.join(lines[:-1])
        offer['listingDescription'] = new_desc
        for f in ['offerId', 'status', 'listing', 'marketplaceId', 'auditInfo', 'warnings', 'errors']:
            offer.pop(f, None)
        r2 = requests.put(
            f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Content-Language': 'en-US'},
            json=offer
        )
        if r2.status_code in [200, 204]:
            print("✅ Description updated")
        else:
            print(f"❌ Failed: {r2.text[:300]}")

    elif choice == '3':
        new_qty = input(f"New quantity (current: {offer.get('availableQuantity', 100)}): ").strip()
        # Update inventory item quantity
        r2 = requests.get(
            f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn13}',
            headers={'Authorization': f'Bearer {token}'}
        )
        if r2.status_code == 200:
            inv = r2.json()
            inv['availability']['shipToLocationAvailability']['quantity'] = int(new_qty)
            r3 = requests.put(
                f'https://api.ebay.com/sell/inventory/v1/inventory_item/{isbn13}',
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Content-Language': 'en-US'},
                json=inv
            )
            print("✅ Quantity updated" if r3.status_code in [200, 204] else f"❌ Failed: {r3.text[:200]}")
        else:
            print(f"❌ Could not fetch inventory item: {r2.text[:200]}")

    elif choice == '4':
        import pprint
        pprint.pprint(offer)

    else:
        print("No changes made")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python revise_listing.py <isbn13>")
        print("\nActive listings:")
        with open(STATE_FILE) as f:
            state = json.load(f)
        for isbn, l in state.get('listings', {}).items():
            if l.get('offer_id'):
                print(f"  {isbn} — {l['title'][:55]}")
    else:
        revise(sys.argv[1])
