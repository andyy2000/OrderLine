"""Build a menu file from the restaurant's own website text (select-all + copy on the menu page).

    python -m scripts.import_site_menu --source data/sources/tops-china-website.txt \
        --rules data/menus/tops-china.rules.json

The site format (one of Andy's WebNest menu sites):
- a nav block lists every category between "Menu" and "Search dishes"
- each category header is followed by an optional one-line note ("Served with white
  rice", "Every plate $10.25, with pork fried rice and a pork egg roll")
- each item is: name (a trailing "Spicy" means spicy), optional description, then a
  price line: "$7.99", "From $7.99", "Sm $6.75 / Lg $14.95" or "Pt $3.99 / Qt $5.50"
- the footer has the phone number, the hours by weekday and the site's tax estimate

Prices, sizes, includes, hours and phone all come from the page, so the rules file
only adds what the page can't say: spoken aliases, swap options, naming for the
combo/special categories, and anything the owner must confirm.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from app.menu import Menu, normalize
from scripts.import_menu import KITCHEN_NAMES, clean_name, slug, spoken_aliases

PRICE_RE = re.compile(r"^(From\s+)?\$(\d+(?:\.\d{2})?)$")
SIZED_RE = re.compile(r"^(Sm|Lg|Pt|Qt|Small|Large|Pint|Quart)\s+\$(\d+(?:\.\d{2})?)(?:\s*/\s*(Sm|Lg|Pt|Qt|Small|Large|Pint|Quart)\s+\$(\d+(?:\.\d{2})?))?$")
SIZE_LABEL = {"Sm": "Small", "Lg": "Large", "Pt": "Pint", "Qt": "Quart"}
WEEKDAYS = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
            "saturday": "sat", "sunday": "sun"}
EXTRA_KITCHEN = {
    "moo goo gai pan": "蘑菇鸡片", "happy family": "全家福", "seafood delight": "海鲜大会", "four seasons": "四季",
    "triple delight": "三鲜", "subgum wonton": "什锦云吞", "steak kew": "牛柳", "sesame beef": "芝麻牛",
    "orange beef": "陈皮牛", "butterfly shrimp": "蝴蝶虾", "dragon and phoenix": "龙凤配", "hunan chicken": "湖南鸡",
    "hunan beef": "湖南牛", "hunan shrimp": "湖南虾", "chicken szechuan style": "四川鸡", "beef szechuan style": "四川牛",
    "shrimp szechuan style": "四川虾", "szechuan chicken": "四川鸡", "szechuan beef": "四川牛", "szechuan shrimp": "四川虾",
    "chicken with orange flavor": "陈皮鸡", "general shrimp": "左宗虾", "crispy shrimp": "脆皮虾",
    "kung pao chicken": "宫保鸡丁", "kung pao shrimp": "宫保虾", "curry chicken with onion": "咖喱鸡",
    "curry chicken": "咖喱鸡", "chicken with garlic sauce": "鱼香鸡", "shrimp with garlic sauce": "鱼香虾",
    "beef with garlic sauce": "鱼香牛", "shredded beef with garlic sauce": "鱼香牛", "scallops and shrimp with garlic sauce": "鱼香虾带子",
    "shrimp with lobster sauce": "虾龙糊", "shrimp with mushroom": "冬菇虾", "shrimp with mixed vegetables": "什菜虾",
    "shrimp with cashew nuts": "腰果虾", "shrimp with snow peas": "雪豆虾", "shrimp with chili sauce": "辣酱虾",
    "shrimp with chinese vegetables": "唐菜虾", "shrimp with onion": "洋葱虾", "beef with mixed vegetables": "什菜牛",
    "beef with onion": "洋葱牛", "chicken with mixed vegetables": "什菜鸡", "pepper steak": "青椒牛",
    "pepper steak with onions": "青椒牛", "curry beef with onions": "咖喱牛", "curry shrimp with onions": "咖喱虾",
    "roast pork with mixed vegetables": "什菜叉烧", "roast pork with snow peas": "雪豆叉烧",
    "roast pork with pepper and tomato": "番茄青椒叉烧", "roast pork with bean curd": "豆腐叉烧",
    "roast pork with cashew nuts": "腰果叉烧", "roast pork with broccoli": "芥兰叉烧", "pork with scallions": "葱爆肉",
    "roast pork with chinese vegetables": "唐菜叉烧", "roast pork with onion": "洋葱叉烧",
    "sweet and sour chicken": "甜酸鸡", "sweet and sour pork": "甜酸肉", "sweet and sour shrimp": "甜酸虾",
    "sweet and sour triple": "甜酸三拼", "bean curd szechuan style": "四川豆腐", "pork egg roll": "春卷",
    "shrimp roll": "虾卷", "vegetable spring roll": "素春卷", "fried wonton": "炸云吞", "bbq spare ribs": "烧排骨",
    "bbq boneless ribs": "无骨烧排", "teriyaki beef": "照烧牛串", "teriyaki chicken": "照烧鸡串",
    "teriyaki chicken on a stick": "照烧鸡串", "pu pu platter": "宝宝盘", "fried donuts": "炸甜圈", "french fries": "薯条",
    "steamed dumplings": "蒸饺", "fried dumplings": "锅贴", "chicken noodle soup": "鸡面汤",
    "fried chicken wings": "炸鸡翅", "honey wings": "蜜汁鸡翅", "fried chicken wings with garlic sauce": "鱼香鸡翅",
    "chicken wings with fries": "鸡翅薯条", "fried crab sticks": "炸蟹柳", "fried scallops": "炸带子",
    "steamed rice": "白饭", "fried noodles": "炸面", "fortune cookies": "签语饼", "homemade sweet tea": "甜茶",
    "lemonade": "柠檬水", "singapore chow mei fun": "星洲炒米", "harvest chicken and shrimp": "丰收鸡虾",
    "hawaiian five o": "夏威夷五", "vegetable lo mein": "菜捞面", "vegetable fried rice": "菜炒饭",
    "vegetable chop suey": "菜杂碎", "vegetable chow mein": "菜炒面", "moo shu vegetables": "木须菜",
}


def _sizes(line: str) -> list[dict] | None:
    m = SIZED_RE.match(line)
    if not m:
        return None
    sizes = [{"name": SIZE_LABEL.get(m.group(1), m.group(1)), "price_cents": int(round(float(m.group(2)) * 100))}]
    if m.group(3):
        sizes.append({"name": SIZE_LABEL.get(m.group(3), m.group(3)), "price_cents": int(round(float(m.group(4)) * 100))})
    return sizes


def _is_price(line: str) -> bool:
    return bool(PRICE_RE.match(line) or SIZED_RE.match(line))


def parse_site(text: str) -> dict:
    """Return {"categories": [(name, note)], "items": [...], "phone", "hours", "tax_rate", "address"}."""
    lines = [l.strip() for l in text.splitlines()]
    # category list from the nav block
    try:
        start = lines.index("Menu") + 1
        end = next(i for i in range(start, len(lines)) if lines[i].startswith("Search dishes"))
    except (ValueError, StopIteration):
        raise SystemExit("Could not find the category list between 'Menu' and 'Search dishes'")
    category_names = [l for l in lines[start:end] if l]
    stop = next((i for i in range(end, len(lines)) if lines[i] == "Your order"), len(lines))

    items: list[dict] = []
    categories: list[tuple[str, str | None]] = []
    category: str | None = None
    i = end + 1
    while i < stop:
        line = lines[i]
        if not line:
            i += 1
            continue
        if line in category_names:
            category = line
            categories.append((category, None))
            i += 1
            continue
        if category is None:
            i += 1
            continue
        # gather the next non-empty lines to decide what this is
        nxt = [j for j in range(i + 1, min(i + 4, stop)) if lines[j]]
        if nxt and _is_price(lines[nxt[0]]):
            items.append(_item(line, None, lines[nxt[0]], category))
            i = nxt[0] + 1
            continue
        if len(nxt) >= 2 and _is_price(lines[nxt[1]]) and not _is_price(lines[nxt[0]]) \
                and lines[nxt[0]] not in category_names:
            items.append(_item(line, lines[nxt[0]], lines[nxt[1]], category))
            i = nxt[1] + 1
            continue
        # not an item: it's the category's note line
        if categories and categories[-1][0] == category and categories[-1][1] is None:
            categories[-1] = (category, line)
        i += 1

    footer = "\n".join(lines[stop:])
    phone = re.search(r"\((\d{3})\)\s*(\d{3})-(\d{4})", footer)
    tax = re.search(r"tax \((\d+(?:\.\d+)?)%\)", footer)
    hours: dict[str, list] = {}
    for day, key in WEEKDAYS.items():
        m = re.search(rf"{day.title()}\n(.+)", footer)
        if not m:
            continue
        val = m.group(1).strip()
        span = re.match(r"(\d{1,2}(?::\d{2})?\s*[AP]M) to (\d{1,2}(?::\d{2})?\s*[AP]M)", val)
        hours[key] = [[_hhmm(span.group(1)), _hhmm(span.group(2))]] if span else []
    address = re.search(r"(\d+ [^\n]+(?:Rd|Road|St|Street|Ave|Avenue|Blvd|Dr|Drive)[^\n]*)\n([^\n]+, [A-Z]{2} \d{5})", footer)
    return {
        "categories": categories, "items": items,
        "phone": f"+1{phone.group(1)}{phone.group(2)}{phone.group(3)}" if phone else None,
        "tax_rate": float(tax.group(1)) / 100 if tax else None,
        "hours": hours or None,
        "address": f"{address.group(1)}, {address.group(2)}" if address else None,
    }


def _hhmm(text: str) -> str:
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)", text.strip())
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _item(name_line: str, desc: str | None, price_line: str, category: str) -> dict:
    tags = []
    name = name_line
    if name.endswith("Spicy") and not name.endswith(" Spicy"):
        name, tags = name[:-5].strip(), ["spicy"]
    elif name.endswith(" Spicy"):
        name, tags = name[:-6].strip(), ["spicy"]
    item = {"name": name, "description": desc, "tags": tags, "category": category,
            "price_cents": None, "sizes": None, "price_from": False}
    sizes = _sizes(price_line)
    if sizes:
        item["sizes"] = sizes
    else:
        m = PRICE_RE.match(price_line)
        item["price_cents"] = int(round(float(m.group(2)) * 100))
        item["price_from"] = bool(m.group(1))
    return item


def note_includes(note: str | None) -> list[str]:
    """'Every plate $10.25, with pork fried rice and a pork egg roll' -> ['Pork Fried Rice', 'Pork Egg Roll']."""
    if not note:
        return []
    m = re.search(r"(?:served )?with (.+?)\.?$", note, re.I)
    if not m or "make it a meal" in note.lower():
        return []
    parts = re.split(r"\s*(?:,|and)\s*", m.group(1))
    out = []
    for p in parts:
        p = re.sub(r"^(a|an|the)\s+", "", p.strip(), flags=re.I)
        if p:
            out.append(" ".join(w if w[0].isdigit() else w.capitalize() for w in p.split()))
    return out


def build(parsed: dict, rules: dict) -> tuple[dict, list[str]]:
    review: list[str] = []
    r = dict(rules["restaurant"])
    for key in ("phone", "tax_rate", "hours", "address"):
        if parsed.get(key) and not r.get(key):
            r[key] = parsed[key]
    notes = dict(parsed["categories"])
    suffix = rules.get("category_name_suffix", {})
    alias_suffixes = rules.get("category_alias_suffixes", {})
    cat_groups = rules.get("category_option_groups", {})
    cat_confirm = rules.get("category_confirm", {})
    extra_aliases = rules.get("extra_aliases", {})
    spice_levels = rules.get("spice_levels")
    no_spice = set(rules.get("spice_option_exclude_categories", []))
    rename = rules.get("category_rename", {})
    item_rename = rules.get("item_rename", {})  # "Chicken with Orange Flavor": "Orange Chicken"

    items: list[dict] = []
    ids: set[str] = set()
    for p in parsed["items"]:
        cat = rename.get(p["category"], p["category"])
        base = clean_name(re.sub(r"\s*\((?:\d+|for \d+|with bone|large|\d+ pcs)\)\s*$", "", p["name"], flags=re.I))
        count = re.search(r"\((\d+)(?: pcs)?\)", p["name"])
        renamed_from = None
        if base in item_rename:
            renamed_from, base = base, item_rename[base]
        name = base + suffix.get(p["category"], "")
        item: dict = {"id": slug(name), "name": name, "category": cat, "aliases": [], "tags": list(p["tags"]),
                      "includes": note_includes(notes.get(p["category"]))}
        if item["id"] in ids:
            item["id"] = slug(f"{name} {cat}")
        ids.add(item["id"])
        if name != base:
            item["short_name"] = base
            for s in alias_suffixes.get(p["category"], []):
                item["aliases"].append(f"{base} {s}")
        item["aliases"] += [a for a in spoken_aliases(base) if a not in item["aliases"]]
        if renamed_from:
            item["aliases"].append(renamed_from + suffix.get(p["category"], ""))
        if p["name"] != base and p["name"] not in item["aliases"]:
            item["aliases"].append(re.sub(r"Spicy$", "", p["name"]).strip())
        desc = p.get("description")
        if count:
            desc = f"{count.group(1)} pieces" + (f". {desc}" if desc else "")
        if desc:
            item["description"] = desc
        kitchen = EXTRA_KITCHEN.get(normalize(base)) or KITCHEN_NAMES.get(normalize(base))
        if kitchen:
            item["kitchen_name"] = kitchen + rules.get("kitchen_name_suffix", {}).get(p["category"], "")
        if p["sizes"]:
            item["sizes"] = p["sizes"]
        else:
            item["price_cents"] = p["price_cents"]
        if p["price_from"]:
            item["confirm"] = "Site says 'From' this price: confirm the plain price and the make-it-a-meal upcharge."
            review.append(f"{item['id']}: {item['confirm']}")
        if "spicy" in item["tags"] and spice_levels and cat not in no_spice:
            item["option_groups"] = [{"name": "Spice level", "min": 0, "max": 1,
                                      "options": [{"name": s} for s in spice_levels]}]
        for group in cat_groups.get(p["category"], []):
            item.setdefault("option_groups", []).append(json.loads(json.dumps(group)))
        for alias in extra_aliases.get(item["id"], []):
            if alias not in item["aliases"]:
                item["aliases"].append(alias)
        if item["id"] in set(rules.get("popular_items", [])) and "popular" not in item["tags"]:
            item["tags"].append("popular")
        items.append(item)

    category_notes = {rename.get(c, c): n for c, n in parsed["categories"] if n}
    menu = {"restaurant": r, "category_notes": category_notes,
            "category_confirm": {rename.get(c, c): note for c, note in cat_confirm.items()}, "items": items}
    for cat, note in cat_confirm.items():
        review.insert(0, f"{cat}: {note}")
    if r.get("confirm"):
        review.insert(0, f"restaurant: {r['confirm']}")
    return menu, review


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--rules", required=True)
    ap.add_argument("--out-dir", default="data/menus")
    args = ap.parse_args()
    rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
    parsed = parse_site(Path(args.source).read_text(encoding="utf-8"))
    menu_dict, review = build(parsed, rules)
    menu = Menu.model_validate(menu_dict)
    out = Path(args.out_dir) / f"{menu.restaurant.id}.json"
    out.write_text(json.dumps(menu_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    review_path = Path(args.out_dir) / f"{menu.restaurant.id}.REVIEW.md"
    lines = [f"# {menu.restaurant.name} menu: things to check before real calls", "",
             f"Built from `{args.source}`. Re-run `python -m scripts.import_site_menu` after editing the rules file.", ""]
    lines += [f"- [ ] {note}" for note in review]
    lines += ["- [ ] Ask the cook whether the Chinese names on the tickets are the ones they use.",
              "", f"{len(menu.items)} items in {len(menu.categories())} categories; "
              f"phone {menu.restaurant.phone}; tax {menu.restaurant.tax_rate:.1%}; {len(menu.stt_keyterms())} speech keyterms."]
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(lines[-1])
    print(f"{len(review)} things to check -> {review_path}")
    for cat in menu.categories():
        names = [i.name for i in menu.items if i.category == cat]
        print(f"  {cat} ({len(names)}): {', '.join(names[:5])}{' ...' if len(names) > 5 else ''}")


if __name__ == "__main__":
    main()
