# ============================================================
# lister_config.py
# Configuration for the eBay auto-lister
# Edit the values in this file to match your eBay account
# ============================================================

# ----------------------------------------------------------
# FILTERING — which scanner results to list
# ----------------------------------------------------------

# Only list books with these confidence levels
# Options: "LOW", "MEDIUM", "HIGH"
# FALLBACK is excluded by default — no real eBay data
ALLOWED_CONFIDENCE = ["LOW", "MEDIUM", "HIGH"]

# Minimum profit required before listing ($)
MIN_PROFIT = 15.00

# Minimum margin required before listing (%)
MIN_MARGIN = 20.0

# Skip listings with these concern flags
# WIDE_SPREAD means the eBay price range is unreliable
SKIP_IF_CONCERNS = ["FALLBACK_PRICING", "NO_EBAY_DATA", "WIDE_SPREAD"]

# ----------------------------------------------------------
# LISTING SETTINGS
# ----------------------------------------------------------

# How long listings stay active (days)
# 30 is the max for fixed-price
LISTING_DURATION = "GTC"  # GTC = Good Till Cancelled (recommended)

# Quantity per listing — ALWAYS 1 for dropshipping
QUANTITY = 100

# Handling time in days — give yourself dropship buffer
DISPATCH_TIME_DAYS = 20

# Condition for all books
# Inventory API values: NEW, LIKE_NEW, VERY_GOOD, GOOD, ACCEPTABLE
# (Note: these are different from the old Trading API numeric codes)
CONDITION_ID = "NEW"
CONDITION_DESCRIPTION = "Brand new. Never opened. Ships direct from supplier."

# eBay book category IDs
# 171228 = Textbooks, Education  |  267 = Books (general)
DEFAULT_CATEGORY_ID = "171228"

# ----------------------------------------------------------
# PRICING STRATEGY
# ----------------------------------------------------------

# How to price against the eBay active listing price
# "MATCH"    = use exactly the revenue price from scanner
# "UNDERCUT" = list slightly below (see UNDERCUT_AMOUNT)
PRICING_MODE = "UNDERCUT"

# How much to undercut the active listing price by ($)
# Helps your listing sell faster
UNDERCUT_AMOUNT = 1.00

# ----------------------------------------------------------
# YOUR eBAY ACCOUNT POLICIES
# How to find these:
#   1. Go to eBay Seller Hub
#   2. Click Account → Business Policies
#   3. Copy the Policy ID numbers from each policy
# ----------------------------------------------------------

FULFILLMENT_POLICY_ID = "391308514023"
PAYMENT_POLICY_ID     = "391308491023"
RETURN_POLICY_ID      = "391308498023"
MERCHANT_LOCATION_KEY = "home1"

# ----------------------------------------------------------
# FILE PATHS
# ----------------------------------------------------------

# Scanner output CSV to read from
SCANNER_CSV = "scanner_results.csv"

# Log file to record what was listed
LISTER_LOG = "lister_log.csv"

# State file to avoid re-listing duplicates
LISTER_STATE = "lister_state.json"

# ----------------------------------------------------------
# eBay API SETTINGS
# ----------------------------------------------------------

# Marketplace ID — US eBay
MARKETPLACE_ID = "EBAY_US"

# Currency
CURRENCY = "USD"

# Max offers to publish in one batch (eBay limit = 25)
BATCH_SIZE = 25

# Dry run mode — if True, prints what WOULD be listed but doesn't actually list
# Set to False when you're ready to go live
DRY_RUN = False
