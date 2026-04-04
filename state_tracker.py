"""
state_tracker.py — Tracks opportunities across runs. Controls all alert logic.

ALERT RULES (in priority order):
    1. NEW opportunity   — ISBN never seen before → immediate alert
    2. SIGNIFICANT GAIN  — Existing opp improved beyond threshold → immediate alert
    3. SUPPRESSED        — Everything else → silent, included in daily summary only

SIGNIFICANT GAIN definition (either condition triggers re-alert):
    Profit increase  ≥ config.SIGNIFICANT_PROFIT_INCREASE_PCT  (default 20%)
    ROI increase     ≥ config.SIGNIFICANT_ROI_INCREASE_PCT     (default 15%)

    ROI = profit / supplier_cost
    Only improvements trigger alerts. Degradation is tracked silently.

DAILY SUMMARY:
    One email per day, first scan at or after config.DAILY_SUMMARY_HOUR (UTC).
    Contains ALL current opportunities grouped by confidence level, top 5 by profit.
    Tracked via _meta.last_daily_summary_date in the state file.

STATE FILE STRUCTURE (scanner_state.json):
    {
      "_meta": {
        "last_daily_summary_date": "2025-04-07"
      },
      "9780879396961": {
        "isbn13":               "9780879396961",
        "title":                "Fire and Emergency Services...",
        "profit":               24.50,       ← current profit (updated every scan)
        "roi":                  0.42,        ← current ROI
        "last_alerted_profit":  24.50,       ← profit at time of last alert
        "last_alerted_roi":     0.42,        ← ROI at time of last alert
        "revenue_estimate":     82.45,
        "revenue_source":       "ebay_sold",
        "confidence":           "HIGH",
        "first_seen":           "2025-04-03T10:00:00+00:00",
        "last_alerted":         "2025-04-03T10:00:00+00:00",
        "alert_count":          1
      }
    }
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Tuple, Dict, Optional
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import Opportunity
import config

logger = logging.getLogger(__name__)


# ── State file I/O ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _current_hour_utc() -> int:
    return datetime.now(timezone.utc).hour


def _load_state() -> Dict:
    """Load state file. Returns dict with _meta key always present."""
    path = Path(config.STATE_FILE_PATH)
    base = {"_meta": {"last_daily_summary_date": ""}}

    if not path.exists():
        logger.info(f"No state file at '{path}' — starting fresh")
        return base

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure _meta always exists
        if "_meta" not in data:
            data["_meta"] = {"last_daily_summary_date": ""}
        n = len([k for k in data if k != "_meta"])
        logger.info(f"Loaded state: {n} tracked opportunities from '{path}'")
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Could not read state file '{path}': {e} — starting fresh")
        return base


def _save_state(state: Dict) -> None:
    path = Path(config.STATE_FILE_PATH)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        n = len([k for k in state if k != "_meta"])
        logger.debug(f"State saved: {n} opportunities → '{path}'")
    except IOError as e:
        logger.error(f"Failed to save state: {e}")


# ── ROI helper ────────────────────────────────────────────────────────────────

def _roi(opp: Opportunity) -> float:
    """Return on spend: profit / supplier cost. Returns 0 if cost is 0."""
    cost = opp.book.cost
    return round(opp.profit / cost, 4) if cost > 0 else 0.0


# ── Significant change detection ──────────────────────────────────────────────

def _is_significant_gain(opp: Opportunity, prev: dict) -> Tuple[bool, str]:
    """
    Return (True, reason) if this opportunity improved significantly
    since the last alert, based on profit % or ROI % increase.

    Only improvements trigger re-alerts — degradation is suppressed.
    """
    last_profit = prev.get("last_alerted_profit", prev.get("profit", 0))
    last_roi    = prev.get("last_alerted_roi",    prev.get("roi", 0))
    new_roi     = _roi(opp)

    # Only check improvements (positive delta)
    if opp.profit <= last_profit and new_roi <= last_roi:
        return False, ""

    # Profit increase check
    if last_profit > 0:
        profit_pct = (opp.profit - last_profit) / abs(last_profit)
        if profit_pct >= config.SIGNIFICANT_PROFIT_INCREASE_PCT:
            return True, (
                f"profit +{profit_pct*100:.0f}% "
                f"(${last_profit:.2f} → ${opp.profit:.2f})"
            )

    # ROI increase check
    if last_roi > 0:
        roi_pct = (new_roi - last_roi) / abs(last_roi)
        if roi_pct >= config.SIGNIFICANT_ROI_INCREASE_PCT:
            return True, (
                f"ROI +{roi_pct*100:.0f}% "
                f"({last_roi*100:.0f}% → {new_roi*100:.0f}%)"
            )

    return False, ""


# ── Main classification ───────────────────────────────────────────────────────

def classify_opportunities(
    opportunities: List[Opportunity],
) -> Tuple[List[Opportunity], List[Opportunity], List[Opportunity]]:
    """
    Classify opportunities into three buckets:

        new_opps        → Never seen before. Alert immediately.
        significant     → Existing, but improved beyond threshold. Alert immediately.
        suppressed      → Known, no meaningful change. Include in daily summary only.

    Also updates the state file with current values.
    Returns (new_opps, significant_opps, suppressed_opps).
    """
    if not config.STATE_TRACKING_ENABLED:
        logger.info("State tracking disabled — all opportunities treated as new")
        return opportunities, [], []

    state      = _load_state()
    new_opps   = []
    significant = []
    suppressed  = []
    now         = _now_iso()

    for opp in opportunities:
        isbn = opp.book.isbn13
        prev = state.get(isbn)
        roi  = _roi(opp)

        if prev is None:
            # ── Brand new ─────────────────────────────────────────────
            new_opps.append(opp)
            state[isbn] = {
                "isbn13":              isbn,
                "title":               opp.book.title,
                "profit":              opp.profit,
                "roi":                 roi,
                "last_alerted_profit": opp.profit,
                "last_alerted_roi":    roi,
                "revenue_estimate":    opp.revenue_estimate,
                "revenue_source":      opp.revenue_source,
                "confidence":          opp.confidence,
                "first_seen":          now,
                "last_alerted":        now,
                "alert_count":         1,
            }
            logger.info(
                f"NEW: '{ opp.book.title[:55]}' "
                f"profit=${opp.profit:.2f} roi={roi*100:.0f}%"
            )

        else:
            # ── Already known — check for significant improvement ──────
            gained, reason = _is_significant_gain(opp, prev)

            if gained:
                significant.append(opp)
                state[isbn].update({
                    "profit":              opp.profit,
                    "roi":                 roi,
                    "last_alerted_profit": opp.profit,
                    "last_alerted_roi":    roi,
                    "revenue_estimate":    opp.revenue_estimate,
                    "revenue_source":      opp.revenue_source,
                    "confidence":          opp.confidence,
                    "last_alerted":        now,
                    "alert_count":         prev.get("alert_count", 1) + 1,
                })
                logger.info(
                    f"SIGNIFICANT: '{opp.book.title[:50]}' — {reason}"
                )
            else:
                # Suppress — update values silently
                suppressed.append(opp)
                state[isbn].update({
                    "profit":           opp.profit,
                    "roi":              roi,
                    "revenue_estimate": opp.revenue_estimate,
                    "revenue_source":   opp.revenue_source,
                    "confidence":       opp.confidence,
                })
                logger.debug(
                    f"SUPPRESSED: '{opp.book.title[:50]}' "
                    f"profit=${opp.profit:.2f} (no significant gain)"
                )

    _save_state(state)

    logger.info(
        f"Classification: {len(new_opps)} new | "
        f"{len(significant)} significant | "
        f"{len(suppressed)} suppressed"
    )
    return new_opps, significant, suppressed


# ── Daily summary gate ────────────────────────────────────────────────────────

def should_send_daily_summary() -> bool:
    """
    Return True if the daily summary should be sent this run.

    Conditions:
        - DAILY_SUMMARY_ENABLED = True
        - Current UTC hour >= DAILY_SUMMARY_HOUR
        - Today's date != last_daily_summary_date in state file
    """
    if not config.DAILY_SUMMARY_ENABLED:
        return False

    if _current_hour_utc() < config.DAILY_SUMMARY_HOUR:
        return False

    state = _load_state()
    last  = state["_meta"].get("last_daily_summary_date", "")
    today = _today_str()

    if last == today:
        logger.debug(f"Daily summary already sent today ({today}) — skipping")
        return False

    logger.info(f"Daily summary due (last sent: {last or 'never'}, today: {today})")
    return True


def mark_daily_summary_sent() -> None:
    """Record that today's daily summary was sent. Call after successful send."""
    state = _load_state()
    state["_meta"]["last_daily_summary_date"] = _today_str()
    _save_state(state)
    logger.info(f"Daily summary date recorded: {_today_str()}")


# ── Utilities ─────────────────────────────────────────────────────────────────

def get_state_summary() -> dict:
    """Return basic stats about the current state file."""
    state = _load_state()
    opps  = {k: v for k, v in state.items() if k != "_meta"}
    return {
        "total_tracked":        len(opps),
        "state_file":           config.STATE_FILE_PATH,
        "tracking_enabled":     config.STATE_TRACKING_ENABLED,
        "last_daily_summary":   state["_meta"].get("last_daily_summary_date", "never"),
    }


def clear_state() -> None:
    """
    Wipe the state file. Use this to re-alert on all known opportunities,
    e.g. after changing thresholds or deploying to a new environment.
    """
    path = Path(config.STATE_FILE_PATH)
    if path.exists():
        path.unlink()
        logger.info(f"State file cleared: '{path}'")
    else:
        logger.info("No state file to clear")
