#!/usr/bin/env python3
"""
weekly_task_report.py — Weekly backlog report for Atlas Commerce / atlas_commerce
Runs: GitHub Actions weekly_task_report.yml (Monday 8AM EST)

To update tasks: edit TASKS list below, commit and push.
To mark complete: move entry from TASKS to COMPLETED.
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

TASKS = [

    # HIGH ─────────────────────────────────────────────────────

    ("HIGH", "Regenerate eBay refresh token (sell.fulfillment scope)", [
        "Go to developer.ebay.com and sign in",
        "Click your name > Application Keys",
        "Find 'Profit Scanner' app > click 'User Tokens'",
        "Enable 'sell.fulfillment' scope > click 'Generate Token'",
        "Copy the new refresh token",
        "Go to github.com/njubran82/Scanner > Settings > Secrets > Actions",
        "Update secret EBAY_REFRESH_TOKEN with the new token",
        "On Windows: System Properties > Environment Variables > update EBAY_REFRESH_TOKEN",
        "REQUIRED FOR: order_monitor.py, order_status_report.py, shipping_tracker.py",
    ]),

    ("HIGH", "Delist 11 confirmed duplicate listings in Seller Hub", [
        "Go to ebay.com/sh/lst/active (Seller Hub > Listings > Active)",
        "Search each listing ID in the search box > click Actions > End listing",
        "Listings to delist (the system already has API-managed versions of these):",
        "  358316092497 — Art of Reading Buildings $90",
        "  358358966745 — Low Pressure Boilers $90 (system has it at $79.20)",
        "  358487111163 — Effective Supervisory Practices",
        "  358487084447 — PPI NCIDQ Practicum Mock Exam",
        "  358487112896 — PPI ARE 5.0 Exam Review $155.99 (system has lower price)",
        "  358487080706 — PPI ARE 5.0 Practice Questions",
        "  358487111617 — Character Strengths and Virtues",
        "  358487108223 — Conservation of Books",
        "  358487086405 — Evaluation: A Systematic Approach",
        "  358487119966 — Milady Standard Barbering $103.99 (keep the $202.35 version)",
        "  358411686534 — Trading and Exchanges $100 (duplicate of 358487112756)",
    ]),

    ("HIGH", "Keep 4 catalog-conflict listings — no action needed", [
        "These 4 listings CANNOT be managed by the system via API.",
        "Reason: eBay error 25002 — their ISBNs match multiple catalog entries.",
        "Seller Hub can manage them because it has a manual catalog picker the API lacks.",
        "DO NOT end these listings. Reprice manually in Seller Hub when needed.",
        "  358468899018 — ASQ Certified Manager (ISBN 9781951058067) $91.33",
        "  358486867902 — ASQ Certified Quality Auditor (ISBN 9781951058098) $104.68",
        "  358494469700 — Concrete Manual (ISBN 9781609836184) $175.74",
        "  358487111210 — National Incident Management System (ISBN 9780763781873) $77.99",
    ]),

    ("HIGH", "Fix 7 dead-offer listings — End + Sell Similar in Seller Hub", [
        "These listings have broken offers — the system cannot manage them.",
        "DO NOT use Relist — it carries the broken offer forward. Use Sell Similar.",
        "Steps for each book:",
        "  1. Seller Hub > Listings > Active > search by title",
        "  2. Actions > End listing",
        "  3. Seller Hub > Listings > Ended > find the book",
        "  4. Actions > Sell Similar > verify price > publish",
        "Books to fix:",
        "  American Herbal Products Association's Botanical Safety Handbook",
        "  Functional Occlusion: From TMJ to Smile Design",
        "  Clinical Handbook of Psychological Disorders",
        "  Arborists' Certification Study Guide",
        "  Developmental Biology",
        "  Larone's Medically Important Fungi",
        "  Small Animal Dermatology",
    ]),

    ("HIGH", "Add ~44 manual listings to the system", [
        "These listings are live on eBay but invisible to the repricer and tracker.",
        "They will never be automatically repriced or delisted until this is done.",
        "For EACH listing ID below, follow these steps:",
        "  1. Go to ebay.com/itm/LISTING_ID to open the listing",
        "  2. Find the ISBN-13 (in the description or title)",
        "  3. Find the book on booksgoat.com — note the price and copy the URL",
        "  4. Open E:\\Book\\Lister\\booksgoat_enhanced.csv in Excel or Notepad",
        "  5. Add a new row with these fields:",
        "       isbn13 = 13-digit ISBN",
        "       title = book title",
        "       cost = BooksGoat 5-qty price",
        "       product_url = BooksGoat product page URL",
        "       sell_price = current eBay listing price",
        "       status = active",
        "       (all other fields leave blank)",
        "  6. After all rows are added, open PowerShell and run:",
        "       cd E:\\Book\\Lister",
        "       python sync_manual_listings.py",
        "This populates offer_id for each book, bringing them under system control.",
        "",
        "Listing IDs to process:",
        "  358479807490 — Sterile Processing Technical Manual",
        "  358494470035 — Fire and Emergency Services Instructor 9e",
        "  358487083343 — Strategies and Tactics of Behavioral Research",
        "  358487066225 — PPI PE Structural 16-Hour Practice Exam",
        "  358337561039 — Guides to Evaluation of Permanent Impairment (AMA 6e)",
        "  358338556330 — Interpreting the MMPI-3",
        "  358316213300 — Introduction to Thermodynamics of Materials",
        "  358486867980 — Introduction to Electrodynamics (Griffiths)",
        "  358487112756 — Trading and Exchanges",
        "  358487106377 — The Five Dysfunctions of a Team",
        "  358466900977 — Technical Analysis Using Multiple Timeframes",
        "  358487075961 — Roark's Formulas for Stress and Strain 9e",
        "  358487105949 — Notes from the Grooming Table 2e",
        "  358487119060 — Introduction to Unmanned Aircraft Systems",
        "  358487090837 — Practical Portfolio Performance Measurement",
        "  358487077853 — Assessment in Counseling",
        "  358487095798 — Understanding Ultrasound Physics 4e",
        "  358487095609 — Classical Mechanics (Taylor)",
        "  358487075378 — Field Theory of Non-Equilibrium Systems 2e",
        "  358494466983 — Zero Bone Loss Concepts",
        "  358487106468 — Japanese Candlestick Charting Techniques 2e",
        "  358487080785 — PPI PE Structural Reference Manual 10e",
        "  358359021087 — NASCLA Contractors Guide 14e",
        "  358487101027 — SAGE Handbook of Qualitative Research 6e",
        "  358487104068 — Conducting Research Literature Reviews",
        "  358487090123 — PPI NCIDQ Interior Design Reference Manual 7e",
        "  358487102651 — Fundamentals of Oil & Gas Accounting 6e",
        "  358487076818 — Introduction to Fourier Optics 4e",
        "  358316146060 — Science and Practice of Strength Training",
        "  358487100150 — The Environmental Case",
        "  358487062931 — Intro to Clinical Mental Health Counseling",
        "  358487073702 — Sexuality Counseling: Theory Research Practice",
        "  358359030538 — Fundamentals of Surveying Practice Exam",
        "  358355648507 — Distressed Debt Analysis",
        "  358355920669 — Handbook of Bird Biology 3e",
        "  358487089609 — Infants and Children: Prenatal Through Middle Childhood",
        "  358487105824 — Writing Literature Reviews 7e",
        "  358466886680 — 2018 International Building Code",
        "  358355879237 — DC:0-5 Diagnostic Classification",
        "  358365168759 — Art of Electronics 3e",
        "  358358998471 — Qualitative Inquiry and Research Design",
        "  358355599711 — Real Estate Development 5e",
        "  358179340482 — Anatomical Chart: Diseases and Disorders 2e",
        "  358485889260 — Introduction to Thermodynamics of Materials (check if duplicate of 358316213300)",
    ]),

    ("HIGH", "Upload cover photos — 42 auto-listed books with no image", [
        "For each ISBN: Seller Hub > Listings > Active > search ISBN > Edit > add photo > save",
        "Find cover images by searching the ISBN on Google Images or Amazon.",
        "  9781138852976 — Research Methods in Applied Settings",
        "  9780691121376 — Asset Pricing: Revised Edition",
        "  9781138292406 — Death Society and Human Experience 12e",
        "  9783662631225 — Inborn Metabolic Diseases",
        "  9780190925697 — Understanding Human Communication",
        "  9781119853510 — Hepatology and Transplant Hepatology",
        "  9781119337249 — Blackwell's Five-Minute Veterinary",
        "  9780199329007 — Gardner and Sutherland's Chromosome Abnormalities",
        "  9781884989117 — Spacecraft Thermal Control Handbook",
        "  9781617316203 — Exploring Anatomy & Physiology in the Lab",
        "  9781108965910 — Core Radiology: A Visual Approach",
        "  9781119460985 — Point-of-Care Ultrasound Techniques",
        "  9780071838931 — Hadzic's Peripheral Nerve Blocks",
        "  9781506307886 — Evaluation: A Systematic Approach 8e",
        "  9781640162907 — ICD-10-CM 2024 Complete Official Codebook",
        "  9780134405506 — Construction Estimating Using Excel",
        "  9783030183738 — Ultrasound for Interventional Pain Management",
        "  9781556204135 — A Contemporary Approach to Substance Use Disorders",
        "  9780198836247 — The Library of Paradise",
        "  9780198526629 — Decision Modelling for Health Economic Evaluation",
        "  9780198843061 — Oxford Handbook of Foreign Policy Analysis",
        "  9781107010802 — Quantum Effects in Biology",
        "  9780367754907 — Conservation of Books",
        "  9781556204166 — DSM-5-TR Learning Companion for Counselors",
        "  9781119683810 — Atlas of Operative Oral and Maxillofacial Surgery",
        "  9781119793595 — Practical Early Orthodontic Treatment",
        "  9781071817179 — Tests & Measurement for People Who (Think They) Hate Testing",
        "  9780323376518 — Small Animal Dermatology: A Color Atlas",
        "  9783031101342 — Handbook of Abductive Cognition",
        "  9780197622223 — The Border Between Seeing and Thinking",
        "  9780521545662 — Lisp in Small Pieces",
        "  9780190698614 — Elements of Electromagnetics",
        "  9780831131463 — Reliability-Centered Maintenance",
        "  9780128220474 — Pipe Drafting and Design",
        "  9780195167016 — Character Strengths and Virtues: A Handbook",
        "  9780323697071 — Varcarolis' Foundations of Psychiatric-Mental Health Nursing",
        "  9781032055251 — Flow Cytometry in Neoplastic Hematology",
        "  9781482217377 — Endovascular Skills 4e",
        "  9780367642037 — Textbook of Palliative Medicine and Supportive Care",
        "  9783031234873 — A Clinician's Pearls & Myths in Rheumatology",
        "  9781439887332 — Linear Models with R 2e",
        "  9780470189306 — Electromagnetic Compatibility Engineering",
    ]),

    ("HIGH", "Upload cover photos — 11 original manual listings", [
        "Same process: Seller Hub > Listings > Active > search by title > Edit > add photo",
        "  Safety Professional's Reference and Practice",
        "  ASQ Certified Quality Engineer Handbook",
        "  The Ketogenic Bible",
        "  AI Endgame",
        "  Bayesian Data Analysis",
        "  Exposure and Response Prevention for OCD",
        "  Qualitative Research: A Guide to Design and Implementation",
        "  Lange Q&A Surgical Technology",
        "  Strategies and Tactics of Behavioral Research",
        "  Women's Reproductive Mental Health Across the Lifespan",
        "  Behavior Analysis for Lasting Change",
    ]),

    # MEDIUM ───────────────────────────────────────────────────

    ("MEDIUM", "Investigate and fix repricer.py profit calculation bug", [
        "Current state: REPRICER_MODE=report_only — delisting disabled, safe to leave.",
        "The repricer was computing negative profit on profitable books (instant-delist bug).",
        "To investigate without touching live listings:",
        "  1. Go to github.com/njubran82/Scanner > .github/workflows/scanner.yml",
        "  2. Find the repricer step — change REPRICER_MODE to dry_run",
        "  3. Go to Actions tab > Weekly Scanner > Run workflow",
        "  4. Check the repricer email report — verify computed profits look correct",
        "  5. Compare cost values in report vs BooksGoat 5-qty prices",
        "  6. Once logic is confirmed correct, change REPRICER_MODE back to live",
    ]),

    ("MEDIUM", "Fix ~150 listings stuck at quantity 10", [
        "About 150 listings created on 04/19 show qty=10 instead of qty=20.",
        "These were created via force_publish_all.py which had qty=10 hardcoded.",
        "Raise this in the next dev session to have a bulk quantity fix script built.",
    ]),

    ("MEDIUM", "Add AI descriptions to 24 listings", [
        "24 books were published without descriptions — affects eBay search ranking.",
        "To fix:",
        "  1. Open PowerShell",
        "  2. cd E:\\Book\\Lister",
        "  3. python fix_listings.py",
        "Note: fix_listings.py has a known bug with existing offers — monitor the output.",
        "If it errors on most books, raise in next dev session for a targeted fix.",
    ]),

    ("MEDIUM", "Fix image fallback chain — new listings failing with no photo", [
        "New listings frequently fail with 'Add at least 1 photo' error.",
        "Current fallback chain starts with Open Library which has poor coverage.",
        "Raise in next dev session — requires editing lister.py to try Google Books first.",
    ]),

    ("MEDIUM", "Update booksgoat_spec_v5.html — fix cost basis reference", [
        "The spec says 'Use 10-qty price as standard cost input' — should be 5-qty.",
        "  1. Open E:\\Book\\Scanner\\booksgoat_spec_v5.html in Notepad",
        "  2. Find: 'Use 10-qty price as standard cost input'",
        "  3. Replace with: 'Use 5-qty price as standard cost input'",
        "  4. cd E:\\Book\\Scanner",
        "  5. git add booksgoat_spec_v5.html",
        "  6. git commit -m 'spec: correct cost basis to 5-qty'",
        "  7. git push",
    ]),

    ("MEDIUM", "Run audit_offers.py to clear ghost CSV entries", [
        "Ghost entries are CSV rows with status=active and offer_ids that no longer",
        "exist on eBay. They cause silent errors in the repricer and tracker.",
        "  1. cd E:\\Book\\Scanner",
        "  2. python audit_offers.py",
        "  3. Review output — ghost entries will be marked delisted in the CSV",
    ]),

    ("MEDIUM", "Fix weekly_summary.py duplicate entries in pipeline email", [
        "The Monday pipeline email shows the same books listed multiple times.",
        "Raise in next dev session — requires fixing the log parser in weekly_summary.py.",
    ]),

    # LOW ──────────────────────────────────────────────────────

    ("LOW", "Migrate 7 Item.Country books to API management", [
        "7 books were listed manually in Seller Hub due to old API error (now fixed).",
        "When ready:",
        "  1. End each manual listing in Seller Hub",
        "  2. cd E:\\Book\\Lister && python full_publish.py",
        "  3. python sync_manual_listings.py to confirm offer_ids",
        "Do this AFTER fix_listings.py if False bug is resolved.",
    ]),

    ("LOW", "Get eBay approval for second seller account", [
        "Second account planned: jubran.industries@gmail.com",
        "Do NOT build tooling until eBay explicitly approves the second account.",
        "Multiple accounts without approval risks suspension of atlas_commerce.",
        "Contact eBay seller support to request multi-account approval.",
    ]),

    ("LOW", "Expand blocklist as new min-qty or PDF-only books are discovered", [
        "When a BooksGoat order reveals a book requires 5+ minimum units or is PDF-only:",
        "  1. Cancel the eBay order if unfulfillable",
        "  2. Add the ISBN to BLOCKLIST in: scanner.py, repricer.py, full_publish.py",
        "  3. git add the three files > commit > push",
        "Current blocklist: 13 ISBNs",
    ]),

]

COMPLETED = [
    "On Sale carousel integrated as Source 3 in booksgoat_scraper.py (04/29)",
    "Scraper auto-pushes CSV to GitHub after Monday 6AM run (04/29)",
    "Tracker auto-pushes CSV to GitHub after daily 7AM run (04/29)",
    "Scanner v3 reads full 417-book CSV from all three scraping sources (04/29)",
    "Scanner cost basis corrected to 5-qty (04/29)",
    "Repricer decoupled — REPRICER_MODE=report_only stops instant-delist cycle (04/29)",
    "Daily order status report deployed — order_status_report.py (04/29)",
    "sync_manual_listings.py run — 409/417 offer_ids now populated in CSV (04/29)",
    "Back Mechanic ISBN 9780973501827 added to all three blocklists (04/29)",
    "Understanding Behaviorism ISBN 9781119143642 added to blocklist (04/27)",
    "Art and Science of Technical Analysis ISBN 9781118115121 added to blocklist",
    "C Programming: A Modern Approach ISBN 9780393979503 added to blocklist",
    "CSV pushed to GitHub — single source of truth across all pipeline components (04/29)",
    "All 417 ISBNs confirmed to have offer_id populated (04/29)",
    "Safety Professional's Reference confirmed profitable — $89.99 cost / $128 eBay (04/29)",
    "Weekly task report deployed — emails every Monday 8AM EST (04/30)",
]


def build_report():
    now   = datetime.now()
    lines = [
        "ATLAS COMMERCE — WEEKLY TASK REPORT",
        f"Generated: {now.strftime('%A, %B %d, %Y at %I:%M %p')}",
        f"Seller: atlas_commerce  |  github.com/njubran82/Scanner",
        "=" * 65,
        "",
    ]
    counts = {p: sum(1 for t in TASKS if t[0] == p) for p in ("HIGH", "MEDIUM", "LOW")}
    lines.append(
        f"Open: {counts['HIGH']} high  |  {counts['MEDIUM']} medium  |  "
        f"{counts['LOW']} low  |  {len(COMPLETED)} completed"
    )
    lines.append("")

    task_num = 0
    for priority in ("HIGH", "MEDIUM", "LOW"):
        pt = [(title, steps) for (p, title, steps) in TASKS if p == priority]
        if not pt:
            continue
        emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}[priority]
        lines += [f"{emoji} {priority} PRIORITY", "─" * 65, ""]
        for title, steps in pt:
            task_num += 1
            lines.append(f"[{task_num}] {title}")
            step_num = 0
            for step in steps:
                if step == "":
                    lines.append("")
                elif step.startswith("  "):
                    lines.append(f"      {step.strip()}")
                else:
                    step_num += 1
                    lines.append(f"    {step_num}. {step}")
            lines.append("")

    lines += ["=" * 65, f"COMPLETED ({len(COMPLETED)} items)", "─" * 65]
    for item in COMPLETED:
        lines.append(f"  [x] {item}")
    lines += ["", "=" * 65,
              "To update: edit weekly_task_report.py > git commit > git push",
              "=" * 65]
    return "\n".join(lines)


def send_report():
    body   = build_report()
    counts = {p: sum(1 for t in TASKS if t[0] == p) for p in ("HIGH", "MEDIUM", "LOW")}
    subject = (
        f"📋 Atlas Commerce Tasks — "
        f"{counts['HIGH']} high / {counts['MEDIUM']} medium / {counts['LOW']} low — "
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
    print(f"Sent: {subject}")
    print(f"{len(TASKS)} open | {len(COMPLETED)} completed")


if __name__ == '__main__':
    send_report()
