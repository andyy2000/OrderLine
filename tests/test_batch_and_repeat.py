"""add_items (one call per order), name resolution, reorder_last, transfers and caps."""

import pytest

from app.menu import sound_key
from app.order import MAX_TOTAL_CENTS, OrderError
from app.session import OrderSession

from .conftest import OPEN_TIME


def test_whole_order_in_one_call(session):
    r = session.call_tool("add_items", {"items": [
        {"query": "a large general chicken", "size": "large", "options": ["extra spicy"]},
        {"query": "two egg rolls", "quantity": 2},
        {"query": "quart of wonton soup", "size": "quart"},
        {"query": "shrimp lo mein", "size": "small", "options": ["shrimp"], "notes": "no onions"},
    ]})
    assert r["ok"] and r["problems"] == []
    assert len(r["added"]) == 4 and len(session.cart.lines) == 4
    ids = [l.item_id for l in session.cart.lines]
    assert ids == ["general-tso", "egg-roll", "wonton-soup", "lo-mein"]
    assert session.cart.lines[3].options == ["Shrimp"] and session.cart.lines[3].notes == "no onions"
    assert len(session.tool_log) == 1  # one round trip for the whole order


@pytest.mark.parametrize("heard, expected", [
    ("general sauce chicken", "general-tso"),   # speech-to-text mishears Tso's
    ("crab ragoon", "crab-rangoon"),
    ("lo main", "lo-mein"),
    ("won ton soup", "wonton-soup"),
    ("diet coke", "soda"),
    ("potstickers", "fried-dumplings"),
])
def test_speech_errors_still_resolve(menu, heard, expected):
    item, candidates = menu.resolve(heard)
    assert item is not None and item.id == expected, candidates


def test_ambiguous_and_off_menu_become_questions(session):
    r = session.call_tool("add_items", {"items": [{"query": "soup", "size": "pint"},
                                                  {"query": "orange chicken"},
                                                  {"query": "egg roll"}]})
    assert r["ok"] and len(r["added"]) == 1  # egg roll went in
    errors = [p["error"] for p in r["problems"]]
    assert any("could mean" in e and "Wonton Soup" in e for e in errors)
    assert any("orange chicken" in e for e in errors)
    assert "instruction" in r


def test_partial_batch_reports_missing_size_per_item(session):
    r = session.call_tool("add_items", {"items": [{"query": "general chicken"}, {"query": "lo mein", "size": "large"}]})
    assert not r["ok"] and r["added"] == []
    assert "needs a size" in r["problems"][0]["error"]
    assert "Protein" in r["problems"][1]["error"]
    assert "error" in r  # so the model sees it as a failed call


def test_batch_limits(session):
    assert not session.call_tool("add_items", {"items": []})["ok"]
    assert "at most" in session.call_tool("add_items", {"items": [{"query": "egg roll"}] * 16})["error"]
    assert not session.call_tool("add_items", {"items": "egg roll"})["ok"]


def test_phone_order_cap_sends_to_staff(session):
    r = session.call_tool("add_items", {"items": [{"item_id": "general-tso", "size": "Large", "quantity": 20}] * 2})
    assert len(session.cart.lines) == 1  # 20 x 12.95 = 259; a second 259 would pass 300
    assert "limit" in r["problems"][0]["error"]
    assert session.cart.subtotal_cents < MAX_TOTAL_CENTS


def test_reorder_last(menu, store):
    first = OrderSession(menu=menu, on_submit=store.save_order, now_fn=lambda: OPEN_TIME)
    first.call_tool("add_items", {"items": [{"item_id": "general-tso", "size": "Large", "options": ["Spicy"]},
                                            {"item_id": "egg-roll", "quantity": 2}]})
    first.call_tool("get_order")
    first.call_tool("submit_order", {"customer_name": "Andy", "phone": "8045550123"})

    last = store.last_order_for_phone("sample-china", "8045550123")
    assert last and len(last["lines"]) == 2
    assert store.last_order_for_phone("sample-china", "8045559999") is None

    again = OrderSession(menu=menu, on_submit=store.save_order, now_fn=lambda: OPEN_TIME,
                         caller_phone="8045550123", last_order=last, sold_out={"egg-roll"})
    assert "General Tso's Chicken (Large), Spicy" in again.last_order_summary()
    r = again.call_tool("reorder_last")
    assert r["ok"] and len(r["added"]) == 1 and "sold out" in r["problems"][0]["error"]
    assert again.cart.lines[0].options == ["Spicy"]

    nobody = OrderSession(menu=menu, now_fn=lambda: OPEN_TIME)
    assert "No previous order" in nobody.call_tool("reorder_last")["error"]
    assert nobody.last_order_summary() is None


def test_transfer_keeps_partial_cart_for_staff(session, store):
    session.call_tool("add_items", {"items": [{"query": "egg roll", "quantity": 3}]})
    session.call_tool("transfer_to_human", {"reason": "wants catering"})
    store.log_call("CA1", "sample-china", "transferred", cart=session.cart.summary(),
                   transfer_reason=session.transfer_reason, caller_phone="8045550123")
    t = store.recent_transfers("sample-china")
    assert len(t) == 1 and t[0]["transfer_reason"] == "wants catering"
    assert t[0]["cart"]["lines"][0]["description"] == "3 x Egg Roll"
    assert session.outcome() == "transferred"


def test_call_log_updates_and_stats(store):
    store.log_call("CA1", "sample-china", "transferred", cart={"lines": []}, transfer_reason="angry")
    store.log_call("CA1", "sample-china", "transferred", transcript=[{"role": "user", "content": "hi"}],
                   metrics={"latency_median_s": 0.9})
    c = store.get_call("CA1")
    assert c["transfer_reason"] == "angry" and c["cart"] == {"lines": []}  # kept from the first write
    assert c["transcript"][0]["content"] == "hi" and c["metrics"]["latency_median_s"] == 0.9
    store.log_call("CA2", "sample-china", "order")
    store.log_call("CA3", "sample-china", "no_order")
    store.flag_call("CA3", True)
    assert store.list_calls("sample-china", outcome="order")[0]["call_id"] == "CA2"
    assert store.get_call("CA3")["flagged"] is True
    s = store.stats("sample-china", days=1)
    assert s["calls"] == 3 and s["calls_by_outcome"] == {"transferred": 1, "order": 1, "no_order": 1}
    assert s["transfer_rate"] == pytest.approx(1 / 3, abs=0.001)
    assert s["average_call_seconds"] is not None


def test_sound_key_groups_sound_alikes():
    assert sound_key("rangoon") == sound_key("ragoon") or len(set(sound_key("rangoon")) ^ set(sound_key("ragoon"))) <= 1
    assert sound_key("tsos") == sound_key("sauce")
    assert sound_key("mein") == sound_key("main")
    assert sound_key("chicken") != sound_key("beef")


def test_closed_blocks_edits_but_not_questions(menu):
    from .conftest import CLOSED_TIME
    s = OrderSession(menu=menu, now_fn=lambda: CLOSED_TIME)
    assert "closed" in s.call_tool("add_items", {"items": [{"query": "egg roll"}]})["error"]
    assert s.call_tool("answer_question", {"topic": "hours"})["ok"]
    assert s.call_tool("search_menu", {"query": "egg roll"})["ok"]
