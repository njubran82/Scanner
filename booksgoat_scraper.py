#!/usr/bin/env python3
"""
booksgoat_scraper.py — BooksGoat weekly scraper (v3 — unified discovery)
Location : E:\\Book\\Scraper\\booksgoat_scraper.py
Schedule : Windows Task Scheduler, Monday 6:00 AM

Sources:
  1. BooksGoat category pages (5 URLs) — via Playwright
  2. BooksGoat merchant sheet (Google Sheets CSV) — via requests

Both sources are merged and deduplicated by ISBN-13.
New ISBNs not in booksgoat_enhanced.csv are appended as pending.
"""

import csv
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════
BASE_URL           = "https://www.booksgoat.com/index.php?route=product/category&path={path}"
CATEGORIES         = [315, 327, 328, 329, 330]
MERCHANT_SHEET_URL = os.environ.get(
    "BOOKSGOAT_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1uXD9-87xzSsU4wV0qw34r3OxmoZ5Gz4hw4vhrFykPfU/export?format=csv&gid=0"
)

COOLDOWN_DAYS          = 14
PRICE_CHANGE_THRESHOLD = 0.05
MAX_EMPTY_PAGES        = 2

CSV_PATH = Path(r"E:\Book\Lister\booksgoat_enhanced.csv")
LOG_PATH = Path(r"E:\Book\Scraper\scraper.log")

CSV_FIELDS = [
    "isbn13", "title", "format", "cost", "product_url", "category_path",
    "sell_price", "status", "score", "listed_at", "sold_at",
    "delisted_at", "delist_reason", "checked_at", "offer_id", "description",
]

# ════════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════════
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# CSV HELPERS
# ════════════════════════════════════════════════════════════════
def load_csv() -> dict:
    if not CSV_PATH.exists():
        return {}
    rows = {}
    with CSV_PATH.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            isbn = row.get("isbn13", "").strip()
            if isbn:
                for field in CSV_FIELDS:
                    row.setdefault(field, "")
                rows[isbn] = row
    log.info(f"Loaded {len(rows)} existing books from CSV")
    return rows


def save_csv(rows: dict):
    all_rows = list(rows.values())
    all_fields = list(dict.fromkeys(CSV_FIELDS + [k for r in all_rows for k in r]))
    tmp = CSV_PATH.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    tmp.replace(CSV_PATH)
    log.info(f"CSV saved: {len(all_rows)} rows")


# ════════════════════════════════════════════════════════════════
# PARSING HELPERS
# ════════════════════════════════════════════════════════════════
def extract_isbn(text: str) -> str | None:
    cleaned = re.sub(r"[-\s]", "", text)
    m = re.search(r"(?<!\d)(97[89]\d{10})(?!\d)", cleaned)
    return m.group(1) if m else None


def extract_price(text: str) -> float | None:
    m = re.search(r"\$([\d,]+\.?\d*)", text)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def detect_format(title: str) -> str:
    t = title.lower()
    if "spiral" in t:     return "Spiral Bound"
    if "loose leaf" in t: return "Loose Leaf"
    if "hardcover" in t:  return "Hardcover"
    return "Paperback"


# ════════════════════════════════════════════════════════════════
# SOURCE 1: Merchant sheet
# ════════════════════════════════════════════════════════════════
def scrape_merchant_sheet() -> dict:
    """
    Fetch BooksGoat Google Sheets merchant feed.
    Returns {isbn13: {isbn13, title, cost, category_path}} dict.
    Uses 10-qty price as cost (index 2 in price columns).
    """
    log.info("Fetching BooksGoat merchant sheet...")
    try:
        r = requests.get(MERCHANT_SHEET_URL, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Merchant sheet fetch failed: {e}")
        return {}

    results = {}
    reader = csv.DictReader(r.text.splitlines())
    skipped = 0

    for row in reader:
        # Normalize keys (strip whitespace)
        row = {k.strip(): v.strip() for k, v in row.items() if k}

        isbn13 = row.get("ISBN-13", "").strip().replace("-", "").replace(" ", "")
        if not isbn13 or not re.match(r"^97[89]\d{10}$", isbn13):
            skipped += 1
            continue

        title = row.get("Title", f"Book ISBN {isbn13}")

        # Try 10-qty price first, fall back to 5-qty
        price_str = row.get("10 Qty", "") or row.get("5 Qty", "")
        cost = extract_price(price_str)
        if not cost:
            skipped += 1
            continue

        results[isbn13] = {
            "isbn13":        isbn13,
            "title":         title,
            "format":        detect_format(title),
            "cost":          str(round(cost, 2)),
            "product_url":   "",
            "category_path": "merchant_sheet",
        }

    log.info(f"Merchant sheet: {len(results)} valid books ({skipped} skipped)")
    return results


# ════════════════════════════════════════════════════════════════
# SOURCE 2: Category pages (Playwright)
# ════════════════════════════════════════════════════════════════
def collect_urls_from_category(page, category_path: int) -> list[dict]:
    collected = []
    seen_urls = set()
    page_num = 1
    consecutive_empty = 0

    while consecutive_empty < MAX_EMPTY_PAGES:
        url = BASE_URL.format(path=category_path)
        if page_num > 1:
            url += f"&page={page_num}"

        log.info(f"  Category {category_path} page {page_num}")
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except PWTimeout:
            log.warning(f"  Timeout — stopping category {category_path}")
            break

        try:
            items = page.evaluate("""
                () => {
                    const results = [];
                    document.querySelectorAll('.product-layout').forEach(card => {
                        const titleEl = card.querySelector('.caption h4 a, h4 a');
                        const title = titleEl ? titleEl.textContent.trim() : '';
                        const linkEl = card.querySelector('a[href*="product/product"]');
                        let href = linkEl ? linkEl.getAttribute('href') : '';
                        if (href && !href.startsWith('http'))
                            href = 'https://www.booksgoat.com' + href;
                        const priceEl = card.querySelector('.price-new, .price');
                        const priceText = priceEl ? priceEl.textContent.trim() : '';
                        const cardText = card.textContent || '';
                        if (href) results.push({title, href, priceText, cardText});
                    });
                    return results;
                }
            """)
        except Exception as e:
            log.warning(f"  JS evaluate failed: {e}")
            consecutive_empty += 1
            page_num += 1
            continue

        page_count = 0
        for item in items:
            href = item.get("href", "")
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            page_count += 1
            collected.append({
                "url":           href,
                "title":         item.get("title", ""),
                "price":         extract_price(item.get("priceText", "") or item.get("cardText", "")),
                "isbn_in_card":  extract_isbn(item.get("cardText", "")),
                "category_path": str(category_path),
            })

        if page_count == 0:
            consecutive_empty += 1
            log.info(f"  Empty page {page_num} ({consecutive_empty}/{MAX_EMPTY_PAGES})")
        else:
            consecutive_empty = 0
            log.info(f"  Page {page_num}: {page_count} products")

        page_num += 1
        time.sleep(0.3)

    return collected


def enrich_with_isbns(context, products: list[dict]) -> list[dict]:
    enriched = []
    total = len(products)

    for i, product in enumerate(products):
        isbn = product.get("isbn_in_card")

        if not isbn:
            tab = None
            try:
                tab = context.new_page()
                tab.goto(product["url"], timeout=20000, wait_until="domcontentloaded")
                tab.wait_for_timeout(600)
                page_text = tab.evaluate("() => document.body.innerText")
                isbn = extract_isbn(page_text)
                if not product.get("price"):
                    product["price"] = extract_price(page_text)
                if not product.get("title"):
                    try:
                        h1 = tab.query_selector("h1")
                        if h1:
                            product["title"] = h1.inner_text().strip()
                    except Exception:
                        pass
            except Exception as e:
                log.warning(f"  Tab error [{i+1}/{total}]: {e}")
            finally:
                if tab:
                    try:
                        tab.close()
                    except Exception:
                        pass
            time.sleep(0.25)

        if isbn:
            product["isbn13"] = isbn
            log.info(f"  [{i+1}/{total}] {isbn} | ${product.get('price','?')} | {product['title'][:40]}")
            enriched.append(product)
        else:
            log.info(f"  [{i+1}/{total}] SKIP no ISBN | {product['title'][:45]}")

    return enriched


def scrape_category_pages() -> dict:
    """Scrape all 5 category pages. Returns {isbn13: product_dict}."""
    all_scraped = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        cat_page = context.new_page()

        for cat in CATEGORIES:
            log.info(f"Scraping category {cat}...")
            try:
                products = collect_urls_from_category(cat_page, cat)
                log.info(f"  Pass 1: {len(products)} URLs — starting ISBN extraction...")
                enriched = enrich_with_isbns(context, products)
                for prod in enriched:
                    isbn = prod["isbn13"]
                    if isbn not in all_scraped:
                        all_scraped[isbn] = prod
                log.info(f"  Category {cat}: {len(enriched)} ISBNs found")
            except Exception as e:
                log.error(f"Category {cat} failed: {e}")
            time.sleep(1)

        cat_page.close()
        context.close()
        browser.close()

    log.info(f"Category pages total: {len(all_scraped)} unique ISBNs")
    return all_scraped


# ════════════════════════════════════════════════════════════════
# MERGE + WRITE
# ════════════════════════════════════════════════════════════════
def apply_write_rules(rows: dict, all_scraped: dict, now: datetime) -> dict:
    stats = {"new": 0, "updated": 0, "skipped_active": 0,
             "skipped_cooldown": 0, "no_price": 0}

    for isbn, scraped in all_scraped.items():
        cost = scraped.get("cost") or (str(round(scraped.get("price", 0), 2)) if scraped.get("price") else None)
        if not cost or float(cost) == 0:
            stats["no_price"] += 1
            continue

        existing = rows.get(isbn)
        if existing:
            status = existing.get("status", "").strip()
            if status == "active":
                stats["skipped_active"] += 1
                continue
            if status == "delisted":
                delisted_at = existing.get("delisted_at", "")
                if delisted_at:
                    try:
                        days = (now - datetime.fromisoformat(delisted_at)).days
                        if days < COOLDOWN_DAYS:
                            stats["skipped_cooldown"] += 1
                            continue
                    except Exception:
                        pass
            if status in ("pending", ""):
                old = float(existing.get("cost", 0) or 0)
                new = float(cost)
                if old > 0 and abs(new - old) / old > PRICE_CHANGE_THRESHOLD:
                    log.info(f"  COST CHANGE {isbn}: ${old} → ${new}")
                existing["cost"] = str(round(float(cost), 2))
                if not existing.get("product_url") and scraped.get("url"):
                    existing["product_url"] = scraped.get("url", "")
                if not existing.get("category_path"):
                    existing["category_path"] = scraped.get("category_path", "")
                stats["updated"] += 1
                rows[isbn] = existing
        else:
            title = scraped.get("title", f"Book ISBN {isbn}")
            new_row = {f: "" for f in CSV_FIELDS}
            new_row.update({
                "isbn13":        isbn,
                "title":         title,
                "format":        scraped.get("format", detect_format(title)),
                "cost":          str(round(float(cost), 2)),
                "product_url":   scraped.get("url", ""),
                "category_path": scraped.get("category_path", ""),
                "status":        "pending",
            })
            rows[isbn] = new_row
            stats["new"] += 1
            log.info(f"  NEW {isbn} | ${cost} | {title[:50]}")

    return stats


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════
def run():
    log.info("=" * 70)
    log.info(f"BooksGoat scraper v3 started | {datetime.now().isoformat()}")

    rows = load_csv()
    now = datetime.now()

    # Source 1: Merchant sheet (fast, no browser needed)
    merchant_books = scrape_merchant_sheet()

    # Source 2: Category pages (Playwright)
    category_books = scrape_category_pages()

    # Merge — category pages win on price/url if same ISBN in both
    # (category pages have product URLs, merchant sheet does not)
    all_scraped = {**merchant_books, **category_books}
    log.info(f"Total unique ISBNs from all sources: {len(all_scraped)}")
    log.info(f"  Merchant sheet: {len(merchant_books)}")
    log.info(f"  Category pages: {len(category_books)}")
    overlap = len(set(merchant_books) & set(category_books))
    log.info(f"  Overlap (in both): {overlap}")

    # Apply write rules and update CSV
    stats = apply_write_rules(rows, all_scraped, now)
    save_csv(rows)

    log.info("-" * 70)
    log.info(
        f"Done: {stats['new']} new | {stats['updated']} updated | "
        f"{stats['skipped_active']} skipped active | "
        f"{stats['skipped_cooldown']} skipped cooldown | "
        f"{stats['no_price']} no price"
    )
    log.info("=" * 70)


if __name__ == "__main__":
    run()
