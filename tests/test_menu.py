import pytest
from pydantic import ValidationError

from app.menu import Menu, normalize

from .conftest import CLOSED_TIME, OPEN_TIME, TZ


def test_menu_loads_with_unique_ids(menu):
    assert menu.restaurant.id == "sample-china"
    assert len({i.id for i in menu.items}) == len(menu.items)


@pytest.mark.parametrize("said, expected", [
    ("general chicken", "general-tso"),
    ("General Tso's", "general-tso"),
    ("general tao chicken", "general-tso"),
    ("potstickers", "fried-dumplings"),
    ("crab wontons", "crab-rangoon"),
    ("hot and sour", "hot-sour-soup"),
    ("lomein", "lo-mein"),
    ("broccoli beef", "beef-broccoli"),
    ("kung pow chicken", "kung-pao-chicken"),
])
def test_search_finds_what_callers_say(menu, said, expected):
    results = menu.search(said)
    assert results, f"nothing found for {said!r}"
    assert results[0][0].id == expected


def test_search_returns_nothing_for_off_menu(menu):
    assert all(item.id != "orange-chicken" for item, _ in menu.search("orange chicken"))
    assert menu.search("pizza") == []


def test_hours(menu):
    from datetime import datetime
    assert menu.is_open(OPEN_TIME)
    assert not menu.is_open(CLOSED_TIME)
    assert not menu.is_open(datetime(2026, 11, 3, 10, 59, tzinfo=TZ))
    assert menu.is_open(datetime(2026, 11, 3, 11, 0, tzinfo=TZ))
    assert "Tue: 11 AM to 10 PM" in menu.hours_text()


def test_normalize():
    assert normalize("General Tso’s  Chicken!") == "general tsos chicken"


def test_item_without_price_is_rejected(menu):
    data = menu.model_dump()
    data["items"].append({"id": "x", "name": "X", "category": "C"})
    with pytest.raises(ValidationError):
        Menu.model_validate(data)


def test_duplicate_ids_rejected(menu):
    data = menu.model_dump()
    data["items"].append(dict(data["items"][0]))
    with pytest.raises(ValidationError):
        Menu.model_validate(data)
