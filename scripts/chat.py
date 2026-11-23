"""Talk to the phone agent by typing. Orders you confirm go to the kitchen screen.

    export ANTHROPIC_API_KEY=...
    python -m scripts.chat --restaurant tops-china

Type 'quit' to stop, 'cart' to print the current cart, 'tools' to see tool calls.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from app.agent.text_agent import TextAgent
from app.menu import load_restaurant_menu
from app.session import OrderSession
from app.store import OrderStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restaurant", default="tops-china")
    parser.add_argument("--db", default=os.getenv("ORDERLINE_DB", "data/orderline.db"))
    parser.add_argument("--ignore-hours", action="store_true",
                        help="pretend the restaurant is open (for testing at night); ORDERLINE_ALWAYS_OPEN=1 does the same")
    args = parser.parse_args()

    menu = load_restaurant_menu(args.restaurant)
    store = OrderStore(args.db)
    settings = store.get_settings(args.restaurant)
    now_fn = None
    if args.ignore_hours or os.getenv("ORDERLINE_ALWAYS_OPEN") == "1":
        tz = menu.now().tzinfo
        now_fn = lambda: datetime.now(tz).replace(hour=18, minute=0)  # noqa: E731
    def kitchen_answers(question: str) -> str | None:  # you play the kitchen in this test
        print(f"\n[KITCHEN SCREEN] Caller asks: {question}")
        return input("[KITCHEN] your answer (Enter for no answer): ").strip() or None

    session = OrderSession(menu=menu, on_submit=store.save_order, caller_phone="8045551234",
                           sold_out=set(settings["sold_out"]), pickup_minutes=settings["pickup_minutes"],
                           now_fn=now_fn, on_ask=kitchen_answers,
                           last_order=store.last_order_for_phone(args.restaurant, "8045551234"))
    agent = TextAgent(session)
    print(f"\nAGENT: {agent.opening_line}")
    while True:
        try:
            text = input("\nYOU:   ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"quit", "exit"}:
            break
        if text == "cart":
            print(json.dumps(session.cart.summary(), indent=2))
            continue
        if text == "tools":
            for entry in session.tool_log[-10:]:
                print(json.dumps(entry, ensure_ascii=False))
            continue
        print(f"\nAGENT: {agent.respond(text)}")
        if session.submitted_order or session.transferred:
            print("\n[call would end here]")
            break


if __name__ == "__main__":
    main()
