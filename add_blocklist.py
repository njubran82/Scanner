"""
add_blocklist.py — Adds ISBN 9781119143642 to all blocklists.

Run from E:\Book\Scanner (repo root), then push to GitHub.

Usage:
    python add_blocklist.py
"""
import re
from pathlib import Path

ISBN = "9781119143642"
TITLE = "Understanding Behaviorism: Behavior, Culture, and Evolution"
COMMENT = "# Blocklisted 2026-04-27"

FILES = {
    "scanner.py": r"(BLOCKLIST\s*=\s*\[)",
    "lister.py":  r"(BLOCKLIST\s*=\s*\[)",
    "repricer.py": r"(BLOCKLIST\s*=\s*\[)",
}

def add_to_blocklist(path: Path, pattern: str) -> bool:
    if not path.exists():
        print(f"  SKIP — {path} not found")
        return False

    text = path.read_text(encoding="utf-8")

    if ISBN in text:
        print(f"  SKIP — {path.name}: ISBN already present")
        return False

    m = re.search(pattern, text)
    if not m:
        print(f"  SKIP — {path.name}: BLOCKLIST definition not found")
        return False

    insert_at = m.end()
    new_entry = f'\n    "{ISBN}",  {COMMENT} — {TITLE}'
    text = text[:insert_at] + new_entry + text[insert_at:]
    path.write_text(text, encoding="utf-8")
    print(f"  OK    — {path.name}: added {ISBN}")
    return True


def main():
    print(f"Adding {ISBN} ({TITLE}) to blocklists\n")
    repo = Path(__file__).parent

    changed = 0
    for filename, pattern in FILES.items():
        changed += add_to_blocklist(repo / filename, pattern)

    if changed:
        print(f"\nDone. {changed} file(s) updated.")
        print("Next: git add -A && git commit -m 'Blocklist: Understanding Behaviorism' && git push")
    else:
        print("\nNo files were modified.")


if __name__ == "__main__":
    main()
