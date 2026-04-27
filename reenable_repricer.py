from pathlib import Path

yml_path = Path('.github/workflows/scanner.yml')
txt = yml_path.read_text(encoding='utf-8')

old = '      - name: Run repricer (DISABLED)\n        if: false'
new = '      - name: Run repricer'

count = txt.count(old)
print(f'Found: {count}')
if count:
    txt = txt.replace(old, new, 1)
    yml_path.write_text(txt, encoding='utf-8')
    print('Done — repricer re-enabled in scanner.yml')
else:
    print('Pattern not found')
