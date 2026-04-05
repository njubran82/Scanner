# ============================================================
# lister_auth.py
# Handles eBay OAuth tokens for the lister.
# Uses the refresh token saved by ebay_auth_setup.py
# to automatically get fresh access tokens as needed.
# ============================================================

import os
import time
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"

# Scopes needed for listing (must match what you authorized in setup)
SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
])

# In-memory token cache — avoids hitting the API on every request
_cached_token = None
_token_expiry = 0


def get_access_token():
    """
    Returns a valid eBay access token.
    Automatically refreshes when expired.
    Access tokens last ~2 hours; refresh token lasts ~18 months.
    """
    global _cached_token, _token_expiry

    # If we have a cached token with >5 minutes left, reuse it
    if _cached_token and time.time() < (_token_expiry - 300):
        return _cached_token

    client_id     = os.getenv("EBAY_CLIENT_ID")
    client_secret = os.getenv("EBAY_CLIENT_SECRET")
    refresh_token = os.getenv("EBAY_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise EnvironmentError(
            "Missing eBay credentials in .env\n"
            "Need: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN\n"
            "Run ebay_auth_setup.py first if you haven't yet."
        )

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {credentials}",
    }
    data = {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "scope":         SCOPES,
    }

    response = requests.post(TOKEN_URL, headers=headers, data=data)

    if response.status_code != 200:
        raise Exception(
            f"Token refresh failed ({response.status_code}): {response.text}\n"
            "Try running ebay_auth_setup.py again to re-authorize."
        )

    token_data = response.json()
    _cached_token = token_data["access_token"]
    # expires_in is in seconds — store the absolute expiry time
    _token_expiry = time.time() + token_data.get("expires_in", 7200)

    print("🔑 eBay access token refreshed.")
    return _cached_token


def get_auth_headers():
    """Returns the Authorization + Content-Type headers for API calls."""
    return {
        "Authorization":  f"Bearer {get_access_token()}",
        "Content-Type":   "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
