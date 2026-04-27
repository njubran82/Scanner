import csv, os, re
from pathlib import Path
from datetime import datetime

NEW_ISBNS = [
    ("9780415708234", "Healing the Fragmented Selves of Trauma Survivors — min qty 6 on BooksGoat"),
    ("9781591264507", "PPI FE Electrical and Computer Practice Problems — min qty 5 on BooksGoat"),
]

for fname in ['repricer.py', 'scanner.py']:
    path = Path(fname)
    if not path.exists():
        print(f"SKIP {fname}"); continue
    txt = path.read_text(encoding='utf-8')
    added = []
    for isbn, reason in NEW_ISBNS:
        if isbn in txt:
            print(f"{fname}: {isbn} already in blocklist"); continue
        match = re.search(r"(BLOCKLIST\s*=\s*\{[^}]*?)(\})", txt, re.DOTALL)
        if match:
            txt = txt[:match.end(1)] + f"    '{isbn}',  # {reason}\n" + txt[match.end(1):]
            added.append(isbn)
        else:
            print(f"{fname}: BLOCKLIST pattern not found")
    path.write_text(txt, encoding='utf-8')
    if added:
        print(f"{fname}: added {added}")

for csv_path in [r'E:\Book\Lister\booksgoat_enhanced.csv', r'E:\Book\Scanner\booksgoat_enhanced.csv']:
    if not Path(csv_path).exists():
        print(f"SKIP {csv_path}"); continue
    rows = list(csv.DictReader(open(csv_path, encoding='utf-8')))
    changed = 0
    isbns = {isbn for isbn, _ in NEW_ISBNS}
    for row in rows:
        if row.get('isbn13') in isbns and row.get('status') != 'delisted':
            row['status'] = 'delisted'
            row['delisted_at'] = datetime.now().isoformat()
            row['delist_reason'] = 'unavailable'
            changed += 1
    fields = list(dict.fromkeys(k for r in rows for k in r))
    tmp = csv_path + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader(); w.writerows(rows)
    os.replace(tmp, csv_path)
    print(f"CSV {csv_path}: marked {changed} rows delisted")

print("\nRun: git add repricer.py scanner.py booksgoat_enhanced.csv && git commit -m 'Add 2 ISBNs to blocklist - min qty' && git pull --rebase origin main && git push")
