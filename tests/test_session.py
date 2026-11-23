from app.session import OrderSession, clean_phone

from .conftest import CLOSED_TIME


def place_basic_order(session):
    assert session.call_tool("add_item", {"item_id": "general-tso", "size": "Large"})["ok"]
    assert session.call_tool("add_item", {"item_id": "egg-roll", "quantity": 2})["ok"]


def test_happy_path_submits_and_saves(session, store):
    place_basic_order(session)
    order = session.call_tool("get_order")
    assert order["total"] == "$17.32"  # 1645 + 5.3% tax (87) = 1732
    result = session.call_tool("submit_order", {"customer_name": "Andy", "phone": "804-555-0123"})
    assert result["ok"] and result["order_number"] == 1
    saved = store.list_orders("sample-china")
    assert len(saved) == 1 and saved[0]["phone"] == "8045550123" and saved[0]["total_cents"] == 1732


def test_submit_requires_read_back_after_last_change(session):
    place_basic_order(session)
    r = session.call_tool("submit_order", {"customer_name": "A", "phone": "8045550123"})
    assert not r["ok"] and "get_order" in r["error"]
    session.call_tool("get_order")
    session.call_tool("add_item", {"item_id": "soda", "options": ["Sprite"]})  # changed after read-back
    r = session.call_tool("submit_order", {"customer_name": "A", "phone": "8045550123"})
    assert not r["ok"]
    session.call_tool("get_order")
    assert session.call_tool("submit_order", {"customer_name": "A", "phone": "8045550123"})["ok"]


def test_submit_validates_name_phone_and_empty(session):
    session.call_tool("get_order")
    assert "empty" in session.call_tool("submit_order", {"customer_name": "A", "phone": "8045550123"})["error"]
    place_basic_order(session)
    session.call_tool("get_order")
    assert "10 digits" in session.call_tool("submit_order", {"customer_name": "A", "phone": "555-0123"})["error"]
    assert "name" in session.call_tool("submit_order", {"customer_name": " ", "phone": "8045550123"})["error"]


def test_no_edits_after_submit(session):
    place_basic_order(session)
    session.call_tool("get_order")
    session.call_tool("submit_order", {"customer_name": "A", "phone": "8045550123"})
    assert "already sent" in session.call_tool("add_item", {"item_id": "egg-roll"})["error"]
    assert "already sent" in session.call_tool("submit_order", {"customer_name": "A", "phone": "8045550123"})["error"]


def test_closed_restaurant_refuses_orders(menu):
    s = OrderSession(menu=menu, now_fn=lambda: CLOSED_TIME)
    r = s.call_tool("add_item", {"item_id": "egg-roll"})
    assert not r["ok"] and "closed" in r["error"]
    assert s.call_tool("answer_question", {"topic": "open_now"})["answer"] == "closed"


def test_bad_tool_calls_never_raise(session):
    assert not session.call_tool("launch_rocket")["ok"]
    assert not session.call_tool("add_item", {"wrong_arg": 1})["ok"]
    assert not session.call_tool("remove_item", {"line_index": 3})["ok"]
    assert len(session.tool_log) == 3


def test_search_marks_sold_out(menu):
    s = OrderSession(menu=menu, sold_out={"egg-roll"})
    match = s.call_tool("search_menu", {"query": "egg roll"})["matches"][0]
    assert match["item_id"] == "egg-roll" and match["sold_out"] is True


def test_answers_come_from_data(session):
    assert session.call_tool("answer_question", {"topic": "pickup time"})["answer"] == "about 15 minutes"
    s2 = OrderSession(menu=session.menu, pickup_minutes=35)
    assert s2.call_tool("answer_question", {"topic": "pickup_time"})["answer"] == "about 35 minutes"
    unknown = session.call_tool("answer_question", {"topic": "parking"})
    assert unknown["ok"] and "hours" in unknown["topics"]


def test_transfer_calls_hook(menu):
    reasons = []
    s = OrderSession(menu=menu, on_transfer=reasons.append)
    assert s.call_tool("transfer_to_human", {"reason": "catering"})["ok"]
    assert s.transferred and reasons == ["catering"]


def test_clean_phone():
    assert clean_phone("+1 (804) 555-0123") == "8045550123"
    assert clean_phone("804.555.0123") == "8045550123"
    assert clean_phone("555-0123") is None


def test_submit_uses_caller_id_when_no_phone_given(menu, store):
    from .conftest import OPEN_TIME

    finished = []
    s = OrderSession(menu=menu, on_submit=store.save_order, caller_phone="8045550199",
                     on_finish=finished.append, now_fn=lambda: OPEN_TIME)
    place_basic_order(s)
    s.call_tool("get_order")
    r = s.call_tool("submit_order", {"customer_name": "Bobby"})
    assert r["ok"] and s.submitted_order["phone"] == "8045550199"
    assert finished == ["order placed"]


def test_submit_without_caller_id_asks_for_number(session):
    place_basic_order(session)
    session.call_tool("get_order")
    r = session.call_tool("submit_order", {"customer_name": "Bobby"})
    assert not r["ok"] and "callback" in r["error"]


def test_end_call_marks_outcome_and_calls_hook(menu):
    finished = []
    s = OrderSession(menu=menu, on_finish=finished.append)
    assert s.outcome() == "no_order"
    r = s.call_tool("end_call", {"reason": "prank"})
    assert r["ok"] and "goodbye" in r["instruction"].lower()
    assert s.ended_reason == "prank" and s.outcome() == "ended" and finished == ["prank"]
