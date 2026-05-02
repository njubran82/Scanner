#!/usr/bin/env python3
"""
=============================================================
  update_descriptions.py â€” Bulk Description Fix
  Version : 1.0.0
  Built   : 2026-05-02
  Location: E:/Book/Lister/update_descriptions.py
=============================================================

  PURPOSE
  -------
  Regenerates clean AI descriptions for ALL active listings
  in booksgoat_enhanced.csv and pushes them to eBay via the
  Inventory API. Fixes the "wall of repeating text" problem
  caused by the original vague Haiku prompt.

  WHAT IT DOES
  ------------
  1. Reads booksgoat_enhanced.csv for all active rows
  2. Generates a clean 2-3 sentence description via Claude Haiku
     using a strict, constrained prompt
  3. Caches the description in the CSV (description column)
  4. Upserts the inventory item on eBay with the new description
  5. Logs every action with timestamps

  MODES
  -----
  DRY_RUN = True   â†’ Generates descriptions + updates CSV only
                      (does NOT push to eBay)
  DRY_RUN = False  â†’ Full run: generate + cache + push to eBay

  USAGE
  -----
  python update_descriptions.py              # dry run (default)
  python update_descriptions.py --live       # push to eBay

=============================================================
"""

import os
import sys
import csv
import json
import time
import logging
import base64
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import requests
import anthropic


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# CONFIG
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
EBAY_APP_ID        = os.environ.get("EBAY_APP_ID", "")
EBAY_CERT_ID       = os.environ.get("EBAY_CERT_ID", "")
EBAY_REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

BASE_DIR   = Path(__file__).parent
CSV_PATH   = BASE_DIR / "booksgoat_enhanced.csv"
LOG_PATH   = BASE_DIR / "logs" / f"update_descriptions_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
BACKUP_CSV = BASE_DIR / f"booksgoat_enhanced_BACKUP_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"

DISCLAIMER = (
    "This item is sourced internationally to offer significant savings. "
    "All books are brand new and unread."
)

# Rate limiting
HAIKU_DELAY   = 0.5   # seconds between Haiku calls
EBAY_DELAY    = 1.0   # seconds between eBay API calls
MAX_RETRIES   = 3


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# LOGGING
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
os.makedirs(LOG_PATH.parent, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_PATH)),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("update_descriptions")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# IMPROVED AI DESCRIPTION â€” THE FIX
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def generate_clean_description(title: str, isbn: str) -> Optional[str]:
    """
    Generate a concise, non-repetitive eBay listing description.

    The prompt is heavily constrained to prevent the wall-of-text
    problem from the original version.
    """
    if not ANTHROPIC_API_KEY:
        log.warning("No ANTHROPIC_API_KEY set â€” using fallback description")
        return None

    # Strip common publisher marketing suffixes from the title
    # before sending to Haiku to reduce regurgitation
    clean_title = title.split(" - Comprehensive")[0]
    clean_title = clean_title.split(" - A Complete")[0]
    clean_title = clean_title.split(", 2nd Edition")[0] + (
        ", 2nd Edition" if "2nd Edition" in title else ""
    )
    clean_title = clean_title[:120]  # Hard cap

    prompt = f"""Write an eBay listing description for this textbook.

Title: {clean_title}
ISBN: {isbn}

STRICT RULES â€” violating any rule means the description is rejected:
1. Exactly 2â€“3 sentences. No more.
2. First sentence: what the book covers and who it's for.
3. Second sentence: one standout feature (e.g., edition highlights, page count, practice questions).
4. Optional third sentence: only if genuinely adding new info.
5. DO NOT list chapter names, division names, or feature bullet points.
6. DO NOT repeat any phrase or idea â€” every sentence must say something new.
7. DO NOT use marketing language like "must-have", "comprehensive", "essential", "trusted".
8. DO NOT mention price, condition, shipping, or seller info.
9. Plain text only. No bullet points, no headers, no special formatting.
10. Total length: 40â€“80 words maximum.

Write the description now:"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        desc = msg.content[0].text.strip()

        # Post-processing safety checks
        desc = desc.replace('"', '').replace("Here's", "").replace("Here is", "")
        desc = desc.strip('" \n')

        # Reject if still too long (Haiku ignored the constraint)
        word_count = len(desc.split())
        if word_count > 100:
            log.warning(f"  Description too long ({word_count} words) â€” truncating to first 3 sentences")
            sentences = desc.split(". ")
            desc = ". ".join(sentences[:3])
            if not desc.endswith("."):
                desc += "."

        # Reject if obvious repetition detected
        sentences = [s.strip() for s in desc.split(". ") if s.strip()]
        if len(sentences) >= 2:
            # Check if any two sentences share >50% of their words
            for i in range(len(sentences)):
                for j in range(i + 1, len(sentences)):
                    words_i = set(sentences[i].lower().split())
                    words_j = set(sentences[j].lower().split())
                    if len(words_i) > 3 and len(words_j) > 3:
                        overlap = len(words_i & words_j) / min(len(words_i), len(words_j))
                        if overlap > 0.5:
                            log.warning(f"  Repetition detected ({overlap:.0%} overlap) â€” keeping only first sentence")
                            desc = sentences[0]
                            if not desc.endswith("."):
                                desc += "."

        return desc

    except Exception as e:
        log.error(f"  Claude API error: {e}")
        return None


def build_full_description(ai_desc: Optional[str], title: str, isbn: str) -> str:
    """Assemble the final listing description with disclaimer."""
    if ai_desc:
        return f"{ai_desc}\n\nISBN: {isbn}\n\n{DISCLAIMER}"
    else:
        # Fallback: clean title only
        return f"{title}\n\nISBN: {isbn}\n\n{DISCLAIMER}"


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# EBAY AUTH
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def get_user_token() -> str:
    """Get a fresh user access token via OAuth refresh."""
    credentials = base64.b64encode(
        f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()
    ).decode()

    r = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": EBAY_REFRESH_TOKEN,
            "scope": "https://api.ebay.com/oauth/api_scope/sell.inventory",
        },
        timeout=15,
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    log.info("eBay user token refreshed successfully")
    return token


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# EBAY INVENTORY UPSERT
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def extract_format(title: str) -> str:
    t = title.lower()
    if "hardcover" in t or "hardback" in t:
        return "Hardcover"
    if "spiral" in t or "ring" in t:
        return "Spiral-bound"
    return "Paperback"


def upsert_description(
    sku: str,
    title: str,
    description: str,
    user_token: str,
    image_url: Optional[str] = None,
) -> bool:
    """
    Update an existing inventory item's description on eBay.
    Uses PUT (full replace) on the Inventory Item endpoint.
    """
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }

    clean_title = title[:80]
    fmt = extract_format(title)

    product = {
        "title": clean_title,
        "description": description,
        "isbn": [sku],
        "aspects": {
            "Book Title": [clean_title],
            "ISBN":       [sku],
            "Format":     [fmt],
            "Language":   ["English"],
        },
    }
    if image_url:
        product["imageUrls"] = [image_url]

    payload = {
        "product": product,
        "condition": "NEW",
        "conditionDescription": "Brand new, unread copy.",
        "availability": {
            "shipToLocationAvailability": {"quantity": 1}
        },
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.put(
                f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}",
                headers=headers,
                json=payload,
                timeout=20,
            )
            if r.status_code in (200, 201, 204):
                return True
            elif r.status_code == 429:
                wait = 2 ** attempt
                log.warning(f"  Rate limited â€” waiting {wait}s (attempt {attempt})")
                time.sleep(wait)
            else:
                log.error(f"  eBay upsert failed [{r.status_code}]: {r.text[:300]}")
                return False
        except requests.RequestException as e:
            log.error(f"  Network error on attempt {attempt}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    return False


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# CSV HANDLING
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def load_csv() -> list[dict]:
    """Load booksgoat_enhanced.csv and return rows as dicts."""
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def save_csv(rows: list[dict]) -> None:
    """Write rows back to CSV, preserving all columns."""
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    # Ensure 'description' column exists
    if "description" not in fieldnames:
        fieldnames.append("description")
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def backup_csv() -> None:
    """Create a timestamped backup before making changes."""
    import shutil
    shutil.copy2(CSV_PATH, BACKUP_CSV)
    log.info(f"CSV backed up to {BACKUP_CSV.name}")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# MAIN
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
def main():
    parser = argparse.ArgumentParser(description="Bulk-fix eBay listing descriptions")
    parser.add_argument("--live", action="store_true", help="Push updates to eBay (default: dry run)")
    parser.add_argument("--force", action="store_true", help="Regenerate even if description column is already populated")
    parser.add_argument("--limit", type=int, default=0, help="Only process first N books (0 = all)")
    args = parser.parse_args()

    dry_run = not args.live

    log.info("=" * 60)
    log.info("  UPDATE DESCRIPTIONS â€” Bulk Fix")
    log.info(f"  Mode:  {'DRY RUN' if dry_run else 'ðŸ”´ LIVE â€” pushing to eBay'}")
    log.info(f"  Force: {args.force}")
    log.info(f"  Limit: {args.limit or 'ALL'}")
    log.info("=" * 60)

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set â€” cannot generate descriptions. Exiting.")
        sys.exit(1)

    # Load CSV
    rows = load_csv()
    active_rows = [r for r in rows if r.get("status", "").lower() == "active"]
    log.info(f"Total rows: {len(rows)} | Active: {len(active_rows)}")

    if not active_rows:
        log.warning("No active rows found. Nothing to do.")
        return

    # Backup before changes
    backup_csv()

    # Get eBay token if live mode
    user_token = None
    if not dry_run:
        try:
            user_token = get_user_token()
        except Exception as e:
            log.error(f"Failed to get eBay token: {e}")
            log.error("Cannot push to eBay â€” falling back to dry run")
            dry_run = True

    # Process
    stats = {"generated": 0, "skipped": 0, "pushed": 0, "push_failed": 0, "errors": 0}
    target_rows = active_rows[:args.limit] if args.limit > 0 else active_rows

    for i, row in enumerate(target_rows, 1):
        isbn  = row.get("isbn13", "").strip()
        title = row.get("title", "").strip()

        if not isbn or not title:
            log.warning(f"[{i}/{len(target_rows)}] Skipping row with missing ISBN or title")
            stats["skipped"] += 1
            continue

        # Check if description already exists (and we're not forcing)
        existing_desc = row.get("description", "").strip()
        if existing_desc and not args.force:
            log.info(f"[{i}/{len(target_rows)}] {isbn} â€” already has description, skipping (use --force to override)")
            stats["skipped"] += 1
            continue

        log.info(f"[{i}/{len(target_rows)}] {isbn} â€” {title[:60]}...")

        # Generate new description
        ai_desc = generate_clean_description(title, isbn)
        if ai_desc:
            log.info(f"  âœ“ Generated: {ai_desc[:80]}...")
            stats["generated"] += 1
        else:
            log.warning(f"  âœ— AI generation failed â€” using fallback")
            stats["errors"] += 1

        full_desc = build_full_description(ai_desc, title, isbn)

        # Cache in CSV
        row["description"] = ai_desc or ""

        # Push to eBay
        if not dry_run and user_token:
            image_url = row.get("image_url", "").strip() or None
            success = upsert_description(isbn, title, full_desc, user_token, image_url)
            if success:
                log.info(f"  âœ“ Pushed to eBay")
                stats["pushed"] += 1
            else:
                log.error(f"  âœ— eBay push failed")
                stats["push_failed"] += 1
            time.sleep(EBAY_DELAY)

        time.sleep(HAIKU_DELAY)

    # Save updated CSV
    save_csv(rows)
    log.info(f"CSV saved with cached descriptions")

    # Summary
    log.info("")
    log.info("=" * 60)
    log.info("  SUMMARY")
    log.info("=" * 60)
    log.info(f"  Descriptions generated : {stats['generated']}")
    log.info(f"  Skipped (already done) : {stats['skipped']}")
    log.info(f"  Pushed to eBay         : {stats['pushed']}")
    log.info(f"  eBay push failures     : {stats['push_failed']}")
    log.info(f"  AI generation errors   : {stats['errors']}")
    log.info("=" * 60)

    if dry_run:
        log.info("")
        log.info("  DRY RUN complete â€” descriptions cached in CSV but NOT pushed to eBay.")
        log.info("  To push live, run:  python update_descriptions.py --live")
        log.info("")


if __name__ == "__main__":
    main()

