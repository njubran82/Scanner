import os, base64, requests, json
app_id  = os.environ['EBAY_APP_ID']
cert_id = os.environ['EBAY_CERT_ID']
refresh = os.environ['EBAY_REFRESH_TOKEN']
creds   = base64.b64encode(f'{app_id}:{cert_id}'.encode()).decode()
r = requests.post('https://api.ebay.com/identity/v1/oauth2/token',
    headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'},
    data=f'grant_type=refresh_token&refresh_token={refresh}&scope=https://api.ebay.com/oauth/api_scope/sell.fulfillment https://api.ebay.com/oauth/api_scope/sell.inventory https://api.ebay.com/oauth/api_scope/sell.account')
print(f'Status: {r.status_code}')
data = r.json()
if 'access_token' in data:
    print('Token acquired — fulfillment scope OK')
else:
    print('ERROR:', json.dumps(data, indent=2))
