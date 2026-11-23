"""Turn a pasted Uber Eats menu page into a menu file, applying the restaurant's rules.

    python -m scripts.import_menu --source data/sources/tops-china-ubereats.txt \
        --rules data/menus/tops-china.rules.json

Writes data/menus/<id>.json and data/menus/<id>.REVIEW.md (everything a person
must check before real calls). Re-run any time the source text or rules change.

How the Uber Eats text is read (select-all + copy on the restaurant page):
- a line ending in "(UE)" starts a category
- inside a category: an item name line, then a "$x.xx" price line (sometimes
  with a rating on the same line), then optional rating / "Spicy" / "Popular" /
  description lines until a blank line
- the "Featured items" block near the top repeats items; combos only appear
  there, so those are kept and the duplicates dropped

Onboarding a new restaurant: paste its page into data/sources/, copy a rules
file, change the restaurant block, run this, then go through the REVIEW file.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from app.menu import Menu, normalize

PRICE_RE = re.compile(r"^\$(\d+(?:\.\d{2})?)")
RATING_RE = re.compile(r"^\d+% \(\d+\)$")
TAG_LINES = {"spicy": "spicy", "hot & spicy": "spicy", "hot and spicy": "spicy", "popular": "popular"}
SKIP_LINES = {"•", "see more", "show more"}
TOP_MATCHERS = [re.compile(r"^#\d+ most liked$")]

# Standard Cantonese-American takeout names for the kitchen ticket. Keyed by a
# normalized English name; the cook should confirm these.
KITCHEN_NAMES = {
    "general tsos chicken": "左宗鸡", "general tso tofu": "左宗豆腐", "sesame chicken": "芝麻鸡",
    "orange chicken": "陈皮鸡", "bourbon chicken": "波本鸡", "boneless spare ribs": "无骨排骨",
    "chicken with broccoli": "芥兰鸡", "shrimp with broccoli": "芥兰虾", "beef with broccoli": "芥兰牛",
    "chicken with oyster sauce": "蚝油鸡", "chicken with black bean sauce": "豉汁鸡",
    "chicken with snow peas": "雪豆鸡", "kung po chicken": "宫保鸡丁", "chicken with cashew nuts": "腰果鸡",
    "pepper steak with onion": "青椒牛", "curry beef with onion": "咖喱牛", "mongolian beef": "蒙古牛",
    "beef with snow peas": "雪豆牛", "beef with chinese vegetables": "唐菜牛",
    "shrimp with black bean sauce": "豉汁虾", "baby shrimp with chili sauce": "辣酱虾",
    "shrimp with chinese vegetable": "唐菜虾", "curry shrimp with onion": "咖喱虾",
    "roast pork with mushroom": "冬菇叉烧", "mixed vegetables": "什菜", "sauteed broccoli with garlic sauce": "蒜蓉芥兰",
    "bean curd home style": "家常豆腐", "fried shrimp": "炸虾", "crab rangoon": "蟹角",
    "wonton soup": "云吞汤", "egg drop soup": "蛋花汤", "hot and sour soup": "酸辣汤",
    "egg drop with wonton soup": "蛋花云吞汤", "chicken rice soup": "鸡饭汤", "house special soup": "本楼汤",
    "mixed vegetable soup": "什菜汤", "roast pork yat gaw mein": "叉烧一个面", "chicken noodles soup": "鸡面汤",
    "roast pork fried rice": "叉烧炒饭", "house special fried rice": "本楼炒饭", "shrimp fried rice": "虾炒饭",
    "chicken fried rice": "鸡炒饭", "vegetables fried rice": "菜炒饭", "beef fried rice": "牛炒饭",
    "crab meat fried rice": "蟹肉炒饭", "beef lo mein": "牛捞面", "roast pork lo mein": "叉烧捞面",
    "shrimp lo mein": "虾捞面", "house special lo mein": "本楼捞面", "vegetables lo mein": "菜捞面",
    "chicken lo mein": "鸡捞面", "crab meat lo mein": "蟹肉捞面", "shrimp egg foo young": "虾芙蓉蛋",
    "beef egg foo young": "牛芙蓉蛋", "chicken egg foo young": "鸡芙蓉蛋", "crab meat egg foo young": "蟹肉芙蓉蛋",
    "house special egg foo young": "本楼芙蓉蛋", "roast pork egg foo young": "叉烧芙蓉蛋",
}


def slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", normalize(text))).strip("-")


def clean_name(raw: str) -> str:
    """Make a name the AI can say: 'Chicken w. Broccoli' -> 'Chicken with Broccoli'."""
    name = raw.strip()
    name = re.sub(r"\bw\.\s*", "with ", name)
    name = name.replace("&", "and")
    name = re.sub(r"\s+", " ", name)
    return name


def split_count(name: str) -> tuple[str, str | None]:
    """'Fried Shrimp (21 Pcs)' -> ('Fried Shrimp', '21 pieces'), so the spoken name stays clean."""
    m = re.match(r"^(.*?)\s*\((\d+)\s*(?:pcs|pieces|pc)\.?\)$", name, re.I)
    if m:
        return m.group(1).strip(), f"{m.group(2)} pieces"
    return name, None


def spoken_aliases(name: str) -> list[str]:
    """'Chicken with Broccoli' -> ['Broccoli Chicken']; 'Beef Lo Mein' gets nothing."""
    m = re.match(r"^(\w+) with (.+)$", name, re.I)
    if m:
        return [f"{m.group(2)} {m.group(1)}"]
    return []


def parse_uber_eats(text: str) -> tuple[list[dict], list[str]]:
    """Return (items, categories_seen). Each item: name, price_cents, category, description, tags."""
    lines = [l.rstrip() for l in text.splitlines()]
    items: list[dict] = []
    categories: list[str] = []
    category: str | None = None
    i = 0
    featured = False
    while i < len(lines):
        line = lines[i].strip()
        if line.endswith("(UE)"):
            category = line[:-4].strip()
            if category not in categories:
                categories.append(category)
            featured = False
            i += 1
            continue
        if line == "Featured items":
            category, featured = "Featured items", True
            i += 1
            continue
        if line == "Rating and reviews":
            category = None
            i += 1
            continue
        if category and line and not line.startswith("$") and not RATING_RE.match(line) \
                and line not in SKIP_LINES and not any(m.match(line) for m in TOP_MATCHERS):
            # candidate name: the next non-blank, non-noise line must be a price
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip() in SKIP_LINES
                                      or any(m.match(lines[j].strip()) for m in TOP_MATCHERS)
                                      or lines[j].strip() == line):
                j += 1
            if j < len(lines) and PRICE_RE.match(lines[j].strip()):
                price = int(round(float(PRICE_RE.match(lines[j].strip()).group(1)) * 100))
                item = {"name": line, "price_cents": price, "category": category, "description": None, "tags": []}
                k = j + 1
                while not featured and k < len(lines) and lines[k].strip() \
                        and not any(m.match(lines[k].strip()) for m in TOP_MATCHERS):
                    extra = lines[k].strip()
                    low = extra.lower()
                    if extra in SKIP_LINES or RATING_RE.match(extra) or extra == line:
                        pass
                    elif low in TAG_LINES:
                        if TAG_LINES[low] not in item["tags"]:
                            item["tags"].append(TAG_LINES[low])
                    elif not item["description"] and not extra.startswith("$"):
                        item["description"] = extra
                    k += 1
                items.append(item)
                i = k
                continue
        i += 1
    return items, categories


def apply_rules(parsed: list[dict], categories: list[str], rules: dict) -> tuple[dict, list[str]]:
    review: list[str] = []
    sized = set(rules.get("sized_categories", []))
    size_names = rules.get("size_names", ["Small", "Large"])
    markup = rules.get("large_markup_cents", 0)
    cat_includes = rules.get("category_includes", {})
    rename = rules.get("category_rename", {})
    drop = set(rules.get("drop_categories", []))
    extra_aliases = rules.get("extra_aliases", {})
    spice_levels = rules.get("spice_levels")
    featured_cats = rules.get("featured_categories", {})
    cat_groups = rules.get("category_option_groups", {})
    cat_confirm = rules.get("category_confirm", {})

    def canonical_category(name: str) -> tuple[str, list[str]]:
        """'All Day Special (w. Pork Fried Rice & Pork Egg Roll)' -> ('All Day Special', [...])."""
        m = re.match(r"^(.*?)\s*\((?:w\.|with)\s*(.+)\)$", name)
        includes: list[str] = []
        if m:
            name = m.group(1).strip()
            includes = [clean_name(p) for p in re.split(r"\s*(?:&|and)\s*", m.group(2))]
        name = rename.get(name, name)
        return name, includes or list(cat_includes.get(name, []))

    # First pass: real categories. Second: featured-only items (combos).
    real_names: set[str] = set()
    items: list[dict] = []
    for p in parsed:
        if p["category"] in drop:
            continue
        cat, includes = canonical_category(p["category"])
        name = clean_name(p["name"])
        real_names.add(normalize(name))
        items.append(_build_item(name, p, cat, includes, sized, size_names, markup, rules, spice_levels, review))

    for p in parsed:
        if p["category"] != "Featured items":
            continue
        raw = p["name"].strip()
        is_combo = raw.endswith("Combo") or raw.startswith("C. ")
        name = clean_name(re.sub(r"^C\.\s*", "", raw))
        if is_combo and not name.endswith("Combo"):
            name += " Combo"
        if normalize(name) in real_names:
            continue
        real_names.add(normalize(name))
        if is_combo:
            cat, includes = "Dinner Combos", list(cat_includes.get("Dinner Combos", []))
            price = rules.get("combo_price_cents") or p["price_cents"]
            item = _build_item(name, {**p, "price_cents": price}, cat, includes, set(), size_names, 0, rules,
                               spice_levels, review)
            if raw.startswith("C. "):
                item["aliases"].append(raw)
        elif name in featured_cats:
            cat, includes = canonical_category(featured_cats[name])
            item = _build_item(name, p, cat, includes, sized, size_names, markup, rules, spice_levels, review)
        else:
            item = _build_item(name, p, "Featured (confirm category)", [], set(), size_names, 0, rules,
                               spice_levels, review)
            item["confirm"] = "Only seen in 'Featured items': put it in the right category and check sizes."
            review.append(f"{item['id']}: {item['confirm']}")
        items.append(item)

    for item in items:
        for alias in extra_aliases.get(item["id"], []):
            if alias not in item["aliases"]:
                item["aliases"].append(alias)
        for group in cat_groups.get(item["category"], []):
            item.setdefault("option_groups", []).append(json.loads(json.dumps(group)))
        if item["category"] in cat_confirm and not item.get("confirm"):
            item["confirm"] = cat_confirm[item["category"]]
            review.append(f"{item['id']}: {item['confirm']}")

    seen_cats = [canonical_category(c)[0] for c in categories if c not in drop]
    present = {i["category"] for i in items}
    empty = [c for c in seen_cats if c not in present]
    if empty:
        review.append("These sections were listed on the page but had no items in the pasted text (expand them on "
                      "Uber Eats and paste again, or type them in): " + ", ".join(empty))

    menu = {"restaurant": rules["restaurant"], "items": items}
    return menu, review


def _build_item(name, p, cat, includes, sized, size_names, markup, rules, spice_levels, review) -> dict:
    name, count = split_count(name)
    item: dict = {"id": slug(name), "name": name, "category": cat, "aliases": spoken_aliases(name),
                  "tags": list(p["tags"]), "includes": includes}
    if count:
        item["description"] = count
    if p.get("description"):
        desc = clean_name(p["description"]).replace("Pcs", "pieces")
        if count and count in desc:
            desc = desc  # keep the page's wording
        served = re.search(r"served with ([^.;]+)", desc, re.I)
        if served:  # "Served w. white rice." -> comes with White Rice
            item["includes"] = [served.group(1).strip().title()]
            desc = re.sub(r"\s*served with [^.;]+\.?", "", desc, flags=re.I).strip(" ;")
        if desc:
            item["description"] = desc if not count else f"{count}. {desc}" if count not in desc else desc
    raw = p["name"].strip()
    if clean_name(raw) != raw:
        item["aliases"].append(raw)  # the Uber Eats spelling, e.g. "Chicken w. Broccoli"
    kitchen = KITCHEN_NAMES.get(normalize(re.sub(r"\(.*?\)", "", name)).replace("combo", "").strip())
    if kitchen:
        item["kitchen_name"] = kitchen
    max_sized = rules.get("sized_max_price_cents", 10**9)
    if cat in sized and p["price_cents"] > max_sized:
        item["price_cents"] = p["price_cents"]
        item["confirm"] = "Priced like a specialty, so no sizes were added: check sizes and what it comes with."
        review.append(f"{item['id']}: {item['confirm']}")
    elif cat in sized:
        item["sizes"] = [{"name": size_names[0], "price_cents": p["price_cents"]},
                         {"name": size_names[1], "price_cents": p["price_cents"] + markup}]
        item["includes"] = list(rules.get("sized_includes", [])) or includes
        item["confirm"] = rules.get("sized_confirm")
        review.append(f"{item['id']}: {size_names[1]} price guessed as ${(p['price_cents'] + markup) / 100:.2f}")
    else:
        item["price_cents"] = p["price_cents"]
    if "spicy" in item["tags"] and spice_levels and cat not in set(rules.get("spice_option_exclude_categories", [])):
        item["option_groups"] = [{"name": "Spice level", "min": 0, "max": 1,
                                  "options": [{"name": s} for s in spice_levels]}]
    return item


def write_review(path: Path, menu: Menu, review: list[str], source: str) -> None:
    lines = [f"# {menu.restaurant.name} menu: things to check before real calls", "",
             f"Built from `{source}`. Re-run `python -m scripts.import_menu` after editing the rules file.", ""]
    if menu.restaurant.confirm:
        lines += ["## Restaurant", "", f"- [ ] {menu.restaurant.confirm}", ""]
    lines += ["## Items", ""]
    for note in review:
        lines.append(f"- [ ] {note}")
    lines += ["", "## Kitchen names", "",
              "- [ ] Ask the cook whether the Chinese names on the tickets are the ones they use; edit `kitchen_name` in the menu file.",
              "", "## Not on the page at all", "",
              "- [ ] Egg rolls, drinks, desserts, sides, chef's specialties and dinner combo list were not in the pasted text.",
              f"", f"{len(menu.items)} items imported across {len(menu.categories())} categories."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--rules", required=True)
    ap.add_argument("--out-dir", default="data/menus")
    args = ap.parse_args()
    rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
    parsed, categories = parse_uber_eats(Path(args.source).read_text(encoding="utf-8"))
    menu_dict, review = apply_rules(parsed, categories, rules)
    menu = Menu.model_validate(menu_dict)  # fails loudly on a bad menu
    out = Path(args.out_dir) / f"{menu.restaurant.id}.json"
    out.write_text(json.dumps(menu_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    review_path = Path(args.out_dir) / f"{menu.restaurant.id}.REVIEW.md"
    write_review(review_path, menu, review, args.source)
    print(f"{menu.restaurant.name}: {len(menu.items)} items in {len(menu.categories())} categories -> {out}")
    print(f"{len(review)} things to check -> {review_path}")
    for cat in menu.categories():
        names = [i.name for i in menu.items if i.category == cat]
        print(f"  {cat} ({len(names)}): {', '.join(names[:6])}{' ...' if len(names) > 6 else ''}")


if __name__ == "__main__":
    main()
