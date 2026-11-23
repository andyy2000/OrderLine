from app import twilio_security
from app.store import OrderStore
from app.ticket import format_ticket


def sample_order(pickup="2026-11-03T18:15-05:00"):
    return {"restaurant_id": "sample-china", "customer_name": "Andy", "phone": "8045550123",
            "lines": [{"item_id": "egg-roll", "name": "Egg Roll", "kitchen_name": "春卷", "size": None,
                       "options": [], "quantity": 2, "notes": "extra sauce",
                       "unit_price_cents": 175, "line_total_cents": 350}],
            "subtotal_cents": 350, "tax_cents": 19, "total_cents": 369, "pickup_at": pickup, "call_id": None}


def test_order_numbers_restart_each_local_day(tmp_path):
    store = OrderStore(tmp_path / "t.db")
    assert store.save_order(sample_order()) == 1
    assert store.save_order(sample_order()) == 2
    assert store.save_order(sample_order("2026-11-04T12:00-05:00")) == 1


def test_status_and_change_marker(tmp_path):
    store = OrderStore(tmp_path / "t.db")
    store.save_order(sample_order())
    oid = store.list_orders("sample-china")[0]["id"]
    before = store.last_change("sample-china")
    store.set_status(oid, "ready")
    assert store.get_order(oid)["status"] == "ready"
    assert store.last_change("sample-china") != before
    assert store.list_orders("sample-china", ["confirmed"]) == []


def test_settings_roundtrip(store):
    assert store.get_settings("sample-china")["ai_enabled"] is True
    store.update_settings("sample-china", ai_enabled=False, sold_out=["egg-roll", "egg-roll"])
    s = store.get_settings("sample-china")
    assert s["ai_enabled"] is False and s["sold_out"] == ["egg-roll"]


def test_ticket_has_everything_the_kitchen_needs():
    order = {**sample_order(), "order_number": 42}
    text = format_ticket(order, "Sample China")
    for expected in ["SAMPLE CHINA  #0042", "Pickup 6:15 PM", "2  Egg Roll", "春卷", "** extra sauce",
                     "$3.69", "PAY AT PICKUP", "804-555-0123"]:
        assert expected in text


def test_twilio_signature():
    params = {"CallSid": "CA123", "From": "+18045550123", "To": "+18045550100"}
    url = "https://example.com/twilio/voice?restaurant=sample-china"
    sig = twilio_security.compute_signature("secret", url, params)
    assert twilio_security.is_valid("secret", url, params, sig)
    assert not twilio_security.is_valid("secret", url, {**params, "From": "+1999"}, sig)
    assert not twilio_security.is_valid("other", url, params, sig)
    assert not twilio_security.is_valid("secret", url, params, None)
