"""Lint a menu file before a restaurant goes live.

    python -m scripts.check_menu tops-china

Catches the things that cause wrong orders on real calls:
- two items whose names sound so alike the AI will have to ask every time
- items with sizes but no price, option groups with min > max, duplicate ids
- names that don't resolve to themselves when spoken plainly
- missing kitchen names (optional, but the cook will want them)
"""

from __future__ import annotations

import sys
from itertools import combinations

from app.menu import load_restaurant_menu, sound_text


def main(restaurant_id: str) -> int:
    menu = load_restaurant_menu(restaurant_id)  # raises with a clear message on schema errors
    problems: list[str] = []
    warnings: list[str] = []

    for item in menu.items:
        found, candidates = menu.resolve(item.name)
        if found is None and any(c.id == item.id for c in candidates):
            continue  # a dish that comes several ways: the AI asks which, that's correct
        if found is None or found.id != item.id:
            problems.append(f"'{item.name}' does not resolve to itself (got "
                            f"{found.name if found else [c.name for c in candidates]}); add aliases or rename")
        for group in item.option_groups:
            if group.min > group.max:
                problems.append(f"{item.id}: option group '{group.name}' has min > max")
            if not group.options:
                problems.append(f"{item.id}: option group '{group.name}' has no options")
        if not item.kitchen_name:
            warnings.append(f"{item.id}: no kitchen_name (ticket will show the English name only)")

    for a, b in combinations(menu.items, 2):
        if sound_text(a.name) == sound_text(b.name):
            problems.append(f"'{a.name}' and '{b.name}' sound identical; callers can't tell them apart")

    keyterms = menu.stt_keyterms()
    print(f"{menu.restaurant.name}: {len(menu.items)} items, {len(keyterms)} speech keyterms, "
          f"{len(menu.categories())} categories")
    for w in warnings:
        print(f"  warning: {w}")
    for p in problems:
        print(f"  PROBLEM: {p}")
    confirmations = menu.confirmations()
    for who, note in confirmations:
        print(f"  CONFIRM {who}: {note}")
    if problems:
        print(f"{len(problems)} problem(s)")
        return 1
    if confirmations:
        print(f"NOT READY FOR LIVE CALLS: {len(confirmations)} thing(s) to confirm with the owner "
              f"(see data/menus/{restaurant_id}.REVIEW.md); remove each item's \"confirm\" field once checked.")
        return 2
    print("OK: ready for live calls")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "tops-china"))
