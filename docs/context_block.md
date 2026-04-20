# BooksGoat System Context Block
*Paste this at the start of any new AI chat*

## Identity
- LLC: Jubran Industries LLC | Brand: BooksGoat | eBay: `atlas_commerce`
- Model: Pure dropshipping — buy from BooksGoat ONLY after eBay sale confirmed
- GitHub: `njubran82/Scanner` | Alerts: jubran.industries@gmail.com

## Core rules
- eBay fee: 15.3% | Min profit: $12 | Undercut: 12% below lowest eBay comp
- Amazon price cap: 95% | Delist cooldown: 14 days | Qty: 20 | Handling: 10 days
- Scoring DISABLED — all books above $12 profit are listed (cap: 1,000 listings)

## Pipeline
```
BooksGoat site (5 categories) + merchant sheet (~90 books)
  → booksgoat_scraper.py [Monday 6AM, Windows Task Scheduler]
  → booksgoat_enhanced.csv (408 books, source of truth)
  → full_publish.py [manual] — PUT inventory + POST offer + publish
  → fix_listings.py [manual] — reprice + score + delist
  → booksgoat_tracker.py [daily 7AM, Windows Task Scheduler] — OOS/price check
  → GitHub Actions [Monday 9AM EST] — scanner + lister + repricer + weekly email
  → GitHub Actions [every 2h] — order_monitor → sale alert email
```

## Critical constraints
- BooksGoat blocks datacenter IPs — scraper/tracker must run locally via Playwright
- `merchantLocationKey: "home1"` MUST be in offer POST payload — silently dropped on PUT, causes error 25002 on publish
- `full_publish.py` is the correct bulk listing tool — creates inventory item first, then offer, then publishes
- `fix_listings.py` has `if False` hack — always POSTs new offer, fails if offer already exists
- OAuth token expires ~2 hours — restart fix_listings.py if run is long
- `booksgoat_tracker.py` is the ONLY component that can detect OOS — GitHub Actions IPs are blocked by Cloudflare, cloud repricer does price monitoring only

## Key files
| File | Path |
|---|---|
| Bulk publisher | `E:\Book\Lister\full_publish.py` |
| Lister / repricer | `E:\Book\Lister\fix_listings.py` |
| Working CSV | `E:\Book\Lister\booksgoat_enhanced.csv` |
| Scraper | `E:\Book\Scraper\booksgoat_scraper.py` |
| Tracker | `E:\Book\Tracker\booksgoat_tracker.py` |
| Scanner repo | `E:\Book\Scanner\` |

## Current state (04/20/2026)
- Active listings: 332 (CSV-managed) + ~60 manual = ~392 total
- Pending: ~16 stuck (offer already exists bug)
- All 332 active books have product_url — daily tracker covers all of them

## eBay policies
- Fulfillment: `391308514023` | Payment: `391308491023` | Return: `391308498023`
- Merchant location: `home1` | Category: `261186` | Marketplace: `EBAY_US`

## Permanent blocklist (do not list)
| ISBN | Reason |
|---|---|
| `9781260460445` | Min qty 5 on BooksGoat |
| `9780990873853` | Min qty 5 on BooksGoat |
| `9781119826798` | PDF only — no physical book |

## Known bugs
1. 🔴 `repricer.py` instant-delist cycle — 496 errors last run, delists profitable books
2. 🔴 `weekly_summary.py` duplicate opportunities — parser too broad
3. 🟠 `fix_listings.py` `if False` hack — offer POST always fails if offer exists
4. 🟠 `scanner.py` BLOCKLIST defined but never checked in scoring loop
5. 🟡 ~150 listings stuck at qty 10 from 04/19
6. 🟡 `sell.fulfillment` scope missing from eBay refresh token

## Top open tasks
1. Fix repricer.py instant-delist cycle
2. Delist 29 duplicate listings in Seller Hub
3. Upload photos for 42 no-photo books
4. End + Sell Similar for 7 dead offer Group B books
5. Add blocklist skip logic to scanner.py scoring loop
