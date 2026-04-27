import csv
rows = list(csv.DictReader(open(r'E:\Book\Lister\booksgoat_enhanced.csv', encoding='utf-8')))
active = [r for r in rows if r.get('status') == 'active']
ghosts = [r for r in active if not r.get('offer_id', '').strip()]
real   = [r for r in active if r.get('offer_id', '').strip()]
print(f'Active in CSV: {len(active)}')
print(f'With offer_id (real): {len(real)}')
print(f'Without offer_id (ghosts): {len(ghosts)}')
