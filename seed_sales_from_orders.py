#!/usr/bin/env python3
"""
seed_sales_from_orders.py
Seeds sales_count in protection.db from confirmed order history.
Counts UNITS sold, not number of orders.
"""

import sqlite3, logging
from pathlib import Path

DB_PATH = Path(r'E:\Book\Scanner\protection.db')
LOG_FILE = 'seed_sales.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# UNIT counts from order history (shipped/delivered only, excl. canceled/refunded)
# Notes on multi-unit orders:
#   Nail Tech:    Apr 10 = 2 units, Apr 14 = 15 units → 17 total
#   Barbering:    Apr 14 combined order = 5 units
#   Cosmetology:  Apr 14 combined order = 5 units
#   NIMS:         Apr 15 = 7 units in one order ($744.45 / ~$106 = 7)
#   Sterile:      Apr 16 = 1 unit, Apr 22 = 2 units, listing shows 4 sold → 4 total
#   All others:   All qty 1 per order
CONFIRMED_SALES = {
    # ── MEGA SELLERS ──────────────────────────────────────────────
    '9780521809269': ('Art of Electronics 3rd Ed',                47),  # ~47 orders × 1 unit
    '9781285080475': ('Milady Standard Nail Technology 7th',      17),  # 2 + 15 units
    '9780899705538': ('Guides Permanent Impairment 6th AMA',      10),  # ~10 orders × 1 unit
    '9781579478889': ('Guides Permanent Impairment other ed',      6),  # ~6 orders × 1 unit

    # ── HIGH VELOCITY ─────────────────────────────────────────────
    '9780763781873': ('NIMS 2nd Ed',                               7),  # 1 order × 7 units
    '9781305100558': ('Milady Standard Barbering 6th',             5),  # 1 order × 5 units
    '9781285769417': ('Milady Standard Cosmetology 2016',          5),  # 1 order × 5 units

    # ── MODERATE ──────────────────────────────────────────────────
    '9781517912482': ('Interpreting the MMPI-3',                   4),  # 4 orders × 1 unit
    '9798350705218': ('Sterile Processing CRCST 9th Ed',           4),  # listing shows 4 sold (1+2+1)
    '9781560915263': ('Race Car Vehicle Dynamics',                 3),  # 3 orders × 1 unit
    '9781641950565': ('ACI 318-19 Building Code Requirements',     3),  # 3 orders × 1 unit

    # ── DOUBLE SALES ──────────────────────────────────────────────
    '9781951058067': ('ASQ Certified Manager of Quality',          2),  # 2 orders × 1 unit
    '9781483343723': ('Sexuality Counseling',                      2),  # 2 orders × 1 unit

    # ── SINGLE CONFIRMED SALES ────────────────────────────────────
    '9781439840955': ('Radiation Detection and Measurement',       1),
    '9781683674405': ("Larone's Medically Important Fungi",        1),
    '9781118431436': ("Brown's Boundary Control 7th",              1),
    '9780071769679': ('Atlas Ultrasound Musculoskeletal',          1),
    '9781735141640': ('The Behavior Operations Manual',            1),
    '9780867157994': ('Zero Bone Loss Concepts',                   1),
    '9781492592006': ('Science Practice of Strength Training 3rd', 1),
    '9781591268468': ('PPI PE Structural Reference Manual 10th',   1),
    '9781118909508': ('Architectural Graphic Standards 12th',      1),
    '9781544391250': ('Constitutional Law for a Changing America', 1),
    '9781498754415': ('Essentials Clinical Anatomy Equine',        1),
}


def qty_for_sales(n: int) -> int:
    if n >= 30: return 100
    if n >= 10: return 60
    if n >= 5:  return 40
    if n >= 1:  return 30
    return 20


def run():
    if not DB_PATH.exists():
        log.error(f'DB not found at {DB_PATH} — run protection_patch.py first')
        return

    conn = sqlite3.connect(DB_PATH)

    try:
        conn.execute('ALTER TABLE book_protection ADD COLUMN sales_count INTEGER DEFAULT 0')
        conn.commit()
        log.info('Added sales_count column')
    except sqlite3.OperationalError:
        pass

    log.info(f'Seeding {len(CONFIRMED_SALES)} ISBNs')
    updated = inserted = 0

    for isbn, (title, count) in sorted(CONFIRMED_SALES.items()):
        target_qty = qty_for_sales(count)
        existing = conn.execute(
            'SELECT sales_count FROM book_protection WHERE isbn = ?', (isbn,)
        ).fetchone()

        if existing:
            old = existing[0]
            if count > old:
                conn.execute(
                    'UPDATE book_protection SET sales_count = ? WHERE isbn = ?',
                    (count, isbn)
                )
                log.info(f'  UPDATE {isbn} | {title[:45]:<45} | {old} → {count} units → qty {target_qty}')
                updated += 1
            else:
                log.info(f'  SKIP   {isbn} | {title[:45]:<45} | already {old} (new={count})')
        else:
            conn.execute(
                'INSERT INTO book_protection (isbn, sales_count, protected) VALUES (?, ?, ?)',
                (isbn, count, 1 if count >= 3 else 0)
            )
            log.info(f'  INSERT {isbn} | {title[:45]:<45} | {count} units → qty {target_qty}')
            inserted += 1

    conn.commit()
    conn.close()

    log.info(f'Done: {updated} updated | {inserted} inserted')
    log.info('Now run: python update_quantities.py')

    print('\n=== QUANTITY TIERS FROM ORDER HISTORY ===')
    print(f'{"ISBN":<15} {"Units":>6} {"Qty":>5}  Title')
    print('-' * 70)
    for isbn, (title, count) in sorted(CONFIRMED_SALES.items(), key=lambda x: -x[1][1]):
        print(f'{isbn:<15} {count:>6} {qty_for_sales(count):>5}  {title[:40]}')


if __name__ == '__main__':
    run()
