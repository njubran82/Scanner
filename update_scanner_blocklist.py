txt = open('scanner.py', encoding='utf-8').read()

old = '"9780990873853"'
new = '"9780990873853",\n    "9781119826798"  # Architect\'s Studio Companion - PDF only'

count = txt.count(old)
print(f'Found in scanner.py: {count}')
if count:
    txt = txt.replace(old, new, 1)
    open('scanner.py', 'w', encoding='utf-8').write(txt)
    print('Done')
else:
    print('Pattern not found - checking blocklist structure...')
    import re
    m = re.search(r'BLOCKLIST[^}]+}', txt)
    if m:
        print('Blocklist found:', m.group(0)[:200])
