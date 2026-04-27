"""
add_blocklist.py — Adds ISBN 9781119143642 to scanner.py BLOCKLIST set.
Run from E:\Book\Scanner then push.
"""
from pathlib import Path

ISBN = "9781119143642"
COMMENT = "  # Understanding Behaviorism — blocklisted 2026-04-27"
TARGET = "scanner.py"

path = Path(__file__).parent / TARGET
text = path.read_text(encoding="utf-8")

if ISBN in text:
    print(f"Already in {TARGET} — nothing to do.")
else:
    marker = "BLOCKLIST = {"
    idx = text.index(marker) + len(marker)
    entry = f"\n    '{ISBN}',{COMMENT}"
    text = text[:idx] + entry + text[idx:]
    path.write_text(text, encoding="utf-8")
    print(f"Added {ISBN} to {TARGET}")
