"""
config.py — Central configuration for the BooksGoat → eBay scanner.

All thresholds, credentials, and tunable settings live here.
Edit this file to change behavior without touching any logic.

BUSINESS MODEL NOTES:
    - This is a dropshipping system. No inventory is held.
    - Supplier cost = per-order fulfillment cost (paid after a sale).
    - Supplier ships directly; no inbound or outbound shipping cost to you.
    - The supplier sheet is treated as a structured data feed (proto-API).
    - It refreshes weekly (Sundays); the system fetches it live on every run.
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ─── Supplier ─────────────────────────────────────────────────────────────────

# Active supplier: "url_csv" | "csv" | "booksgoat_api" | "booksgoat_scraper"
# "url_csv" = fetch live from Google Sheets URL (PRIMARY)
# "csv"     = read from a local file (FALLBACK)
SUPPLIER = "url_csv"

# Live CSV URL — fetched on every run
SUPPLIER_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1uXD9-87xzSsU4wV0qw34r3OxmoZ5Gz4hw4vhrFykPfU/export?format=csv"
)

# Local CSV fallback path — used if URL fetch fails and FALLBACK_TO_LOCAL = True
CSV_FALLBACK_PATH = "Bulk_File_-_BooksGoat_-_Sheet1.csv"
FALLBACK_TO_LOCAL = True   # Set False to hard-fail if URL is unreachable

# Retry settings for live URL fetch
URL_FETCH_RETRIES     = 3       # Number of attempts before giving up
URL_FETCH_RETRY_DELAY = 5       # Seconds to wait between retries (doubles each attempt)

# Which BooksGoat pricing tier to use as your per-order fulfillment cost
# Options: "5 Qty" | "10 Qty" | "25 Qty"
# In a dropshipping model, this is the price you pay per sale — not bulk inventory.
COST_TIER = "5 Qty"


# ─── eBay API (Browse API) ────────────────────────────────────────────────────
#
# The Browse API uses OAuth 2.0 Client Credentials. It requires TWO credentials:
#   EBAY_APP_ID  = Client ID     (from developer.ebay.com → Application Keys → Production)
#   EBAY_CERT_ID = Client Secret (same page, "Cert ID" column — NEW requirement)
#
# The old Finding API only needed EBAY_APP_ID. The Browse API needs both.
# Add EBAY_CERT_ID to your .env file and GitHub Secrets.
#
# IMPORTANT: Use PRODUCTION keys (labeled "PRD"), not Sandbox ("SBX").
#
# DATA LIMITATION: Browse API provides ACTIVE listings only (not sold).
# Revenue estimates use active median × (1 - EBAY_ACTIVE_PRICE_DISCOUNT).
# Apply for Marketplace Insights API at developer.ebay.com for sold data.

EBAY_APP_ID   = os.getenv("EBAY_APP_ID",  "")   # Client ID
EBAY_CERT_ID  = os.getenv("EBAY_CERT_ID", "")   # Client Secret — REQUIRED for Browse API

EBAY_MAX_RESULTS     = 50      # Listings to fetch per book (Browse API max per page)
EBAY_REQUEST_DELAY   = 0.4     # Seconds between API calls (avoid 429 rate limits)

# Discount applied to active listing median to estimate expected sell price.
# Active prices are typically 10–20% above what books actually sell for.
# Default 10% is conservative — adjust based on your sell-through data.
EBAY_ACTIVE_PRICE_DISCOUNT = 0.10


# ─── Fee & Cost Model ─────────────────────────────────────────────────────────

# eBay Final Value Fee — Books category (13.25% of total sale amount)
EBAY_FEE_RATE = 0.1325

# Shipping costs — BOTH set to $0.00 for this dropshipping model:
#   - Supplier ships to your customer directly, no inbound cost to you
#   - You offer free shipping on eBay listings (built into item price)
# Change only if your arrangement with the supplier changes.
SHIPPING_COST_INBOUND  = 0.00   # What supplier charges you to ship (currently $0)
SHIPPING_COST_OUTBOUND = 0.00   # What you charge your customer (free shipping)

# For profit calculations, total shipping impact = 0.00 by default
SHIPPING_COST = SHIPPING_COST_INBOUND + SHIPPING_COST_OUTBOUND

# eBay per-order fee (currently $0 for standard sellers)
EBAY_PER_ORDER_FEE = 0.00


# ─── Profit & Opportunity Thresholds ──────────────────────────────────────────

# Minimum net profit per book to qualify as an opportunity
MIN_PROFIT = 10.00

# Minimum margin as a decimal (0.10 = 10%)
# Kept low because this is a volume-focused dropshipping business.
# The $10 hard floor is the primary gate; margin is a secondary sanity check.
MIN_MARGIN = 0.10

# Amazon price fallback: if no eBay sold data exists, use Amazon price × discount
# as a conservative eBay revenue estimate. Flagged clearly in all outputs.
USE_AMAZON_FALLBACK      = True
AMAZON_TO_EBAY_DISCOUNT  = 0.85   # eBay typically sells ~15% below Amazon list


# ─── Velocity Filter (OPTIONAL — disabled by default) ─────────────────────────

# Velocity = minimum number of eBay sold comps required before flagging a book.
# Disabled by default: the $10 profit floor is sufficient for volume sourcing.
# Enable this to require market proof-of-demand before alerting.
VELOCITY_FILTER_ENABLED  = False
VELOCITY_MIN_SOLD        = 3        # Minimum sold comps required (if enabled)


# ─── State Tracking & Noise Control ──────────────────────────────────────────

# State tracking prevents alert spam by only notifying on new or changed opps.
# The state database is a simple JSON file stored locally between runs.
STATE_TRACKING_ENABLED  = True
STATE_FILE_PATH         = "scanner_state.json"

# ── Significant change thresholds ─────────────────────────────────────────────
# An existing opportunity triggers a NEW alert only if it improves meaningfully.
# Both checks are evaluated — either one passing is enough to re-alert.
#
#   Profit increase:  (new_profit - alerted_profit) / alerted_profit >= threshold
#   ROI increase:     ROI = profit / supplier_cost
#                     (new_roi - alerted_roi) / alerted_roi >= threshold
#
# Degradation (profit or ROI going DOWN) is tracked silently — no alert.
# Set either value to 999 to effectively disable that check.
SIGNIFICANT_PROFIT_INCREASE_PCT = 0.20   # 20% profit improvement triggers re-alert
SIGNIFICANT_ROI_INCREASE_PCT    = 0.15   # 15% ROI improvement triggers re-alert

# ── Daily summary ──────────────────────────────────────────────────────────────
# One email per day containing all current opportunities — regardless of
# whether any new/changed alerts fired. Sent on the first scan run at or
# after DAILY_SUMMARY_HOUR (24-hour UTC).
#
# Set DAILY_SUMMARY_ENABLED = False to disable the daily digest entirely.
DAILY_SUMMARY_ENABLED = True
DAILY_SUMMARY_HOUR    = 18   # 6 PM UTC — adjust to your preferred time


# ─── Alerts ──────────────────────────────────────────────────────────────────

# Email (Gmail SMTP or any SMTP provider)
EMAIL_ENABLED  = True
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "")
SMTP_PASSWORD  = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM     = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO       = os.getenv("EMAIL_TO", "")

# SMS (Twilio)
SMS_ENABLED          = True
TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER   = os.getenv("TWILIO_FROM_NUMBER", "")
TWILIO_TO_NUMBER     = os.getenv("TWILIO_TO_NUMBER", "")
SMS_MIN_OPPORTUNITIES = 1   # Don't SMS unless at least this many new opps found


# ─── Scheduling ───────────────────────────────────────────────────────────────

SCHEDULER_ENABLED = False
SCHEDULER_CRON    = "0 */6 * * *"   # Every 6 hours by default
