"""
run_scanner.py — CLI entry point.

USAGE:
    python run_scanner.py                        # Single scan, live URL
    python run_scanner.py --schedule             # Repeating scan on cron schedule
    python run_scanner.py --csv path/to/file.csv # Override: use local CSV
    python run_scanner.py --min-profit 15        # Override profit threshold
    python run_scanner.py --min-margin 0.15      # Override margin threshold (15%)
    python run_scanner.py --no-email             # Disable email for this run
    python run_scanner.py --no-sms               # Disable SMS for this run
    python run_scanner.py --clear-state          # Wipe state file, re-alert everything
    python run_scanner.py --state-info           # Show state file stats and exit

SCHEDULING:
    --schedule runs the scan once immediately, then repeats on config.SCHEDULER_CRON.
    Default: every 6 hours. For production, consider Supervisor or systemd.

WEEKLY REFRESH MODEL:
    The supplier sheet updates every Sunday. This scanner fetches it live
    on every run. Running more frequently than weekly is fine — state tracking
    suppresses duplicate alerts between sheet updates.
"""

import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/scanner.log", encoding="utf-8"),
        ],
    )


def parse_args():
    p = argparse.ArgumentParser(
        description="BooksGoat → eBay Arbitrage Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--schedule",     action="store_true",
                   help="Run on repeating schedule (config.SCHEDULER_CRON)")
    p.add_argument("--csv",          type=str,
                   help="Use a local CSV file instead of the live URL")
    p.add_argument("--min-profit",   type=float,
                   help="Override MIN_PROFIT threshold")
    p.add_argument("--min-margin",   type=float,
                   help="Override MIN_MARGIN as decimal (e.g. 0.15 = 15%%)")
    p.add_argument("--no-email",     action="store_true",
                   help="Disable email for this run")
    p.add_argument("--no-sms",       action="store_true",
                   help="Disable SMS for this run")
    p.add_argument("--clear-state",  action="store_true",
                   help="Clear the state file before scanning (re-alert on all)")
    p.add_argument("--state-info",   action="store_true",
                   help="Show state file stats and exit")
    p.add_argument("--no-state",     action="store_true",
                   help="Disable state tracking for this run (treat all as new)")
    return p.parse_args()


def apply_overrides(args):
    if args.csv:
        config.SUPPLIER = "csv"
        config.CSV_FALLBACK_PATH = args.csv
        print(f"→ Using local CSV: {args.csv}")
    if args.min_profit is not None:
        config.MIN_PROFIT = args.min_profit
        print(f"→ Min profit override: ${args.min_profit:.2f}")
    if args.min_margin is not None:
        config.MIN_MARGIN = args.min_margin
        print(f"→ Min margin override: {args.min_margin*100:.0f}%")
    if args.no_email:
        config.EMAIL_ENABLED = False
        print("→ Email disabled for this run")
    if args.no_sms:
        config.SMS_ENABLED = False
        print("→ SMS disabled for this run")
    if args.no_state:
        config.STATE_TRACKING_ENABLED = False
        print("→ State tracking disabled for this run")


def run_scheduled():
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        print("APScheduler not installed. Run: pip install apscheduler")
        sys.exit(1)

    from scanner import run_scan

    print(f"\nScheduler starting. Cron: '{config.SCHEDULER_CRON}'")
    print("Running first scan immediately...\n")
    run_scan()

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_scan,
        trigger = CronTrigger.from_crontab(config.SCHEDULER_CRON),
        id      = "book_scanner",
        misfire_grace_time = 300,
    )
    print(f"\nNext scheduled run: {scheduler.get_jobs()[0].next_run_time}")
    print("Press Ctrl+C to stop.\n")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler stopped.")
        scheduler.shutdown()


def main():
    setup_logging()
    args = parse_args()

    # Handle info/utility flags that exit early
    if args.state_info:
        from state_tracker import get_state_summary
        info = get_state_summary()
        print(f"\nState file: {info['state_file']}")
        print(f"Tracking enabled: {info['tracking_enabled']}")
        print(f"Tracked opportunities: {info['total_tracked']}")
        return

    if args.clear_state:
        from state_tracker import clear_state
        clear_state()
        print("State cleared. All opportunities will re-alert on next scan.")

    apply_overrides(args)

    if args.schedule:
        run_scheduled()
    else:
        from scanner import run_scan
        run_scan()


if __name__ == "__main__":
    main()
