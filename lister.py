# ============================================================
# lister.py
# Lists profitable books on eBay from scanner_results.csv.
# Strategy: list any book profitable after fees, price
# competitively near eBay active lower end to sell faster.
# lister_state.json is the unified registry used by tracker.
# ============================================================

import csv, json, os, sys, time, requests
from protection_patch import is_protected
from datetime import datetime

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import lister_config as cfg
from lister_auth import get_auth_headers

BASE_URL = "https://api.ebay.com/sell/inventory/v1"


# ── Filtering ─────────────────────────────────────────────

def should_skip(row):
    isbn = row.get("ISBN-13", "").strip()
    if not isbn or len(isbn) < 10:
        return "Missing ISBN"
    try:
        cost    = float(str(row.get("Cost",    "0")).replace("$","").replace(",","").strip())
        revenue = float(str(row.get("Revenue", "0")).replace("$","").replace(",","").strip())
    except ValueError:
        return "Cannot parse price"
    if cost <= 0 or revenue <= 0:
        return "Zero price"
    raw_conf   = str(row.get("Confidence","")).strip()
    confidence = raw_conf.split()[-1].upper() if raw_conf else ""
    if confidence not in cfg.ALLOWED_CONFIDENCE:
        return f"Confidence '{confidence}' not allowed"
    for flag in cfg.SKIP_IF_CONCERNS:
        if flag in str(row.get("Concerns","")):
            return f"Blocked flag: {flag}"
    if calculate_competitive_price(row) is None:
        return "Not profitable at any competitive price"
    return None


def load_opportunities(csv_path):
    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}"); return []
    opps, skipped = [], []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            r = should_skip(row)
            (skipped if r else opps).append((row, r) if r else row)
    # Normalise: opps contains dicts, skipped contains tuples
    clean_opps = [x for x in opps if isinstance(x, dict)]
    print(f"CSV: {len(clean_opps)} pass, {len(skipped)} skipped.")
    return clean_opps


# ── State ─────────────────────────────────────────────────

def load_state():
    if os.path.exists(cfg.LISTER_STATE):
        s = json.load(open(cfg.LISTER_STATE))
        s.setdefault("listings", {}); s.setdefault("listed_isbns", [])
        return s
    return {"listed_isbns": [], "listings": {}, "last_run": None}

def save_state(state):
    json.dump(state, open(cfg.LISTER_STATE,"w"), indent=2)


# ── Pricing ───────────────────────────────────────────────

def calculate_competitive_price(row):
    """Undercut eBay active by UNDERCUT_PCT; fall back to full price."""
    try:
        revenue = float(str(row.get("Revenue","0")).replace("$","").replace(",","").strip())
        cost    = float(str(row.get("Cost",   "0")).replace("$","").replace(",","").strip())
    except ValueError:
        return None
    if revenue <= 0 or cost <= 0:
        return None
    for price in [round(revenue*(1-cfg.UNDERCUT_PCT),2), round(revenue,2)]:
        if price*(1-cfg.EBAY_FEE_RATE) - cost >= cfg.MIN_PROFIT_AFTER_FEES:
            return price
    return None


# ── Cover image ───────────────────────────────────────────

def get_cover_image_url(isbn):
    url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
    try:
        r = requests.head(url, timeout=5, allow_redirects=True)
        if r.status_code==200 and int(r.headers.get("Content-Length",0))>1000:
            return url
    except Exception:
        pass
    try:
        r = requests.get(f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}", timeout=5)
        if r.status_code==200:
            items = r.json().get("items",[])
            if items:
                links = items[0].get("volumeInfo",{}).get("imageLinks",{})
                img = links.get("large") or links.get("thumbnail")
                if img:
                    return img.replace("http://","https://").replace("&edge=curl","")
    except Exception:
        pass
    return None


# ── Book metadata (author, format, publisher) ─────────────

def get_book_metadata(isbn):
    """Fetch author, format, publisher from Google Books API. Returns dict with safe fallbacks."""
    meta = {"author": None, "format": "Paperback", "publisher": None}
    try:
        r = requests.get(
            f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}", timeout=5
        )
        if r.status_code == 200:
            items = r.json().get("items", [])
            if items:
                info = items[0].get("volumeInfo", {})
                authors = info.get("authors", [])
                if authors:
                    meta["author"] = authors[0]
                meta["publisher"] = info.get("publisher")
                # Google Books printType: BOOK or MAGAZINE
                page_count = info.get("pageCount", 0)
                # Guess format from page count — most textbooks are paperback or hardcover
                # We default to Paperback as the safest generic value for eBay category 171228
    except Exception:
        pass
    return meta


# ── Required closing statement for all listings ───────────
CLOSING_STATEMENT = (
    "This item is sourced internationally to offer significant savings. "
    "Tracking information may not update until the package reaches the United States. "
    "All books are brand new, in mint condition, and carefully inspected before shipment."
)


# ── AI-generated description ───────────────────────────────
def generate_description(title: str, isbn: str) -> str:
    """
    Calls Claude API to generate a compelling eBay listing description.
    Falls back to a simple description if API call fails.
    """
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         os.getenv("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "messages": [{
                    "role":    "user",
                    "content": (
                        f"Write a concise, compelling eBay listing description for this book:\n"
                        f"Title: {title}\nISBN-13: {isbn}\n\n"
                        f"2-3 sentences max. Highlight what the book covers and who it's for. "
                        f"Do not mention price, shipping, or condition. Plain text only, no formatting."
                    )
                }],
            },
            timeout=15,
        )
        if response.status_code == 200:
            content = response.json().get("content", [])
            if content and content[0].get("type") == "text":
                return content[0]["text"].strip()
    except Exception:
        pass

    # Fallback description
    return f"{title}\n\nISBN-13: {isbn}"


# ── eBay API payloads ─────────────────────────────────────

def build_inventory_item_payload(row, cover_url, full_desc, meta):
    title = row.get("Title","").strip()[:80]
    isbn  = row.get("ISBN-13","").strip()

    aspects = {
        "Publication Name": [title],
        "ISBN":             [isbn],
        "Language":         ["English"],
        "Format":           [meta.get("format") or "Paperback"],
        "Type":             ["Textbook"],
    }
    if meta.get("author"):
        aspects["Author"] = [meta["author"]]
    if meta.get("publisher"):
        aspects["Publisher"] = [meta["publisher"]]

    p = {
        "product": {
            "title":       title,
            "description": full_desc,
            "isbn":        [isbn],
            "aspects":     aspects,
        },
        "condition":            cfg.CONDITION_ID,
        "conditionDescription": cfg.CONDITION_DESCRIPTION,
        "availability": {"shipToLocationAvailability": {"quantity": cfg.QUANTITY}},
    }
    if cover_url:
        p["product"]["imageUrls"] = [cover_url]
    return p


def build_offer_payload(row, sku, price, full_desc):
    return {
        "sku": sku, "marketplaceId": cfg.MARKETPLACE_ID,
        "format": "FIXED_PRICE", "availableQuantity": cfg.QUANTITY,
        "categoryId": cfg.DEFAULT_CATEGORY_ID,
        "listingDescription": full_desc,
        "pricingSummary": {"price": {"currency": cfg.CURRENCY, "value": str(price)}},
        "listingDuration": cfg.LISTING_DURATION,
        "merchantLocationKey": cfg.MERCHANT_LOCATION_KEY,
        "listingPolicies": {
            "fulfillmentPolicyId": cfg.FULFILLMENT_POLICY_ID,
            "paymentPolicyId":     cfg.PAYMENT_POLICY_ID,
            "returnPolicyId":      cfg.RETURN_POLICY_ID,
        },
    }


# ── eBay API calls ────────────────────────────────────────

def _headers():
    h = get_auth_headers(); h["Content-Language"] = "en-US"; return h

def create_inventory_item(sku, payload):
    return requests.put(f"{BASE_URL}/inventory_item/{sku}", headers=_headers(), json=payload)

def create_offer(payload):
    return requests.post(f"{BASE_URL}/offer", headers=_headers(), json=payload)

def publish_offer_single(offer_id):
    return requests.post(f"{BASE_URL}/offer/{offer_id}/publish", headers=_headers())


# ── Logging ───────────────────────────────────────────────

def append_log(rows):
    fields = ["timestamp","isbn","title","price","offer_id","listing_id","status","notes"]
    exists = os.path.exists(cfg.LISTER_LOG)
    with open(cfg.LISTER_LOG,"a",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists: w.writeheader()
        w.writerows(rows)


# ── Main loop ─────────────────────────────────────────────

def run_lister():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}\neBay Lister — {ts}\n{'='*60}")
    if cfg.DRY_RUN:
        print("DRY RUN — no real listings will be created.\n")

    if not cfg.DRY_RUN:
        missing = [f for f in ["FULFILLMENT_POLICY_ID","PAYMENT_POLICY_ID",
                                "RETURN_POLICY_ID","MERCHANT_LOCATION_KEY"]
                   if str(getattr(cfg,f,"")).startswith("YOUR_")]
        if missing:
            print(f"Config incomplete: {missing}"); return

    state         = load_state()
    already_listed = set(state.get("listed_isbns",[]))
    opps          = load_opportunities(cfg.SCANNER_CSV)
    if not opps: return

    # Never skip protected books — always attempt relist even if in already_listed
    def _enhanced_row(isbn):
        """Look up the booksgoat_enhanced.csv row for this ISBN to check protected flag."""
        import csv as _csv
        enhanced_path = getattr(cfg, 'ENHANCED_CSV', r'E:\Book\Lister\booksgoat_enhanced.csv')
        try:
            with open(enhanced_path, newline='', encoding='utf-8') as _f:
                for _row in _csv.DictReader(_f):
                    if _row.get('isbn13','').strip() == isbn:
                        return _row
        except Exception:
            pass
        return {}

    new_opps = [
        r for r in opps
        if r.get("ISBN-13","") not in already_listed
        or is_protected(_enhanced_row(r.get("ISBN-13","")))
    ]
    print(f"New to list: {len(new_opps)} | Already listed: {len(opps)-len(new_opps)}")
    if not new_opps: print("Nothing new."); return

    # Preview table
    print(f"\n{'─'*85}")
    print(f"{'#':<4} {'Title':<46} {'ISBN':<14} {'Price':>8} {'Profit':>8} {'Conf'}")
    print(f"{'─'*85}")
    for i, row in enumerate(new_opps, 1):
        price  = calculate_competitive_price(row) or 0
        try:
            cost = float(str(row.get("Cost","0")).replace("$","").replace(",",""))
        except: cost = 0
        profit = round(price*(1-cfg.EBAY_FEE_RATE)-cost, 2)
        conf   = str(row.get("Confidence","")).split()[-1] if row.get("Confidence") else "?"
        print(f"{i:<4} {row.get('Title','')[:44]:<46} {row.get('ISBN-13',''):<14} "
              f"${price:>7.2f} ${profit:>7.2f} {conf}")
    print(f"{'─'*85}")

    if cfg.DRY_RUN:
        print(f"\nDry run done. {len(new_opps)} would be listed."); return

    print(f"\nCreating {len(new_opps)} listings...\n")
    log_rows = []

    for i, row in enumerate(new_opps, 1):
        isbn  = row.get("ISBN-13","").strip()
        title = row.get("Title","Unknown")[:55]
        sku   = f"ISBN-{isbn}"
        price = calculate_competitive_price(row)
        if price is None: continue

        try: cost = float(str(row.get("Cost","0")).replace("$","").replace(",",""))
        except: cost = 0.0
        bgsgoat_url = row.get("URL","")

        print(f"[{i}/{len(new_opps)}] {title}...")

        cover_url = get_cover_image_url(isbn)
        print(f"  Cover: {'found' if cover_url else 'none'}")

        # Fetch metadata and generate description once — reused for both payloads
        meta      = get_book_metadata(isbn)
        ai_desc   = generate_description(title, isbn)
        full_desc = f"{ai_desc}\n\nISBN-13: {isbn}\n\n{CLOSING_STATEMENT}"

        # Step A: inventory item
        inv_r = create_inventory_item(sku, build_inventory_item_payload(row, cover_url, full_desc, meta))
        if inv_r.status_code not in (200, 204):
            msg = inv_r.text[:120]
            print(f"  Inventory FAILED ({inv_r.status_code}): {msg}")
            log_rows.append({"timestamp":ts,"isbn":isbn,"title":title,"price":price,
                              "offer_id":"","listing_id":"","status":"FAILED_INVENTORY","notes":msg})
            continue
        print(f"  Inventory item OK.")

        # Step B: offer
        offer_r = create_offer(build_offer_payload(row, sku, price, full_desc))
        if offer_r.status_code not in (200, 201):
            msg = offer_r.text[:120]
            print(f"  Offer FAILED ({offer_r.status_code}): {msg}")
            log_rows.append({"timestamp":ts,"isbn":isbn,"title":title,"price":price,
                              "offer_id":"","listing_id":"","status":"FAILED_OFFER","notes":msg})
            continue
        offer_id = offer_r.json().get("offerId")
        print(f"  Offer created: {offer_id}")

        # Step C: publish
        pub_r = publish_offer_single(offer_id)
        if pub_r.status_code in (200, 201):
            listing_id = pub_r.json().get("listingId","")
            print(f"  LIVE — Listing ID: {listing_id}")
            log_rows.append({"timestamp":ts,"isbn":isbn,"title":title,"price":price,
                              "offer_id":offer_id,"listing_id":listing_id,
                              "status":"PUBLISHED","notes":""})
            # Update unified state — tracker reads this
            state["listed_isbns"] = list(set(state.get("listed_isbns",[])) | {isbn})
            state["listings"][isbn] = {
                "offer_id":               offer_id,
                "listing_id":             listing_id,
                "title":                  row.get("Title","")[:80],
                "ebay_price":             price,
                "cost":                   cost,
                "booksgoat_url":          bgsgoat_url,
                "listed_at":              ts,
                "source":                 "auto",
                "status":                 "ACTIVE",
                "last_checked":           None,
                "last_supplier_price":    cost,
                "last_supplier_available": True,
                "delist_reason":          None,
                "delisted_at":            None,
            }
        else:
            msg = pub_r.text[:150]
            print(f"  Publish FAILED ({pub_r.status_code}): {msg}")
            log_rows.append({"timestamp":ts,"isbn":isbn,"title":title,"price":price,
                              "offer_id":offer_id,"listing_id":"","status":"FAILED_PUBLISH","notes":msg})

        time.sleep(0.3)

    state["last_run"] = ts
    save_state(state)
    if log_rows: append_log(log_rows)

    published = sum(1 for r in log_rows if r["status"]=="PUBLISHED")
    print(f"\n{'='*60}\nDone. {published} listed | {len(log_rows)-published} failed\n{'='*60}\n")


if __name__ == "__main__":
    run_lister()
