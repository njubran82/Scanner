#!/usr/bin/env python3
"""
booksgoat_scraper.py — BooksGoat weekly scraper (v4 — three sources)
Location : E:\\Book\\Scraper\\booksgoat_scraper.py
Schedule : Windows Task Scheduler, Monday 6:00 AM

Sources:
  1. BooksGoat category pages (5 URLs) — via Playwright
  2. BooksGoat merchant sheet (Google Sheets CSV) — via requests
  3. BooksGoat "On Sale" homepage carousel — via Playwright (same browser session)

All three sources are merged and deduplicated by ISBN-13.
New ISBNs not in booksgoat_enhanced.csv are appended as pending.

Email alerts:
  - Sent on completion with full stats summary
  - Sent on failure with error details
  - Requires SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO env vars
"""

import csv
import logging
import os
import re
import smtplib
import time
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════
BASE_URL           = "https://www.booksgoat.com/index.php?route=product/category&path={path}"
HOMEPAGE_URL       = "https://www.booksgoat.com/"
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
# EMAIL ALERTS
# ════════════════════════════════════════════════════════════════
def send_email(subject: str, body: str):
    """Send email alert via SMTP. Fails silently if env vars not set."""
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    email_to  = os.environ.get("EMAIL_TO", os.environ.get("SMTP_USER", ""))

    if not smtp_user or not smtp_pass:
        log.warning("Email not sent — SMTP_USER / SMTP_PASSWORD not set")
        return

    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = email_to

        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, email_to, msg.as_string())

        log.info(f"Email sent: {subject}")
    except Exception as e:
        log.warning(f"Email failed: {e}")


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
    Uses 5-qty price as cost basis.
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
        row = {k.strip(): v.strip() for k, v in row.items() if k}

        isbn13 = row.get("ISBN-13", "").strip().replace("-", "").replace(" ", "")
        if not isbn13 or not re.match(r"^97[89]\d{10}$", isbn13):
            skipped += 1
            continue

        title = row.get("Title", f"Book ISBN {isbn13}")

        # Use 5-qty price as cost basis (corrected from 10-qty)
        price_str = row.get("5 Qty", "") or row.get("10 Qty", "")
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


def scrape_category_pages(context) -> dict:
    """Scrape all 5 category pages. Returns {isbn13: product_dict}."""
    all_scraped = {}
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
    log.info(f"Category pages total: {len(all_scraped)} unique ISBNs")
    return all_scraped


# ════════════════════════════════════════════════════════════════
# SOURCE 3: On Sale homepage carousel (Playwright)
# ════════════════════════════════════════════════════════════════
def scrape_on_sale_section(context) -> dict:
    """
    Scrape the 'On Sale' carousel from booksgoat.com homepage.
    Reuses the existing Playwright browser context — no second browser launch.

    Uses CAROUSEL prices as cost (what BooksGoat charges us).
    Product page prices are retail/Amazon prices — NOT used as cost.

    Returns {isbn13: product_dict} ready for apply_write_rules().
    """
    log.info("Scraping On Sale homepage carousel (Source 3)...")
    results = {}

    try:
        page = context.new_page()
        page.goto(HOMEPAGE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)  # let carousel JS initialize

        # Extract all products with CAROUSEL prices from On Sale section
        carousel_products = page.evaluate('''() => {
            const results = [];
            const seen = new Set();

            // Find On Sale container by heading text
            let saleContainer = null;
            const allElements = document.querySelectorAll(
                'h1, h2, h3, h4, h5, span, p, div, strong'
            );
            for (const el of allElements) {
                const ownText = Array.from(el.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent.trim())
                    .join(' ');
                const fullText = el.textContent?.trim() || '';
                if (ownText.match(/^On\\s+sale$/i) ||
                    (fullText.match(/^On\\s+sale$/i) && el.children.length === 0)) {
                    let parent = el.parentElement;
                    for (let i = 0; i < 10; i++) {
                        if (!parent) break;
                        if (parent.querySelectorAll('a[href]').length >= 3 &&
                            parent.querySelectorAll('img').length >= 2) {
                            saleContainer = parent;
                            break;
                        }
                        parent = parent.parentElement;
                    }
                    if (saleContainer) break;
                }
            }

            if (!saleContainer) return results;

            const links = saleContainer.querySelectorAll('a[href]');
            for (const a of links) {
                const href = a.href || '';
                if (!href || href === '#' || seen.has(href)) continue;
                if (!href.includes('booksgoat.com') ||
                    href === 'https://www.booksgoat.com/') continue;

                const parent = a.closest('div') || a.parentElement;
                const text = (parent || a).textContent || '';

                if (/\\$\\d+/.test(text) || /add\\s*to\\s*cart/i.test(text)) {
                    seen.add(href);

                    // Title
                    const card = a.closest('[class*="product"], [class*="item"]') || parent;
                    const titleEl = card?.querySelector(
                        '.product-name, h3, h4, h5, [class*="name"]'
                    );
                    let title = titleEl ? titleEl.textContent.trim() : a.textContent.trim();
                    title = title.replace(/add\\s*to\\s*cart/i, '')
                                 .replace(/\\$[\\d,.]+/g, '').trim();

                    // CAROUSEL price = BooksGoat cost
                    const priceMatch = text.match(/\\$(\\d+[.,]?\\d*)/);
                    const cost = priceMatch ? priceMatch[1] : '';

                    results.push({
                        url: href,
                        title: title.substring(0, 100),
                        cost: cost,
                    });
                }
            }
            return results;
        }''')

        page.close()

        if not carousel_products:
            log.warning("On Sale: no products found in carousel")
            return {}

        log.info(f"On Sale carousel: {len(carousel_products)} products visible")

        # Visit each product page for ISBN (price already from carousel)
        for i, prod in enumerate(carousel_products):
            url  = prod.get("url", "")
            cost = prod.get("cost", "")
            title_guess = prod.get("title", "")

            if not url:
                continue

            tab = None
            try:
                tab = context.new_page()
                tab.goto(url, wait_until="domcontentloaded", timeout=15000)
                tab.wait_for_timeout(1500)

                page_text = tab.evaluate("() => document.body.innerText || ''")

                # Extract ISBN
                isbn = None
                for pattern in [
                    r'ISBN[\s:\-]*(97[89][\d\s\-]{10,17})',
                    r'\(ISBN\s*(97[89]\d{10})\)',
                    r'\b(97[89]\d{10})\b',
                ]:
                    m = re.search(pattern, page_text, re.IGNORECASE)
                    if m:
                        isbn = re.sub(r'[\s\-]', '', m.group(1))
                        if len(isbn) == 13:
                            break
                        isbn = None

                if not isbn:
                    log.info(f"  [{i+1}/{len(carousel_products)}] SKIP no ISBN | {url[:60]}")
                    continue

                # Full title from page
                title = tab.evaluate("""() => {
                    for (const sel of ['h1', 'h2', '.product-title']) {
                        const el = document.querySelector(sel);
                        if (el && el.textContent.trim().length > 5)
                            return el.textContent.trim();
                    }
                    return document.title || '';
                }""") or title_guess

                # Format detection
                fmt_m = re.search(
                    r'(Hardcover|Paperback|Spiral Bound|Loose Leaf)',
                    page_text, re.IGNORECASE
                )
                fmt = fmt_m.group(1) if fmt_m else detect_format(title)

                results[isbn] = {
                    "isbn13":        isbn,
                    "title":         title,
                    "format":        fmt,
                    "cost":          cost,          # ← carousel price (BooksGoat cost)
                    "price":         float(cost) if cost else 0,
                    "url":           url,
                    "product_url":   url,
                    "category_path": "on_sale",
                }
                log.info(
                    f"  [{i+1}/{len(carousel_products)}] {isbn} | "
                    f"${cost} | {title[:45]}"
                )

            except Exception as e:
                log.warning(f"  [{i+1}/{len(carousel_products)}] Error {url[:60]}: {e}")
            finally:
                if tab:
                    try:
                        tab.close()
                    except Exception:
                        pass
            time.sleep(0.3)

    except Exception as e:
        log.error(f"On Sale scraper failed: {e}")

    log.info(f"On Sale total: {len(results)} unique ISBNs with valid ISBNs")
    return results


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
    start_time = datetime.now()
    log.info("=" * 70)
    log.info(f"BooksGoat scraper v4 started | {start_time.isoformat()}")

    try:
        rows = load_csv()
        now = datetime.now()

        # Source 1: Merchant sheet (fast, no browser)
        merchant_books = scrape_merchant_sheet()

        # Sources 2 + 3: Category pages + On Sale — single Playwright session
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

            # Source 2: Category pages
            category_books = scrape_category_pages(context)

            # Source 3: On Sale homepage carousel
            on_sale_books = scrape_on_sale_section(context)

            context.close()
            browser.close()

        # Merge all three sources
        # Priority: category pages > on_sale > merchant_sheet
        # (category pages have verified product URLs)
        all_scraped = {**merchant_books, **on_sale_books, **category_books}

        log.info(f"Total unique ISBNs from all sources: {len(all_scraped)}")
        log.info(f"  Source 1 — Merchant sheet:  {len(merchant_books)}")
        log.info(f"  Source 2 — Category pages:  {len(category_books)}")
        log.info(f"  Source 3 — On Sale section: {len(on_sale_books)}")
        overlap_12 = len(set(merchant_books) & set(category_books))
        overlap_3  = len(set(on_sale_books) - set(merchant_books) - set(category_books))
        log.info(f"  Overlap (sheet + category): {overlap_12}")
        log.info(f"  On Sale only (new ISBNs):   {overlap_3}")

        # Apply write rules and save
        stats = apply_write_rules(rows, all_scraped, now)
        save_csv(rows)

        elapsed = (datetime.now() - start_time).seconds
        summary = (
            f"BooksGoat scraper v4 completed at {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"Elapsed: {elapsed}s\n\n"
            f"Sources:\n"
            f"  Merchant sheet:  {len(merchant_books)} books\n"
            f"  Category pages:  {len(category_books)} books\n"
            f"  On Sale section: {len(on_sale_books)} books\n"
            f"  Total unique:    {len(all_scraped)} books\n\n"
            f"CSV changes:\n"
            f"  New books added:       {stats['new']}\n"
            f"  Costs updated:         {stats['updated']}\n"
            f"  Skipped (active):      {stats['skipped_active']}\n"
            f"  Skipped (cooldown):    {stats['skipped_cooldown']}\n"
            f"  Skipped (no price):    {stats['no_price']}\n\n"
            f"CSV total rows: {len(rows)}"
        )

        log.info("-" * 70)
        log.info(f"Done: {stats['new']} new | {stats['updated']} updated | "
                 f"{stats['skipped_active']} skipped active | "
                 f"{stats['skipped_cooldown']} skipped cooldown | "
                 f"{stats['no_price']} no price")
        log.info("=" * 70)

        # Success email
        send_email(
            subject=f"✅ BooksGoat Scraper — {stats['new']} new books | {datetime.now().strftime('%a %b %d')}",
            body=summary
        )

    except Exception as e:
        error_msg = traceback.format_exc()
        log.error(f"SCRAPER FAILED: {e}\n{error_msg}")

        # Failure email
        send_email(
            subject=f"❌ BooksGoat Scraper FAILED — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            body=(
                f"BooksGoat scraper failed at {datetime.now().isoformat()}\n\n"
                f"Error: {e}\n\n"
                f"Full traceback:\n{error_msg}"
            )
        )
        raise  # re-raise so Task Scheduler marks the job as failed


if __name__ == "__main__":
    run()
