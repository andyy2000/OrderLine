from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.menu import load_restaurant_menu
from app.session import OrderSession
from app.store import OrderStore

TZ = ZoneInfo("America/New_York")
OPEN_TIME = datetime(2026, 11, 3, 18, 0, tzinfo=TZ)     # Tuesday 6 PM
CLOSED_TIME = datetime(2026, 11, 3, 23, 30, tzinfo=TZ)  # Tuesday 11:30 PM


@pytest.fixture
def menu():
    return load_restaurant_menu("sample-china")


@pytest.fixture
def store():
    return OrderStore(":memory:")


@pytest.fixture
def session(menu, store):
    return OrderSession(menu=menu, on_submit=store.save_order, now_fn=lambda: OPEN_TIME)
