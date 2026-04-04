"""
scanner.py — Main orchestrator for one complete scan cycle.

ALERT FLOW (per run):
    1. Analyze all books from supplier
    2. Classify opportunities:
         new         → IMMEDIATE alert email + SMS
         significant → IMMEDIATE alert email + SMS
         suppressed  → silent (included in daily summary only)
    3. Daily summary gate:
         First scan at or after DAILY_SUMMARY_HOUR UTC that hasn't
         sent today's summary yet → send summary email
    4. Save scanner_results.csv (overwrite) + timestamped archive

RESULT:
    - You get immediate alerts only when something genuinely new or
      meaningfully better appears.
    - You get one digest per day covering everything currently live.
    - The scanner can run hourly without spamming you.
"""

import logging
import csv
from datetime import datetime
from typing import List, Optional
from pathlib import Path
from collections import Counter

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from suppliers import get_supplier
from profit_analyzer import analyze_all
from market_data import get_health_summary as get_api_health_summary
from state_tracker import (
    classify_opportunities,
    should_send_daily_summary,
    mark_daily_summary_sent,
)
from notifier import send_immediate_alert, send_daily_summary, send_sms, send_error_alert
from models import Opportunity
import config

logger = logging.getLogger(__name__)

CSV_LATEST_PATH = "scanner_results.csv"


# ── CSV ───────────────────────────────────────────────────────────────────────

def save_results_csv(all_results: List[Opportunity]) -> str:
    """Save all results to scanner_results.csv + timestamped archive."""
    sorted_results = sorted(
        all_results,
        key=lambda o: (not o.is_opportunity, -o.profit)
    )

    header = [
        "Opportunity", "Confidence", "Title", "ISBN-13", "ISBN-10",
        "Supplier Cost", "Revenue Estimate", "Revenue Source",
        "eBay Fee", "Profit", "Margin %",
        "eBay Sold Count", "eBay Sold Median", "Price Spread %",
        "eBay Active Count", "eBay Active Median",
        "Amazon Price", "Amazon Rank",
        "Concern Flags", "Skip Reason", "Data Source",
    ]

    rows = []
    for opp in sorted_results:
        b = opp.book
        rows.append([
            "YES" if opp.is_opportunity else "NO",
            opp.confidence,
            b.title, b.isbn13, b.isbn10,
            f"${b.cost:.2f}",
            f"${opp.revenue_estimate:.2f}",
            opp.revenue_source,
            f"${opp.ebay_fee:.2f}",
            f"${opp.profit:.2f}",
            f"{opp.margin_pct*100:.1f}%",
            opp.ebay_sold_count,
            f"${opp.ebay_sold_median:.2f}" if opp.ebay_sold_median else "",
            opp.price_spread_pct if opp.price_spread_pct else "",
            opp.ebay_active_count,
            f"${opp.ebay_active_median:.2f}" if opp.ebay_active_median else "",
            f"${b.amazon_price:.2f}" if b.amazon_price else "",
            b.amazon_rank or "",
            opp.concern_str,
            opp.skip_reason,
            b.source,
        ])

    _write_csv(CSV_LATEST_PATH, header, rows)
    logger.info(f"Results saved → {CSV_LATEST_PATH}")

    archive_dir = Path("scan_results")
    archive_dir.mkdir(exist_ok=True)
    ts_path     = archive_dir / f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    _write_csv(str(ts_path), header, rows)
    logger.info(f"Archive saved  → {ts_path}")

    return CSV_LATEST_PATH


def _write_csv(path: str, header: list, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ── Terminal summary ──────────────────────────────────────────────────────────

def print_scan_summary(
    new_opps:      List[Opportunity],
    significant:   List[Opportunity],
    suppressed:    List[Opportunity],
    all_results:   List[Opportunity],
    api_health:    dict,
) -> None:
    total      = len(all_results)
    all_opps   = new_opps + significant + suppressed
    actionable = len(new_opps) + len(significant)

    mode_sym = {
        "EBAY_CONFIRMED": "🟢",
        "MIXED":          "🟡",
        "FALLBACK_ONLY":  "🔴",
    }.get(api_health.get("run_mode", ""), "⚪")

    print("\n" + "═" * 100)
    print(
        f"  SCAN COMPLETE  |  {total} books  |  "
        f"{len(all_opps)} opportunities  |  "
        f"{actionable} immediate alerts  |  "
        f"{len(suppressed)} suppressed"
    )
    print(
        f"  {mode_sym} {api_health.get('run_mode','?')} — "
        f"{api_health.get('run_mode_reason','')}"
    )
    print("═" * 100)

    def _section(opps: List[Opportunity], label: str) -> None:
        if not opps:
            return
        print(f"\n  {label}")
        print(
            f"  {'#':<3}  {'Conf':<8}  {'Title':<48}  "
            f"{'Cost':>6}  {'Rev':>8}  {'Profit':>7}  "
            f"{'ROI':>5}  {'Src':<11}  {'Sold':>4}  Concerns"
        )
        print("  " + "─" * 115)
        for i, opp in enumerate(opps, 1):
            roi = opp.profit / opp.book.cost * 100 if opp.book.cost else 0
            src = {
                "ebay_sold":       "eBay sold",
                "ebay_active":     "eBay active",
                "amazon_estimate": "Amazon~",
            }.get(opp.revenue_source, opp.revenue_source)
            flags = opp.concern_str[:28] if opp.concern_flags else "—"
            print(
                f"  {i:<3}  {opp.confidence:<8}  "
                f"{opp.book.title[:47]:<48}  "
                f"${opp.book.cost:>5.2f}  "
                f"${opp.revenue_estimate:>7.2f}  "
                f"${opp.profit:>6.2f}  "
                f"{roi:>4.0f}%  "
                f"{src:<11}  "
                f"{opp.ebay_sold_count:>4}  "
                f"{flags}"
            )

    _section(new_opps,    "🆕 NEW — immediate alert")
    _section(significant, "📈 SIGNIFICANT GAIN — immediate alert")

    if suppressed:
        print(f"\n  — {len(suppressed)} suppressed (no significant change — daily summary only)")

    # Confidence breakdown
    conf_counts = Counter(o.confidence for o in all_opps)
    print(f"\n  Confidence: " + "  ".join(
        f"{k}:{v}" for k, v in sorted(conf_counts.items())
    ))

    # Concern summary
    all_flags = [f for o in all_opps for f in o.concern_flags]
    if all_flags:
        top_flags = Counter(all_flags).most_common(4)
        print(f"  Concerns:   " + "  ".join(f"{v}× {k}" for k, v in top_flags))

    print("═" * 100 + "\n")


# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan() -> List[Opportunity]:
    """Execute one full scan. Returns all opportunities that passed thresholds."""
    logger.info("=" * 70)
    logger.info(f"SCAN START — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(
        f"Supplier: {config.SUPPLIER} | "
        f"Min profit: ${config.MIN_PROFIT} | "
        f"Min margin: {config.MIN_MARGIN*100:.0f}% | "
        f"eBay App ID: {'SET' if config.EBAY_APP_ID else 'MISSING ⚠'}"
    )
    logger.info("=" * 70)

    if not config.EBAY_APP_ID:
        logger.warning(
            "EBAY_APP_ID not configured — this run uses Amazon fallback only. "
            "Results will be marked FALLBACK and should not be used for execution."
        )

    try:
        # ── 1. Load ──────────────────────────────────────────────────────
        supplier = get_supplier()
        books    = supplier.get_books()

        if not books:
            logger.error("No books loaded — aborting")
            send_error_alert("Scanner aborted: no books loaded")
            return []

        logger.info(f"Loaded {len(books)} books from {supplier}")

        # ── 2. Analyze ───────────────────────────────────────────────────
        all_results   = analyze_all(books)
        opportunities = [o for o in all_results if o.is_opportunity]
        api_health    = get_api_health_summary()

        logger.info(
            f"Analysis: {len(opportunities)} opportunities | "
            f"eBay: {api_health['sold_successes']} OK / "
            f"{api_health['sold_failures']} failed | "
            f"Mode: {api_health['run_mode']}"
        )

        # ── 3. Classify ──────────────────────────────────────────────────
        new_opps, significant, suppressed = classify_opportunities(opportunities)

        # ── 4. Terminal output ───────────────────────────────────────────
        print_scan_summary(
            new_opps, significant, suppressed, all_results, api_health
        )

        # ── 5. Save CSV (every run) ──────────────────────────────────────
        csv_path = save_results_csv(all_results)

        # ── 6. Immediate alert — new or significant only ─────────────────
        if new_opps or significant:
            logger.info(
                f"Sending immediate alert: "
                f"{len(new_opps)} new, {len(significant)} significant"
            )
            send_immediate_alert(
                new_opps     = new_opps,
                significant  = significant,
                total_scanned= len(books),
                api_health   = api_health,
                csv_path     = csv_path,
            )
            send_sms(
                new_opps     = new_opps,
                significant  = significant,
                total_scanned= len(books),
                api_health   = api_health,
            )
        else:
            logger.info("No new or significant opportunities — immediate alert suppressed")

        # ── 7. Daily summary — once per day ──────────────────────────────
        if should_send_daily_summary():
            logger.info("Sending daily summary")
            sent = send_daily_summary(
                all_opportunities = opportunities,
                total_scanned     = len(books),
                api_health        = api_health,
                csv_path          = csv_path,
            )
            if sent:
                mark_daily_summary_sent()
        else:
            logger.info("Daily summary not due this run")

        logger.info(
            f"SCAN DONE — {len(new_opps)} new | "
            f"{len(significant)} significant | "
            f"{len(suppressed)} suppressed | "
            f"mode: {api_health['run_mode']}"
        )
        return opportunities

    except Exception as e:
        logger.exception(f"Scanner crashed: {e}")
        send_error_alert(f"Scanner crashed: {e}")
        raise
