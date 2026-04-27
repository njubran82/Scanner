"""
Patches repricer.py to add comp floor filter.
Discards any comp below cost * COMP_FLOOR_MULTIPLIER before taking min.
This eliminates used copies / wrong editions from pulling price down.
"""
from pathlib import Path

txt = Path('repricer.py').read_text(encoding='utf-8')

# Find the get_ebay_comps function and add floor filter before returning
old = '''    conf = 'HIGH' if len(prices) >= 3 else 'MEDIUM' if prices else 'NONE'
    return prices, conf'''

new = '''    conf = 'HIGH' if len(prices) >= 3 else 'MEDIUM' if prices else 'NONE'
    return prices, conf

def filter_comps(prices: list, cost: float, multiplier: float = 1.1) -> list:
    """Discard comps below cost * multiplier — eliminates used/wrong edition outliers."""
    if not prices or not cost:
        return prices
    floor = cost * multiplier
    filtered = [p for p in prices if p >= floor]
    return filtered if filtered else prices  # fall back to unfiltered if all removed'''

count = txt.count(old)
print(f'Found get_ebay_comps return: {count}')
if count:
    txt = txt.replace(old, new, 1)

# Now patch calc_target to use filter_comps
old2 = '''    comps, conf = get_ebay_comps(isbn, app_token)
    if comps:
        target = round(min(comps) * (1 - UNDERCUT_PCT), 2)'''

new2 = '''    comps, conf = get_ebay_comps(isbn, app_token)
    if comps:
        comps = filter_comps(comps, cost)  # remove cheap outliers before taking min
        target = round(min(comps) * (1 - UNDERCUT_PCT), 2)'''

count2 = txt.count(old2)
print(f'Found calc_target comps: {count2}')
if count2:
    txt = txt.replace(old2, new2, 1)

Path('repricer.py').write_text(txt, encoding='utf-8')
print('Patched successfully' if count and count2 else 'PARTIAL patch — check output')
