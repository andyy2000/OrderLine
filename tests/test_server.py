import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import twilio_security
from app.server import create_app
from app.store import OrderStore

from .test_store_ticket_security import sample_order

R = "?restaurant=sample-china"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(store):
    app = create_app(store=store, admin_token="", twilio_auth_token="", bot_ws_url="wss://bot.example/ws",
                     poll_seconds=0.05)
    return TestClient(app)


def test_kitchen_page_and_unknown_restaurant(client):
    page = client.get("/kitchen" + R)
    assert page.status_code == 200
    assert "<title>Kitchen · Sample China</title>" in page.text
    assert client.get("/kitchen?restaurant=nope").status_code == 404


def test_orders_status_and_print_queue(client, store):
    store.save_order(sample_order())
    orders = client.get("/api/orders" + R).json()
    assert len(orders) == 1
    queue = client.get("/api/print-queue" + R).json()
    assert len(queue) == 1 and "Egg Roll" in queue[0]["ticket"]
    oid = queue[0]["id"]
    assert client.post(f"/api/orders/{oid}/printed" + R).json()["status"] == "printed"
    assert client.get("/api/print-queue" + R).json() == []
    assert client.post(f"/api/orders/{oid}/status" + R, json={"status": "ready"}).json()["status"] == "ready"
    assert client.post(f"/api/orders/{oid}/status" + R, json={"status": "eaten"}).status_code == 400
    assert client.post("/api/orders/999/status" + R, json={"status": "ready"}).status_code == 404


def test_stream_pushes_new_orders_over_real_server(tmp_path):
    """Start the real server with uvicorn, open the live stream, add an order, see it arrive."""
    db = tmp_path / "live.db"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "ORDERLINE_DB": str(db), "ADMIN_TOKEN": "", "TWILIO_AUTH_TOKEN": ""}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.server:app", "--port", str(port),
                             "--log-level", "warning"], cwd=ROOT, env=env)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/stream" + R)
        resp = conn.getresponse()
        assert resp.getheader("content-type").startswith("text/event-stream")

        def next_data():
            while True:
                line = resp.readline().decode()
                if line.startswith("data:"):
                    return json.loads(line[len("data: "):])

        assert next_data() == {"orders": [], "transfers": [], "questions": []}  # nothing yet
        OrderStore(db).save_order(sample_order())  # e.g. the voice bot, a separate process
        data = next_data()["orders"]
        assert data[0]["customer_name"] == "Andy" and data[0]["order_number"] == 1
        conn.close()
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_settings_validation(client):
    assert client.get("/api/settings" + R).json()["ai_enabled"] is True
    assert client.post("/api/settings" + R, json={"sold_out": ["egg-roll"], "pickup_minutes": 30}).status_code == 200
    s = client.get("/api/settings" + R).json()
    assert s["sold_out"] == ["egg-roll"] and s["pickup_minutes"] == 30
    assert client.post("/api/settings" + R, json={"sold_out": ["pizza"]}).status_code == 400
    assert client.post("/api/settings" + R, json={"pickup_minutes": 999}).status_code == 400


def test_twilio_webhook_routes_to_bot_or_phone(client):
    r = client.post("/twilio/voice" + R, data={"From": "+18045551234", "To": "+18045550100"})
    assert '<Stream url="wss://bot.example/ws">' in r.text
    assert 'name="from_number" value="+18045551234"' in r.text
    client.post("/api/settings" + R, json={"ai_enabled": False})
    r = client.post("/twilio/voice" + R, data={"From": "+18045551234"})
    assert "<Dial>+18045550100</Dial>" in r.text
    assert "<Dial>" in client.post("/twilio/fallback" + R).text


def test_admin_token_and_twilio_signature(store):
    app = create_app(store=store, admin_token="t0k", twilio_auth_token="secret",
                     bot_ws_url="wss://bot.example/ws", public_base_url="https://kitchen.example")
    c = TestClient(app)
    assert c.get("/api/orders" + R).status_code == 401
    assert c.get("/api/orders" + R + "&token=t0k").status_code == 200
    assert c.get("/api/orders" + R, headers={"X-Admin-Token": "t0k"}).status_code == 200
    form = {"CallSid": "CA1", "From": "+18045551234", "To": "+18045550100"}
    assert c.post("/twilio/voice" + R, data=form).status_code == 403
    sig = twilio_security.compute_signature("secret", "https://kitchen.example/twilio/voice" + R, form)
    ok = c.post("/twilio/voice" + R, data=form, headers={"X-Twilio-Signature": sig})
    assert ok.status_code == 200 and "<Stream" in ok.text


def test_block_and_unblock_number(client):
    assert client.post("/api/block" + R, json={"phone": "123"}).status_code == 400
    r = client.post("/api/block" + R, json={"phone": "(804) 555-0100"})
    assert r.status_code == 200 and r.json()["blocked_numbers"] == ["8045550100"]
    assert client.get("/api/settings" + R).json()["blocked_numbers"] == ["8045550100"]
    client.post("/api/block" + R, json={"phone": "8045550100", "blocked": False})
    assert client.get("/api/settings" + R).json()["blocked_numbers"] == []
