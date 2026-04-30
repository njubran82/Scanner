# New Components — Deployment Guide

## Files to add to njubran82/Scanner repo

### Root directory (alongside existing scripts)
| File | Purpose |
|---|---|
| `ship_deadline_guard.py` | Emergency mark-shipped before eBay deadline |
| `weekly_order_report.py` | Weekly email report with order sources |
| `guard_state.json` | Persists which orders the guard already handled |
| `fulfill_toggle.json` | On/off state for auto-fulfillment (default: OFF) |

### .github/workflows/ directory
| File | Purpose |
|---|---|
| `ship_guard.yml` | Runs deadline guard every 6 hours |
| `toggle_fulfillment.yml` | Manual toggle for auto-fulfillment |
| `weekly_order_report.yml` | Weekly report every Monday 12 PM EST |

## How to deploy

```bash
cd E:\Book\Scanner
# Copy the .py and .json files to repo root
# Copy the .yml files to .github/workflows/
git add .
git commit -m "Add deadline guard, order report, fulfillment toggle"
git push
```

## How to use the toggle

**Turn ON:**
GitHub → Actions → "Toggle Auto-Fulfillment" → Run workflow → select `true`

**Turn OFF:**
Same → select `false`

**Or via CLI:**
```bash
gh workflow run "Toggle Auto-Fulfillment" -f enabled=true
gh workflow run "Toggle Auto-Fulfillment" -f enabled=false
```

Default is OFF. When OFF, the deadline guard skips (unless triggered manually).

## Required secrets (already configured)
- `EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_REFRESH_TOKEN`
- `SMTP_USER`, `SMTP_PASSWORD`
- `BOOKSGOAT_CSV_URL` (for order source report)

## Reminder: sell.fulfillment scope
The deadline guard needs `sell.fulfillment` scope on your refresh token.
Your shipping_tracker.py already posts tracking successfully, so the scope
may already be present. If the guard fails with `insufficient_scope`,
regenerate the token with sell.fulfillment enabled.
