# BooksGoat Book Arbitrage Pipeline

**Jubran Industries LLC** | eBay seller: `atlas_commerce` | Repo: `njubran82/Scanner`

Pure dropshipping — buy from BooksGoat only after eBay sale confirmed.

---

## Quick reference

| Rule | Value |
|---|---|
| eBay fee rate | 15.3% |
| Min profit | $12.00 |
| Undercut | 12% below lowest eBay comp |
| Amazon price cap | 95% of Amazon list price |
| Listing quantity | 20 |
| Handling time | 10 days |
| Delist cooldown | 14 days |
| Max listings | 1,000 (Basic Store) |
| Scoring | DISABLED — all profitable books listed |

---

## File locations

| Component | Path |
|---|---|
| Main lister / repricer | `E:\Book\Lister\fix_listings.py` |
| Bulk publisher | `E:\Book\Lister\full_publish.py` |
| Working CSV | `E:\Book\Lister\booksgoat_enhanced.csv` |
| Scraper | `E:\Book\Scraper\booksgoat_scraper.py` |
| Local tracker | `E:\Book\Tracker\booksgoat_tracker.py` |
| Scanner repo | `E:\Book\Scanner\` |

---

## How to run things

### List new pending books
```
cd E:\Book\Lister
python full_publish.py
```
Creates inventory item + offer + publishes. Handles all 25702/25002 errors correctly. Skips blocklist. Saves every 10 books.

### Reprice + score + delist
```
cd E:\Book\Lister
python fix_listings.py
```
Phase 1: scores all 408 books. Phase 2: reprices active. Phase 3: delists unprofitable. Token expires ~2 hours — restart if needed.

### Scrape new books from BooksGoat
```
cd E:\Book\Scraper
python booksgoat_scraper.py
```
Runs automatically Monday 6AM via Task Scheduler. Merges merchant sheet + 5 category pages. Appends new pending rows to CSV.

---

## Schedules

| Task | When | Script |
|---|---|---|
| BooksGoat scraper | Monday 6:00 AM (Windows Task Scheduler) | `booksgoat_scraper.py` |
| GitHub Actions scanner | Monday 9:00 AM EST (GitHub Actions) | `scanner.py → lister.py → repricer.py → weekly_summary.py` |
| Daily OOS tracker | Daily 7:00 AM (Windows Task Scheduler) | `booksgoat_tracker.py` |
| Order monitor | Every 2 hours (GitHub Actions) | `order_monitor.py` |

---

## eBay policies

| Policy | ID |
|---|---|
| Fulfillment | `391308514023` — 10 day handling |
| Payment | `391308491023` |
| Return | `391308498023` |
| Merchant location | `home1` — **must be in offer POST or publish fails** |
| Category | `261186` |

---

## Permanent blocklist

Books that cannot be dropshipped — excluded from all listing scripts.

| ISBN | Title | Reason |
|---|---|---|
| `9781260460445` | Lange Q&A Radiography Examination | Min qty 5 |
| `9780990873853` | Overcoming Gravity: Gymnastics | Min qty 5 |
| `9781119826798` | Architect's Studio Companion | PDF only on BooksGoat |

To add a new entry: update `MIN_QTY_BLOCKLIST` in `fix_listings.py` and `BLOCKLIST` in `scanner.py`, then push.

---

## 4 permanent eBay catalog conflicts (25002)

These fail API listing due to eBay catalog conflicts. List manually in Seller Hub instead.

| ISBN | Title |
|---|---|
| `9781609836184` | Concrete Manual (2015 IBC / ACI 318-14) |
| `9780763781873` | National Incident Management System |
| `9781951058067` | ASQ Certified Manager of Quality Handbook |
| `9781951058098` | ASQ Certified Quality Auditor Handbook |

---

## Known bugs

| # | Severity | Bug |
|---|---|---|
| 1 | 🔴 Critical | `repricer.py` instant-delist cycle — 496 errors last run |
| 2 | 🔴 Critical | `weekly_summary.py` duplicate opportunities list |
| 3 | 🟠 High | `fix_listings.py` `if False` hack — always POSTs new offer, fails if offer exists |
| 4 | 🟠 High | `scanner.py` BLOCKLIST defined but not checked in scoring loop |
| 5 | 🟡 Medium | ~150 listings stuck at qty 10 from 04/19 |
| 6 | 🟡 Medium | `sell.fulfillment` scope missing from refresh token |
| 7 | 🟡 Medium | `full_publish.py` doesn't generate AI descriptions |

---

## Order fulfillment (manual)

When you get a sale email from `order_monitor.py`:
1. Open the BooksGoat URL in the email
2. Add 1 unit to cart — verify single-unit purchase is available
3. Enter buyer's shipping address, pay
4. Enter tracking number in eBay Seller Hub after shipment

> ⚠️ Some books require minimum 5-unit purchase. If cart forces qty 5+, cancel the eBay order immediately and add the ISBN to the blocklist.

---

## Full spec

See [`docs/spec_v1.html`](docs/spec_v1.html) for the complete system reference with tabbed navigation.

---

*Last updated: 04/20/2026*
