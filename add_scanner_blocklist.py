txt = open('scanner.py', encoding='utf-8').read()

old = 'MIN_PROFIT    = 12.00         # was 1.00'
new = '''MIN_PROFIT    = 12.00         # was 1.00

# Permanent blocklist — do not list these ISBNs under any circumstances
BLOCKLIST = {
    "9781260460445",  # Lange Q&A Radiography — min qty 5
    "9780990873853",  # Overcoming Gravity — min qty 5
    "9781119826798",  # Architect's Studio Companion — PDF only on BooksGoat
}'''

count = txt.count(old)
print(f'Found: {count}')
txt = txt.replace(old, new, 1)
open('scanner.py', 'w', encoding='utf-8').write(txt)
print('Done')
