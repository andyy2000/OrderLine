import asyncio
import os

import pytest
from starlette.testclient import TestClient

from app.notify import confirmation_text, send_sms
from app.server import create_app
from app.store import OrderStore

from .test_store_ticket_security import sample_order

R = "?restaurant=sample-china"


def test_confirmation_text_is_short_and_complete():
    order = {**sample_order(), "order_number": 7}
    text = confirmation_text(order, "Sample China")
    assert text.startswith("Sample China: order #7 received. 2 Egg Roll.")
    assert "Total $3.69" in text and "6:15 PM" in text and len(text) <= 320


def test_send_sms_posts_to_twilio_or_skips(monkeypatch):
    sent = {}

    async def fake_post(url, data, headers):
        sent.update(url=url, data=data, auth=headers["Authorization"])
        return 201, "{}"

    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    assert asyncio.run(send_sms("8045550123", "hi", post=fake_post)) is False  # not configured: skip quietly
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_SMS_FROM", "+18045550100")
    assert asyncio.run(send_sms("8045550123", "hi", post=fake_post)) is True
    assert sent["url"].endswith("/Accounts/AC1/Messages.json")
    assert sent["data"] == {"To": "+18045550123", "From": "+18045550100", "Body": "hi"}
    assert sent["auth"].startswith("Basic ")


@pytest.fixture
def client(store):
    return TestClient(create_app(store=store, admin_token="", twilio_auth_token="", bot_ws_url="wss://b/ws"))


def test_calls_page_api_and_stats(client, store):
    store.save_order(sample_order())
    store.log_call("CA9", "sample-china", "order", transcript=[{"role": "assistant", "content": "Thanks for calling"}],
                   tool_log=[{"tool": "get_order", "args": {}, "result": {"ok": True}}], caller_phone="8045550123")
    assert "<title>Calls · Sample China</title>" in client.get("/calls" + R).text
    calls = client.get("/api/calls" + R).json()
    assert calls[0]["call_id"] == "CA9" and "transcript" not in calls[0]
    one = client.get("/api/calls/CA9" + R).json()
    assert one["transcript"][0]["content"] == "Thanks for calling" and one["tool_log"][0]["tool"] == "get_order"
    assert client.post("/api/calls/CA9/flag" + R, json={"flagged": True}).json()["ok"]
    assert client.get("/api/calls/CA9" + R).json()["flagged"] is True
    assert client.get("/api/calls/nope" + R).status_code == 404
    s = client.get("/api/stats" + R + "&days=30").json()
    assert s["orders"] == 1 and s["revenue_cents"] == 369 and s["calls"] == 1 and s["completion_rate"] == 1.0


def test_stream_payload_includes_transfers(store):
    """The live payload is {orders, transfers}; the kitchen page shows transfers as banners."""
    store.save_order(sample_order())
    store.log_call("CA2", "sample-china", "transferred", cart={"lines": [{"description": "1 x Egg Roll"}]},
                   transfer_reason="catering", caller_phone="8045550123")
    app = create_app(store=store, admin_token="", twilio_auth_token="", poll_seconds=0.01)
    # Drive the SSE generator directly instead of holding an endless HTTP stream open.
    route = next(r for r in app.routes if r.path == "/api/stream")
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/api/stream", "query_string": b"restaurant=sample-china",
             "headers": [], "app": app}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def first_event():
        response = await route.endpoint(Request(scope, receive))
        agen = response.body_iterator
        chunk = await agen.__anext__()
        await agen.aclose()
        return chunk

    import json

    chunk = asyncio.run(first_event())
    data = json.loads(chunk.split("data: ", 1)[1])
    assert data["orders"][0]["customer_name"] == "Andy"
    assert data["transfers"][0]["reason"] == "catering" and data["transfers"][0]["cart"]["lines"][0]["description"] == "1 x Egg Roll"


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE calls (call_id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL, "
                       "started_at TEXT NOT NULL, outcome TEXT, transcript_json TEXT, tool_log_json TEXT);")
    conn.execute("INSERT INTO calls VALUES ('CA0', 'tops-china', '2026-11-01T00:00:00', 'order', '[]', '[]')")
    conn.commit()
    conn.close()
    store = OrderStore(path)  # must not fail on the old schema
    assert store.get_call("CA0")["flagged"] is False
    store.log_call("CA0", "sample-china", "order", metrics={"turns": 3})
    assert store.get_call("CA0")["metrics"] == {"turns": 3}
