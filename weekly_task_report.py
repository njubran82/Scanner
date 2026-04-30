#!/usr/bin/env python3
"""
weekly_task_report.py — Weekly backlog + actionable items report
Runs: GitHub Actions weekly_task_report.yml (Monday 8AM EST)

Sends a structured email covering:
  1. Books needing manual attention in Seller Hub
  2. Outstanding code/pipeline tasks
  3. Completed items (for reference)

Edit the TASKS list below to add/remove/complete items.
"""

import os, smtplib
from datetime import datetime
from email.mime.text import MIMEText

SMTP_HOST     = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT     = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER     = os.environ['SMTP_USER']
SMTP_PASSWORD = os.environ['SMTP_PASSWORD']
EMAIL_FROM    = os.environ.get('EMAIL_FROM', SMTP_USER)
EMAIL_TO      = os.environ.get('EMAIL_TO', SMTP_USER)

# ════════════════════════════════════════════════════════════════
# TASK LIST — edit this section to manage the backlog
# Format: (priority, category, description)
# Priority: HIGH / MEDIUM / LOW
# To mark complete: move to COMPLETED list at bottom
# ════════════════════════════════════════════════════════════════

TASKS = [

    # ── SELLER HUB — MANUAL ACTIONS ───────────────────────────────────────

    ("HIGH", "Seller Hub — Photos",
     "Upload photos for these 42 no-photo auto-listed books. "
     "Search by ISBN in Seller Hub > Listings > Active."),

    ("HIGH", "Seller Hub — Photos",
     "9781138852976 — Research Methods in Applied Settings"),

    ("HIGH", "Seller Hub — Photos",
     "9780691121376 — Asset Pricing: Revised Edition"),

    ("HIGH", "Seller Hub — Photos",
     "9781138292406 — Death Society and Human Experience 12e"),

    ("HIGH", "Seller Hub — Photos",
     "9783662631225 — Inborn Metabolic Diseases"),

    ("HIGH", "Seller Hub — Photos",
     "9780190925697 — Understanding Human Communication"),

    ("HIGH", "Seller Hub — Photos",
     "9781119853510 — Hepatology and Transplant Hepatology"),

    ("HIGH", "Seller Hub — Photos",
     "9781119337249 — Blackwell's Five-Minute Veterinary"),

    ("HIGH", "Seller Hub — Photos",
     "9780199329007 — Gardner and Sutherland's Chromosome Abnormalities"),

    ("HIGH", "Seller Hub — Photos",
     "9781884989117 — Spacecraft Thermal Control Handbook"),

    ("HIGH", "Seller Hub — Photos",
     "9781617316203 — Exploring Anatomy & Physiology in the Lab"),

    ("HIGH", "Seller Hub — Photos",
     "9781108965910 — Core Radiology: A Visual Approach"),

    ("HIGH", "Seller Hub — Photos",
     "9781119460985 — Point-of-Care Ultrasound Techniques"),

    ("HIGH", "Seller Hub — Photos",
     "9780071838931 — Hadzic's Peripheral Nerve Blocks"),

    ("HIGH", "Seller Hub — Photos",
     "9781506307886 — Evaluation: A Systematic Approach 8e"),

    ("HIGH", "Seller Hub — Photos",
     "9781640162907 — ICD-10-CM 2024 Complete Official Codebook"),

    ("HIGH", "Seller Hub — Photos",
     "9780134405506 — Construction Estimating Using Excel"),

    ("HIGH", "Seller Hub — Photos",
     "9783030183738 — Ultrasound for Interventional Pain Management"),

    ("HIGH", "Seller Hub — Photos",
     "9781556204135 — A Contemporary Approach to Substance Use Disorders"),

    ("HIGH", "Seller Hub — Photos",
     "9780198836247 — The Library of Paradise"),

    ("HIGH", "Seller Hub — Photos",
     "9780198526629 — Decision Modelling for Health Economic Evaluation"),

    ("HIGH", "Seller Hub — Photos",
     "9780198843061 — The Oxford Handbook of Foreign Policy Analysis"),

    ("HIGH", "Seller Hub — Photos",
     "9781107010802 — Quantum Effects in Biology"),

    ("HIGH", "Seller Hub — Photos",
     "9780367754907 — Conservation of Books"),

    ("HIGH", "Seller Hub — Photos",
     "9781556204166 — DSM-5-TR Learning Companion for Counselors"),

    ("HIGH", "Seller Hub — Photos",
     "9781119683810 — Atlas of Operative Oral and Maxillofacial Surgery"),

    ("HIGH", "Seller Hub — Photos",
     "9781119793595 — Practical Early Orthodontic Treatment"),

    ("HIGH", "Seller Hub — Photos",
     "9781071817179 — Tests & Measurement for People Who (Think They) Hate Testing"),

    ("HIGH", "Seller Hub — Photos",
     "9780323376518 — Small Animal Dermatology: A Color Atlas"),

    ("HIGH", "Seller Hub — Photos",
     "9783031101342 — Handbook of Abductive Cognition"),

    ("HIGH", "Seller Hub — Photos",
     "9780197622223 — The Border Between Seeing and Thinking"),

    ("HIGH", "Seller Hub — Photos",
     "9780521545662 — Lisp in Small Pieces"),

    ("HIGH", "Seller Hub — Photos",
     "9780190698614 — Elements of Electromagnetics"),

    ("HIGH", "Seller Hub — Photos",
     "9780831131463 — Reliability-Centered Maintenance"),

    ("HIGH", "Seller Hub — Photos",
     "9780128220474 — Pipe Drafting and Design"),

    ("HIGH", "Seller Hub — Photos",
     "9780195167016 — Character Strengths and Virtues: A Handbook"),

    ("HIGH", "Seller Hub — Photos",
     "9780323697071 — Varcarolis' Foundations of Psychiatric-Mental Health Nursing"),

    ("HIGH", "Seller Hub — Photos",
     "9781032055251 — Flow Cytometry in Neoplastic Hematology"),

    ("HIGH", "Seller Hub — Photos",
     "9781482217377 — Endovascular Skills 4e"),

    ("HIGH", "Seller Hub — Photos",
     "9780367642037 — Textbook of Palliative Medicine and Supportive Care"),

    ("HIGH", "Seller Hub — Photos",
     "9783031234873 — A Clinician's Pearls & Myths in Rheumatology"),

    ("HIGH", "Seller Hub — Photos",
     "9781439887332 — Linear Models with R 2e"),

    ("HIGH", "Seller Hub — Photos",
     "9780470189306 — Electromagnetic Compatibility Engineering"),

    ("HIGH", "Seller Hub — Manual Listings — Photos",
     "Upload photos for these 11 original manual listings: "
     "Safety Pro, ASQ QE, Ketogenic, AI Endgame, Bayesian, "
     "Exposure/Response, Qualitative Research, Lange Q&A, "
     "Strategies & Tactics, Women's Reproductive, Behavior Analysis"),

    ("HIGH", "Seller Hub — Dead Offers",
     "Group B — End + Sell Similar (NOT Relist) for 7 dead offer books: "
     "American Herbal, Functional Occlusion, Clinical Handbook, Arborists, "
     "Developmental Bio, Larone's Fungi, Small Animal Derm"),

    ("HIGH", "Seller Hub — Delist",
     "Safety Professional's Reference — manual listing, profit negative ($-12.38). "
     "Delist manually in Seller Hub."),

    ("HIGH", "Seller Hub — Duplicates",
     "Delist 29 duplicate listings in Seller Hub "
     "(full list in previous session — search for duplicates by title)"),

    # ── PIPELINE / CODE TASKS ──────────────────────────────────────────────

    ("HIGH", "Pipeline — eBay Token",
     "Regenerate eBay refresh token WITH sell.fulfillment scope. "
     "eBay Developer Portal → Profit Scanner app → OAuth Scopes → "
     "enable sell.fulfillment → regenerate → update GitHub secret "
     "EBAY_REFRESH_TOKEN and Windows env var. "
     "Required for order_monitor.py and order_status_report.py to work fully."),

    ("HIGH", "Pipeline — Unmatched Listings",
     "Review E:\\Book\\Lister\\unmatched_listings.csv — 65 listings on eBay "
     "not in booksgoat_enhanced.csv. Determine which to add to CSV or delist."),

    ("HIGH", "Pipeline — 8 No-Inventory Listings",
     "8 ISBNs in CSV have no inventory item on eBay (found by sync_manual_listings.py). "
     "These cannot be managed via API. For each: "
     "End listing in Seller Hub → recreate with full_publish.py."),

    ("MEDIUM", "Pipeline — Repricer Bug",
     "repricer.py instant-delist cycle is now suppressed via REPRICER_MODE=report_only. "
     "Root cause still undiagnosed. Investigate why profit computes negative for active books. "
     "Likely: wrong cost basis being pulled (verify 5-qty is used), or fee rate mismatch. "
     "Use REPRICER_MODE=dry_run to test fixes before switching back to live."),

    ("MEDIUM", "Pipeline — fix_listings.py if False bug",
     "get_or_create_offer() always forces POST — books with existing offers fail silently "
     "and stay pending. Fix: proper check for existing offer before POST vs PUT. "
     "Workaround: use full_publish.py instead of fix_listings.py for new listings."),

    ("MEDIUM", "Pipeline — Quantity stuck at 10",
     "~150 listings from 04/19 have qty=10 instead of qty=20. "
     "Inventory item PUT failing silently. Fix by bulk-updating those offer quantities."),

    ("MEDIUM", "Pipeline — AI Descriptions missing",
     "24 books published without AI descriptions. "
     "Run fix_listings.py to generate descriptions + republish."),

    ("MEDIUM", "Pipeline — Image Fallback Chain",
     "lister.py image fallback chain misses many ISBNs (Open Library coverage incomplete). "
     "Add Google Books API as primary source and improve fallback order. "
     "Currently causes 'Add at least 1 photo' errors on new listings."),

    ("MEDIUM", "Pipeline — Spec Correction",
     "booksgoat_spec_v5.html still says 'Use 10-qty price as standard cost input'. "
     "Should say 5-qty. Update the spec HTML file."),

    ("MEDIUM", "Pipeline — Ghost Entries",
     "Run audit_offers.py to reconcile ghost entries in CSV vs actual eBay offers."),

    ("LOW", "Pipeline — Second eBay Account",
     "jubran.industries@gmail.com — get eBay approval before building out second account."),

    ("LOW", "Pipeline — 7 Item.Country Books",
     "After fix_listings.py if False bug is resolved: "
     "end manual listings for these 7 books and republish via API."),

    ("LOW", "Pipeline — Blocklist Expansion",
     "Monitor for new min-qty or PDF-only discoveries and add to blocklist. "
     "Current blocklist: 13 ISBNs."),

    ("LOW", "Pipeline — Weekly Scanner Email",
     "weekly_summary.py has duplicate opportunities bug — "
     "parser matches too broadly across full log. Fix to read only SCAN COMPLETE block."),

]

# ════════════════════════════════════════════════════════════════
# COMPLETED — move items here when done (kept for reference)
# ════════════════════════════════════════════════════════════════

COMPLETED = [
    "On Sale carousel integrated as Source 3 in booksgoat_scraper.py",
    "Scraper auto-pushes CSV to GitHub after Monday run",
    "Tracker auto-pushes CSV to GitHub after daily run",
    "Scanner v3 reads full 417-book CSV (all three sources)",
    "Scanner 5-qty cost basis fixed",
    "Repricer decoupled — REPRICER_MODE=report_only deployed",
    "Daily order status report deployed (order_status_report.py)",
    "sync_manual_listings.py run — 409 offer_ids populated in CSV",
    "Back Mechanic (9780973501827) added to scanner + repricer blocklist",
    "Understanding Behaviorism (9781119143642) added to blocklist",
    "Art and Science of Technical Analysis (9781118115121) added to blocklist",
    "C Programming Modern Approach (9780393979503) added to blocklist",
    "Task C ISBNs all blocklisted",
    "CSV pushed to GitHub — scraper/tracker/scanner now share single source of truth",
]


# ════════════════════════════════════════════════════════════════
# REPORT BUILDER
# ════════════════════════════════════════════════════════════════

def build_report():
    now = datetime.now()
    lines = [
        f"ATLAS COMMERCE — WEEKLY TASK REPORT",
        f"Generated: {now.strftime('%A, %B %d, %Y at %I:%M %p')}",
        "=" * 65,
        "",
    ]

    # Group by priority then category
    for priority in ("HIGH", "MEDIUM", "LOW"):
        priority_tasks = [(cat, desc) for (p, cat, desc) in TASKS if p == priority]
        if not priority_tasks:
            continue

        emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}[priority]
        lines.append(f"{emoji} {priority} PRIORITY ({len(priority_tasks)} items)")
        lines.append("─" * 65)

        # Group by category
        by_cat = {}
        for cat, desc in priority_tasks:
            by_cat.setdefault(cat, []).append(desc)

        for cat, descs in by_cat.items():
            lines.append(f"\n  [{cat}]")
            for desc in descs:
                # Wrap long descriptions
                lines.append(f"  • {desc}")
        lines.append("")

    # Completed section
    lines += [
        "=" * 65,
        f"✅ RECENTLY COMPLETED ({len(COMPLETED)} items)",
        "─" * 65,
    ]
    for item in COMPLETED:
        lines.append(f"  ✓ {item}")

    lines += [
        "",
        "=" * 65,
        "To update this list: edit TASKS in weekly_task_report.py",
        "then git commit + push to E:\\Book\\Scanner\\",
        "=" * 65,
    ]

    return "\n".join(lines)


def send_report():
    body    = build_report()
    subject = (
        f"📋 Atlas Commerce Weekly Tasks — "
        f"{sum(1 for p,_,_ in TASKS if p=='HIGH')} high, "
        f"{sum(1 for p,_,_ in TASKS if p=='MEDIUM')} medium, "
        f"{sum(1 for p,_,_ in TASKS if p=='LOW')} low — "
        f"{datetime.now().strftime('%b %d')}"
    )
    msg = MIMEText(body, 'plain')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    print(f"Report sent: {subject}")
    print(f"Tasks: {len(TASKS)} open | {len(COMPLETED)} completed")


if __name__ == '__main__':
    send_report()
