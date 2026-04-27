import re
from pathlib import Path

content = Path('repricer_input.py').read_text(encoding='utf-8')

# 1. Remove keyword fallback from get_ebay_comps
old_fallback = '''    if not prices:
        try:
            r = requests.get(
                'https://api.ebay.com/buy/browse/v1/item_summary/search',
                headers=headers,
                params={'q': isbn, 'category_ids': '267',
                        'filter': 'conditions:{NEW},buyingOptions:{FIXED_PRICE}',
                        'sort': 'price', 'limit': '20'},
                timeout=15)
            for item in r.json().get('itemSummaries', []):
                try: prices.append(float(item['price']['value']))
                except: pass
        except Exception as e:
            log.warning(f'  Keyword error {isbn}: {e}')
    conf = 'HIGH' if len(prices) >= 3 else 'MEDIUM' if prices else 'NONE\''''

new_fallback = '''    # No keyword fallback — broad title searches match wrong editions and corrupt pricing
    conf = 'HIGH' if len(prices) >= 3 else 'MEDIUM' if prices else 'NONE\''''

content = content.replace(old_fallback, new_fallback)

# 2. Add MIN_PRICE_MULTIPLIER constant after AMAZON_CAP line
content = content.replace(
    'AMAZON_CAP    = 0.95',
    'AMAZON_CAP    = 0.95\nMIN_PRICE_MULT = 1.20   # never list below cost × 1.2 regardless of comps'
)

# 3. In calc_target, apply floor after computing target from comps
old_target_block = '''    if amazon_price and target > amazon_price * AMAZON_CAP:
        target = round(amazon_price * AMAZON_CAP, 2)
        method += ' [amazon capped]'
    profit = round(target * (1 - EBAY_FEE_RATE) - cost, 2)
    return target, profit, method'''

new_target_block = '''    if amazon_price and target > amazon_price * AMAZON_CAP:
        target = round(amazon_price * AMAZON_CAP, 2)
        method += ' [amazon capped]'
    # Never list below cost × MIN_PRICE_MULT — prevents bad comps from killing margin
    min_price = round(cost * MIN_PRICE_MULT, 2)
    if target < min_price:
        target = min_price
        method += ' [min floor]'
    profit = round(target * (1 - EBAY_FEE_RATE) - cost, 2)
    return target, profit, method'''

content = content.replace(old_target_block, new_target_block)

Path('repricer_output.py').write_text(content, encoding='utf-8')
print("Done")

# Verify the changes
if 'No keyword fallback' in content:
    print("✅ Keyword fallback removed")
else:
    print("❌ Keyword fallback NOT removed")

if 'MIN_PRICE_MULT' in content:
    print("✅ MIN_PRICE_MULT added")
else:
    print("❌ MIN_PRICE_MULT NOT added")

if 'min floor' in content:
    print("✅ Min floor logic added")
else:
    print("❌ Min floor logic NOT added")
