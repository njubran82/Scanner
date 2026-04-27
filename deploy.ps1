# deploy.ps1 — Copy new files into the Scanner repo, run blocklist update, push everything.
# Run from anywhere. Adjust $RepoDir if your repo is in a different location.

$RepoDir = "E:\Book\Scanner"
$DownloadDir = "E:\Book\Scanner"

Set-Location $RepoDir

# ── 1. Copy Python + JSON files to repo root ─────────────────
$RootFiles = @(
    "ship_deadline_guard.py",
    "weekly_order_report.py",
    "guard_state.json",
    "fulfill_toggle.json",
    "add_blocklist.py"
)

foreach ($file in $RootFiles) {
    $src = Join-Path $DownloadDir $file
    if (Test-Path $src) {
        Copy-Item $src $RepoDir -Force
        Write-Host "Copied: $file"
    } else {
        Write-Warning "Not found: $src — skipping"
    }
}

# ── 2. Copy workflow YMLs to .github/workflows/ ──────────────
$WorkflowDir = Join-Path $RepoDir ".github\workflows"
New-Item -ItemType Directory -Force -Path $WorkflowDir | Out-Null

$WorkflowFiles = @(
    "ship_guard.yml",
    "toggle_fulfillment.yml",
    "weekly_order_report.yml"
)

foreach ($file in $WorkflowFiles) {
    $src = Join-Path $DownloadDir $file
    if (Test-Path $src) {
        Copy-Item $src $WorkflowDir -Force
        Write-Host "Copied workflow: $file"
    } else {
        Write-Warning "Not found: $src — skipping"
    }
}

# ── 3. Run blocklist update ───────────────────────────────────
Write-Host "`nRunning blocklist update..."
python add_blocklist.py

# ── 4. Commit and push ───────────────────────────────────────
Write-Host "`nCommitting..."
git add -A
git commit -m "Add deadline guard, order report, fulfillment toggle, blocklist update"
git push

Write-Host "`nDone. Check GitHub Actions tab to confirm workflows appear."
