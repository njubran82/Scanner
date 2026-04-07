# ============================================================
# lister_config.py
# Strategy: prioritise sell-through and volume over high margin.
# Any book that is profitable after eBay fees gets listed.
# ============================================================

# Minimum profit in $ after eBay fees
MIN_PROFIT_AFTER_FEES = 1.00
MIN_PROFIT            = 1.00
MIN_MARGIN            = 0.0

# All confidence levels including FALLBACK are allowed
ALLOWED_CONFIDENCE = ["LOW", "MEDIUM", "HIGH", "FALLBACK"]

# Only skip if genuinely no usable price data
SKIP_IF_CONCERNS = ["NO_PRICE_DATA"]

# ------ LISTING SETTINGS ------
LISTING_DURATION      = "GTC"
QUANTITY              = 100
DISPATCH_TIME_DAYS    = 20
CONDITION_ID          = "NEW"
CONDITION_DESCRIPTION = "Brand new. Never opened. Ships direct from supplier."
DEFAULT_CATEGORY_ID   = "171228"

# ------ COMPETITIVE PRICING ------
UNDERCUT_PCT  = 0.1      # Undercut eBay active price by 1%
EBAY_FEE_RATE = 0.1325    # eBay Final Value Fee

# ------ BUSINESS POLICIES ------
FULFILLMENT_POLICY_ID = "391308514023"
PAYMENT_POLICY_ID     = "391308491023"
RETURN_POLICY_ID      = "391308498023"
MERCHANT_LOCATION_KEY = "home1"

# ------ FILE PATHS ------
SCANNER_CSV  = "scanner_results.csv"
LISTER_LOG   = "lister_log.csv"
LISTER_STATE = "lister_state.json"

# ------ eBay API ------
MARKETPLACE_ID = "EBAY_US"
CURRENCY       = "USD"
BATCH_SIZE     = 25

DRY_RUN = False
