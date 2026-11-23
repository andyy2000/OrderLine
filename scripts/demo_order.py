"""Place a sample order with no API keys, exactly the way the AI would (same tool calls).

Use it to see the kitchen screen and printer work before wiring up the voice bot:
    python -m scripts.demo_order                       # Top's China real menu
    DEMO_RESTAURANT=sample-china python -m scripts.demo_order
"""

from __future__ import annotations

import os
from datetime import datetime

from app.menu import load_restaurant_menu
from app.session import OrderSession
from app.store import OrderStore
from app.ticket import format_ticket

ORDERS = {
    "tops-china": [
        {"query": "general chicken combo", "options": ["shrimp roll"]},
        {"query": "quart of chicken broccoli", "options": ["fried rice"]},
        {"query": "pint shrimp fried rice"},
        {"query": "wonton soup", "size": "pint", "quantity": 2},
        {"query": "kung pao chicken combo", "options": ["hot"], "notes": "no peanuts"},
    ],
    "sample-china": [
        {"query": "general chicken", "size": "large", "options": ["extra spicy"]},
        {"query": "won ton soup", "size": "quart"},
        {"query": "egg rolls", "quantity": 2},
        {"query": "shrimp lo mein", "size": "small", "options": ["shrimp"], "notes": "no onions"},
    ],
}


def main() -> None:
    restaurant_id = os.getenv("DEMO_RESTAURANT", "tops-china")
    menu = load_restaurant_menu(restaurant_id)
    store = OrderStore(os.getenv("ORDERLINE_DB", "data/orderline.db"))
    tz = menu.now().tzinfo
    session = OrderSession(menu=menu, on_submit=store.save_order,
                           now_fn=lambda: datetime.now(tz).replace(hour=18, minute=0))
    # The whole order in ONE tool call, written the way a caller says it.
    steps = [
        ("add_items", {"items": ORDERS[restaurant_id]}),
        ("get_order", {}),
        ("submit_order", {"customer_name": "Andy", "phone": "(804) 555-0123"}),
    ]
    for name, args in steps:
        result = session.call_tool(name, args)
        print(f"{name} -> ok={result['ok']}" + ("" if result["ok"] else f" error={result['error']}"))
        for line in result.get("added", []):
            print(f"   added: {line}")
        for problem in result.get("problems", []):
            print(f"   problem: {problem}")
    order = session.submitted_order
    print("\n" + format_ticket(order, menu.restaurant.name))


if __name__ == "__main__":
    main()
