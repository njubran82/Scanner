# ============================================================
# sync_and_list.py
# Downloads the latest scanner_results.csv from GitHub,
# then automatically runs lister.py to list new opportunities.
#
# HOW TO RUN:
#   python sync_and_list.py
#
# RECOMMENDED: Schedule in Windows Task Scheduler to run
# a few hours after your scanner runs on Monday mornings.
# Scanner runs at 8:00 AM UTC (Monday) = 3:00 AM EST
# Run this at 5:00 AM EST to give scanner time to finish.
# ============================================================

import os
import sys
import requests
import shutil
from datetime import datetime

# ----------------------------------------------------------
# CONFIG
# ----------------------------------------------------------

# Your GitHub raw CSV URL
# Format: https://raw.githubusercontent.com/{user}/{repo}/main/scanner_results.csv
GITHUB_CSV_URL = "https://raw.githubusercontent.com/njubran82/BooksGOAT-Scanner/main/scanner_results.csv"

# Where to save the downloaded CSV
LOCAL_CSV_PATH = r"E:\Book\Lister\scanner_results.csv"

# Backup folder for previous scan results
BACKUP_FOLDER = r"E:\Book\Lister\scan_history"

# Path to lister script
LISTER_SCRIPT = r"E:\Book\Lister\lister.py"

# Log file
SYNC_LOG = r"E:\Book\Lister\sync_log.txt"


# ----------------------------------------------------------
# HELPER: Write to log
# ----------------------------------------------------------

def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    with open(SYNC_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------
# STEP 1: Download latest scanner_results.csv from GitHub
# ----------------------------------------------------------

def download_csv():
    log("Downloading scanner_results.csv from GitHub...")

    try:
        response = requests.get(GITHUB_CSV_URL, timeout=30)

        if response.status_code == 404:
            log("❌ File not found on GitHub. Has the scanner run yet?")
            return False

        if response.status_code != 200:
            log(f"❌ Download failed (HTTP {response.status_code})")
            return False

        content = response.text

        # Sanity check — make sure it looks like a real CSV
        if "ISBN" not in content and "isbn" not in content:
            log("❌ Downloaded file doesn't look like a scanner CSV. Aborting.")
            return False

        # Back up existing CSV if it exists
        if os.path.exists(LOCAL_CSV_PATH):
            os.makedirs(BACKUP_FOLDER, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(BACKUP_FOLDER, f"scanner_results_{timestamp}.csv")
            shutil.copy(LOCAL_CSV_PATH, backup_path)
            log(f"📦 Previous CSV backed up to: {backup_path}")

        # Save new CSV
        with open(LOCAL_CSV_PATH, "w", encoding="utf-8") as f:
            f.write(content)

        lines = content.strip().split("\n")
        log(f"✅ Downloaded {len(lines) - 1} rows (excluding header).")
        return True

    except Exception as e:
        log(f"❌ Exception during download: {e}")
        return False


# ----------------------------------------------------------
# STEP 2: Run lister.py
# ----------------------------------------------------------

def run_lister():
    log("Running lister.py...")

    if not os.path.exists(LISTER_SCRIPT):
        log(f"❌ lister.py not found at: {LISTER_SCRIPT}")
        return False

    # Run lister.py as a subprocess
    import subprocess
    result = subprocess.run(
        [sys.executable, LISTER_SCRIPT],
        cwd=os.path.dirname(LISTER_SCRIPT),
        capture_output=True,
        text=True
    )

    # Print lister output
    if result.stdout:
        for line in result.stdout.strip().split("\n"):
            log(f"  [lister] {line}")

    if result.returncode != 0:
        log(f"❌ lister.py exited with error code {result.returncode}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                log(f"  [lister error] {line}")
        return False

    log("✅ lister.py completed successfully.")
    return True


# ----------------------------------------------------------
# MAIN
# ----------------------------------------------------------

def main():
    log("=" * 60)
    log("sync_and_list.py starting")
    log("=" * 60)

    # Step 1: Download CSV
    if not download_csv():
        log("❌ Sync failed. Lister will not run.")
        return

    # Step 2: Run lister
    run_lister()

    log("=" * 60)
    log("sync_and_list.py done")
    log("=" * 60)


if __name__ == "__main__":
    main()
