import csv, os, re
from pathlib import Path
from datetime import datetime

NEW_ISBN = "9780415898058"
NEW_REASON = "Even if it Costs Me My Life — min qty 5 on BooksGoat"

for fname in ['repricer.py', 'scanner.py']:
    path = Path(fname)
    if not path.exists():
        print(f"SKIP {fname} — not found")
        continue
    txt = path.read_text(encoding='utf-8')
    if NEW_ISBN in txt:
        print(f"{fname}: already in blocklist")
        continue
    # Find blocklist closing brace
    match = re.search(r"(BLOCKLIST\s*=\s*\{[^}]*?)(\})", txt, re.DOTALL)
    if match:
        new_line = f"    '{NEW_ISBN}',  # {NEW_REASON}\n"
        txt = txt[:match.end(1)] + new_line + txt[match.end(1):]
        path.write_text(txt, encoding='utf-8')
        print(f"{fname}: added")
    else:
        print(f"{fname}: BLOCKLIST pattern not found")

for csv_path in [r'E:\Book\Lister\booksgoat_enhanced.csv', r'E:\Book\Scanner\booksgoat_enhanced.csv']:
    if not Path(csv_path).exists():
        print(f"SKIP {csv_path}")
        continue
    rows = list(csv.DictReader(open(csv_path, encoding='utf-8')))
    changed = 0
    for row in rows:
        if row.get('isbn13') == NEW_ISBN and row.get('status') != 'delisted':
            row['status'] = 'delisted'
            row['delisted_at'] = datetime.now().isoformat()
            row['delist_reason'] = 'unavailable'
            changed += 1
    fields = list(dict.fromkeys(k for r in rows for k in r))
    tmp = csv_path + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, csv_path)
    print(f"CSV {csv_path}: marked {changed} rows delisted")

print("\nRun: git add repricer.py scanner.py booksgoat_enhanced.csv && git commit -m 'Add Even if it Costs Me My Life to blocklist' && git push")
