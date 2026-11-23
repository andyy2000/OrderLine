"""The website importer, the real Top's China menu it produces, and the three-way dishes."""

import json
from pathlib import Path

import pytest

from app.menu import Menu, load_restaurant_menu
from app.session import OrderSession
from app.ticket import format_ticket
from scripts.import_menu import clean_name, parse_uber_eats, split_count, spoken_aliases
from scripts.import_site_menu import build, note_includes, parse_site

from .conftest import OPEN_TIME

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "data" / "sources" / "tops-china-website.txt"
UBER = ROOT / "data" / "sources" / "tops-china-ubereats.txt"
RULES = ROOT / "data" / "menus" / "tops-china.rules.json"


def test_name_helpers():
    assert clean_name("Chicken w. Broccoli") == "Chicken with Broccoli"
    assert split_count("Fried Shrimp (21 Pcs)") == ("Fried Shrimp", "21 pieces")
    assert spoken_aliases("Chicken with Broccoli") == ["Broccoli Chicken"]
    assert note_includes("Every plate $10.25, with pork fried rice and a pork egg roll") == ["Pork Fried Rice", "Pork Egg Roll"]
    assert note_includes("Served with white rice") == ["White Rice"]
    assert note_includes("Served with 4 pancakes") == ["4 Pancakes"]
    assert note_includes("Order plain, or make it a meal with a side") == []


def test_uber_eats_parser_still_works():
    items, categories = parse_uber_eats(UBER.read_text(encoding="utf-8"))
    assert any(i["name"] == "Crab Rangoon (8 Pcs)" and i["price_cents"] == 699 for i in items)


def test_site_parser_reads_prices_sizes_hours_and_phone():
    p = parse_site(SITE.read_text(encoding="utf-8"))
    by = {(i["category"], i["name"]): i for i in p["items"]}
    assert by[("Appetizers", "Fried Shrimp (21 pcs)")]["price_cents"] == 799
    assert by[("Soup", "Hot and Sour Soup")]["sizes"] == [{"name": "Pint", "price_cents": 399}, {"name": "Quart", "price_cents": 550}]
    assert by[("Soup", "Hot and Sour Soup")]["tags"] == ["spicy"]
    assert by[("Appetizers", "Boneless Spare Ribs")]["sizes"][1] == {"name": "Large", "price_cents": 1495}
    assert by[("House Specialties", "Fried Chicken Wings (4)")]["price_from"] is True
    assert by[("Combination Plates", "General Tso's Chicken")]["price_cents"] == 1025
    assert by[("All Day Special", "Bourbon Chicken")]["price_cents"] == 825
    assert by[("Chef's Specialties", "Bourbon Chicken")]["price_cents"] == 1250
    assert dict(p["categories"])["Chicken"] == "Served with white rice"
    assert p["phone"] == "+18045200380" and p["tax_rate"] == 0.10
    assert p["hours"]["mon"] == [] and p["hours"]["tue"] == [["10:30", "23:00"]]
    assert p["address"] == "20849 Woodpecker Rd, Petersburg, VA 23803"
    assert len(p["categories"]) == 20 and len(p["items"]) == 210


def test_committed_menu_matches_importer_output():
    menu_dict, _ = build(parse_site(SITE.read_text(encoding="utf-8")), json.loads(RULES.read_text(encoding="utf-8")))
    committed = json.loads((ROOT / "data" / "menus" / "tops-china.json").read_text(encoding="utf-8"))
    assert committed == menu_dict
    Menu.model_validate(menu_dict)


def test_real_menu_facts():
    menu = load_restaurant_menu("tops-china")
    assert menu.restaurant.phone == "+18045200380" and menu.restaurant.tax_rate == 0.10
    assert not menu.is_open(OPEN_TIME.replace(day=2))  # Monday: closed
    assert menu.is_open(OPEN_TIME)                       # Tuesday 6 PM
    combo = menu.get("general-tsos-chicken-combo")
    assert combo.price_cents == 1025 and combo.includes == ["Pork Fried Rice", "Pork Egg Roll"]
    assert combo.short_name == "General Tso's Chicken" and "General Tso's Chicken combination plate" in combo.aliases
    assert menu.get("general-tsos-chicken-special").includes == ["Fried Rice"]
    assert menu.get("chicken-with-broccoli").includes == ["White Rice"]
    assert [s.name for s in menu.get("chicken-with-broccoli").sizes] == ["Pint", "Quart"]
    assert menu.get("orange-chicken").name == "Orange Chicken" and "Chicken with Orange Flavor" in menu.get("orange-chicken").aliases
    assert menu.get("pork-egg-roll").price_cents == 190 and menu.get("shrimp-roll").price_cents == 199
    assert len(menu.category_confirm) == 6 and len(menu.confirmations()) == 12


@pytest.mark.parametrize("said, expected", [
    ("bourbon chicken combo", "bourbon-chicken-combo"),
    ("general chicken special", "general-tsos-chicken-special"),
    ("general chicken combination", "general-tsos-chicken-combo"),
    ("kung pao chicken combo", "kung-pao-chicken-combo"),
    ("orange chicken combo", "orange-chicken-combo"),
    ("egg roll", "pork-egg-roll"),
    ("shrimp roll", "shrimp-roll"),
    ("crab ragoon", "crab-rangoon"),
    ("house special lo mein", "house-special-lo-mein"),
    ("quart chicken broccoli", "chicken-with-broccoli"),
    ("pint of wonton soup", "wonton-soup"),
    ("sweet tea", "homemade-sweet-tea"),
    ("singapore noodles", "singapore-chow-mei-fun"),
    ("happy family", "happy-family"),
    ("moo goo gai pan combo", "moo-goo-gai-pan-combo"),
])
def test_real_menu_resolves(said, expected):
    item, candidates = load_restaurant_menu("tops-china").resolve(said)
    assert item is not None and item.id == expected, [c.id for c in candidates]


@pytest.mark.parametrize("said, ids", [
    ("bourbon chicken", {"bourbon-chicken", "bourbon-chicken-combo", "bourbon-chicken-special"}),
    ("general tso chicken", {"general-tsos-chicken", "general-tsos-chicken-combo", "general-tsos-chicken-special"}),
    ("pepper steak", {"pepper-steak", "pepper-steak-combo"}),
])
def test_three_way_dishes_ask_which(said, ids):
    item, candidates = load_restaurant_menu("tops-china").resolve(said)
    assert item is None and {c.id for c in candidates} == ids


def test_ambiguity_message_has_prices_and_sides():
    s = OrderSession(menu=load_restaurant_menu("tops-china"), now_fn=lambda: OPEN_TIME)
    r = s.call_tool("add_items", {"items": [{"query": "bourbon chicken"}]})
    err = r["problems"][0]["error"]
    assert "Bourbon Chicken ($12.50, with White Rice)" in err
    assert "Bourbon Chicken Combo ($10.25, with Pork Fried Rice and Pork Egg Roll)" in err
    assert "Bourbon Chicken Special ($8.25, with Fried Rice)" in err


def test_combo_swaps_change_what_comes_with_it():
    menu = load_restaurant_menu("tops-china")
    s = OrderSession(menu=menu, on_submit=lambda o: 3, now_fn=lambda: OPEN_TIME)
    r = s.call_tool("add_items", {"items": [
        {"query": "bourbon chicken combo", "options": ["shrimp roll"]},
        {"query": "general tso's chicken combo", "options": ["white rice", "no roll"]},
        {"query": "quart chicken broccoli", "options": ["fried rice"]},
    ]})
    assert r["problems"] == []
    assert r["added"][0] == "1 x Bourbon Chicken Combo (comes with Pork Fried Rice and Shrimp Roll)"
    assert r["added"][1] == "1 x General Tso's Chicken Combo (comes with White Rice and No Roll)"
    assert r["added"][2] == "1 x Chicken with Broccoli (Quart) (comes with Fried Rice)"
    lines = s.cart.lines
    assert lines[0].unit_price_cents == 1035 and lines[0].options == []  # swap, not an extra
    assert lines[2].unit_price_cents == 1299 + 100
    s.call_tool("get_order")
    s.call_tool("submit_order", {"customer_name": "Andy", "phone": "8045550123"})
    ticket = format_ticket(s.submitted_order, menu.restaurant.name)
    assert "w/ Pork Fried Rice, Shrimp Roll" in ticket and "w/ White Rice, No Roll" in ticket and "左宗鸡 套餐" in ticket


def test_read_back_sentence_is_complete_and_spoken():
    s = OrderSession(menu=load_restaurant_menu("tops-china"), now_fn=lambda: OPEN_TIME)
    assert s.call_tool("get_order")["read_back"] == "You don't have anything in your order yet."
    s.call_tool("add_items", {"items": [{"query": "bourbon chicken combo", "options": ["shrimp roll"]},
                                        {"query": "wonton soup", "size": "pint", "quantity": 2},
                                        {"query": "kung pao chicken combo", "options": ["hot"], "notes": "no peanuts"}]})
    rb = s.call_tool("get_order")["read_back"]
    assert rb == ("So that's one Bourbon Chicken Combo, with pork fried rice and shrimp roll; "
                  "two Wonton Soup, pint, with crispy noodles; and one Kung Pao Chicken Combo, hot, "
                  "with pork fried rice and pork egg roll, no peanuts. Your total is $29.81.")


def test_ask_restaurant_round_trip(store):
    asked = []
    s = OrderSession(menu=load_restaurant_menu("tops-china"), now_fn=lambda: OPEN_TIME,
                     on_ask=lambda q: asked.append(q) or "Yes, no peanuts is fine")
    r = s.call_tool("ask_restaurant", {"question": "Can you make kung pao without peanuts?"})
    assert r["ok"] and r["answer"].startswith("Yes") and asked == ["Can you make kung pao without peanuts?"]
    silent = OrderSession(menu=s.menu, now_fn=lambda: OPEN_TIME, on_ask=lambda q: None)
    assert "No answer" in silent.call_tool("ask_restaurant", {"question": "Any MSG?"})["error"]
    nobody = OrderSession(menu=s.menu, now_fn=lambda: OPEN_TIME)
    assert "can't be reached" in nobody.call_tool("ask_restaurant", {"question": "Any MSG?"})["error"]
    # the store side the kitchen tablet uses
    qid = store.ask_question("tops-china", "CA1", "Can you make it without MSG?", timeout_s=30)
    assert store.open_questions("tops-china")[0]["question"] == "Can you make it without MSG?"
    assert store.get_answer(qid) is None
    assert store.answer_question(qid, "Yes") is True and store.get_answer(qid) == "Yes"
    assert store.answer_question(qid, "No") is False  # already answered
    assert store.open_questions("tops-china") == []
    expired = store.ask_question("tops-china", "CA2", "Still there?", timeout_s=-1)
    assert all(q["id"] != expired for q in store.open_questions("tops-china"))


def test_keyterms_fit_deepgrams_budget():
    terms = load_restaurant_menu("tops-china").stt_keyterms()
    assert 20 <= len(terms) <= 60
    assert sum(len(t.split()) for t in terms) * 2 < 500  # generous token estimate
    for must in ["General Tso's", "Lo Mein", "Kung Pao", "Rangoon", "Bourbon", "Egg Foo Young", "Mei Fun"]:
        assert must in terms
    assert "Sauteed Broccoli" in terms  # accent stripped, not mangled
