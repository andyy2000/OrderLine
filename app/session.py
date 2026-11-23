"""One phone call = one OrderSession.

The voice bot and the text prototype both talk to the order only through
`OrderSession.call_tool(name, args)`. It always returns a JSON-safe dict:
{"ok": True, ...} on success, {"ok": False, "error": "..."} when a rule
blocks the action. It never raises, so a bad tool call can't crash a live call.

Efficiency: the AI can add a whole order in ONE call (`add_items` takes what
the caller said, e.g. "general chicken", and resolves it to a menu item), and a
repeat caller can get their last order in one call (`reorder_last`). Fewer tool
round trips means fewer model calls per order, which is most of the latency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from .menu import SIZE_ALIASES, Item, Menu, fmt_money, normalize
from .order import Cart, OrderError

PHONE_RE = re.compile(r"\D")
MAX_BATCH = 15


def clean_phone(raw: str) -> str | None:
    digits = PHONE_RE.sub("", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def _describe_item(item: Item, sold_out: set[str]) -> dict:
    d = {
        "item_id": item.id,
        "name": item.name,
        "price": item.price_label(),
        "sizes": [s.name for s in item.sizes],
        "options": [
            {"group": g.name, "required": g.required, "max": g.max,
             **({"swaps_out": g.replaces} if g.replaces else {}),
             "choices": [o.name + (f" (+{fmt_money(o.price_cents)})" if o.price_cents else "")
                         for o in g.options]}
            for g in item.option_groups
        ],
        "sold_out": (not item.available) or item.id in sold_out,
    }
    if item.includes:
        d["comes_with"] = item.comes_with()
    if item.description:
        d["description"] = item.description
    if item.tags:
        d["tags"] = item.tags
    return d


@dataclass
class OrderSession:
    menu: Menu
    on_submit: Callable[[dict], int] | None = None  # saves the order, returns order number
    on_transfer: Callable[[str], None] | None = None
    on_ask: Callable[[str], str | None] | None = None  # sends a question to the kitchen, returns the answer
    on_finish: Callable[[str], None] | None = None  # order submitted or call ended: the bot may hang up
    caller_phone: str | None = None  # from caller ID, digits only
    call_id: str | None = None
    sold_out: set[str] = field(default_factory=set)
    pickup_minutes: int | None = None  # owner override when busy
    last_order: dict | None = None  # this caller's previous order, if any
    now_fn: Callable[[], datetime] | None = None

    cart: Cart = field(init=False)
    _read_back_version: int = field(default=-1, init=False)
    _version: int = field(default=0, init=False)
    submitted_order: dict | None = field(default=None, init=False)
    transferred: bool = field(default=False, init=False)
    transfer_reason: str | None = field(default=None, init=False)
    ended_reason: str | None = field(default=None, init=False)
    tool_log: list[dict] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.cart = Cart(self.menu, sold_out=self.sold_out)

    # ---- dispatcher ---------------------------------------------------
    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> dict:
        args = dict(args or {})
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            result = {"ok": False, "error": f"Unknown tool '{name}'."}
        else:
            try:
                result = handler(**args)
            except OrderError as e:
                result = {"ok": False, "error": str(e)}
            except (TypeError, ValueError, AttributeError) as e:  # bad arguments from the LLM
                result = {"ok": False, "error": f"Bad arguments for {name}: {e}"}
        self.tool_log.append({"tool": name, "args": args, "result": result})
        return result

    def _now(self) -> datetime:
        return self.now_fn() if self.now_fn else self.menu.now()

    def _changed(self) -> None:
        self._version += 1

    def _guard_editable(self) -> None:
        if self.submitted_order is not None:
            raise OrderError("This order was already sent to the kitchen. Transfer to staff to change it.")
        if not self.menu.is_open(self._now()):
            raise OrderError("The restaurant is closed right now, so orders can't be taken. Give the hours.")

    # ---- tools --------------------------------------------------------
    def tool_search_menu(self, query: str) -> dict:
        matches = self.menu.search(query)
        if not matches:
            return {"ok": True, "matches": [],
                    "note": "Nothing on the menu matches. Tell the caller and ask what else they'd like."}
        return {"ok": True, "matches": [_describe_item(item, self.sold_out) for item, _ in matches]}

    def tool_add_items(self, items: list[dict]) -> dict:
        """Add several items at once. Each item: {query or item_id, quantity, size, options, notes}."""
        self._guard_editable()
        if not isinstance(items, list) or not items:
            raise OrderError("items must be a non-empty list.")
        if len(items) > MAX_BATCH:
            raise OrderError(f"Add at most {MAX_BATCH} items per call.")
        added, problems = [], []
        for spec in items:
            if not isinstance(spec, dict):
                problems.append({"item": spec, "error": "Each item must be an object."})
                continue
            try:
                line = self._add_one(spec)
                added.append(line.describe(with_includes=True))
            except OrderError as e:
                problems.append({"item": spec.get("query") or spec.get("item_id"), "error": str(e)})
        if added:
            self._changed()
        result = {"ok": bool(added), "added": added, "problems": problems,
                  "order_total_so_far": fmt_money(self.cart.total_cents)}
        if problems:
            result["instruction"] = "Ask the caller about each problem, one at a time, then call add_items again for those."
        if not added and problems:
            result["error"] = problems[0]["error"]
        return result

    def tool_add_item(self, item_id: str | None = None, query: str | None = None, quantity: int = 1,
                      size: str | None = None, options: list[str] | None = None, notes: str = "") -> dict:
        """Single-item add (kept for simple callers and tests). add_items is what the AI uses."""
        self._guard_editable()
        line = self._add_one({"item_id": item_id, "query": query, "quantity": quantity, "size": size,
                              "options": options, "notes": notes})
        self._changed()
        return {"ok": True, "added": line.describe(), "line_total": fmt_money(line.line_total_cents),
                "order_total_so_far": fmt_money(self.cart.total_cents)}

    def _add_one(self, spec: dict):
        item_id = spec.get("item_id")
        query = spec.get("query")
        if not item_id:
            if not query:
                raise OrderError("Each item needs a query (what the caller said) or an item_id.")
            item, candidates = self.menu.resolve(str(query))
            if item is None and candidates:
                names = "; ".join(
                    f"{c.name} ({c.price_label()}{', with ' + c.comes_with() if c.includes else ''})"
                    for c in candidates)
                raise OrderError(f"'{query}' could mean: {names}. Ask the caller which one.")
            if item is None:
                raise OrderError(f"'{query}' isn't on the menu. Tell the caller and ask what else they'd like.")
            item_id = item.id
        quantity = spec.get("quantity", 1)
        if quantity is None:
            quantity = 1
        if isinstance(quantity, str) and quantity.isdigit():
            quantity = int(quantity)
        size = spec.get("size")
        if not size and query:  # "a quart of wonton soup": the size is in the words
            item = self.menu.get(item_id)
            said = {SIZE_ALIASES.get(w, w) for w in normalize(str(query)).split()}
            for s in (item.sizes if item else []):
                if normalize(s.name) in said:
                    size = s.name
                    break
        return self.cart.add_item(item_id, size=size, options=spec.get("options") or [],
                                  quantity=quantity, notes=spec.get("notes") or "")

    def tool_update_item(self, line_index: int, quantity: int | None = None, size: str | None = None,
                         options: list[str] | None = None, notes: str | None = None) -> dict:
        self._guard_editable()
        line = self.cart.update_item(line_index, size=size, options=options, quantity=quantity, notes=notes)
        self._changed()
        return {"ok": True, "updated": line.describe(), "order_total_so_far": fmt_money(self.cart.total_cents)}

    def tool_remove_item(self, line_index: int) -> dict:
        self._guard_editable()
        line = self.cart.remove_item(line_index)
        self._changed()
        return {"ok": True, "removed": line.describe(), "order_total_so_far": fmt_money(self.cart.total_cents)}

    def tool_get_order(self) -> dict:
        if self.cart.lines:
            self._read_back_version = self._version
        return {"ok": True, **self.cart.summary(),
                "read_back": self.read_back_sentence(),
                "pickup_in_minutes": self._pickup_minutes(),
                "caller_id_phone": self.caller_phone,
                "instruction": "Say the read_back sentence word for word, then ask the caller to confirm."}

    def read_back_sentence(self) -> str:
        """The exact sentence the AI reads to the caller before confirming."""
        if not self.cart.lines:
            return "You don't have anything in your order yet."
        parts = []
        for line in self.cart.lines:
            qty = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}.get(line.quantity, str(line.quantity))
            text = f"{qty} {line.name}"
            if line.size:
                text += f", {line.size.lower()}"
            if line.options:
                text += f", {', '.join(o.lower() for o in line.options)}"
            if line.includes:
                text += f", with {' and '.join(i.lower() for i in line.includes)}"
            if line.notes:
                text += f", {line.notes}"
            parts.append(text)
        if len(parts) == 1:
            items = parts[0]
        else:
            items = "; ".join(parts[:-1]) + "; and " + parts[-1]
        return f"So that's {items}. Your total is {fmt_money(self.cart.total_cents)}."

    def tool_ask_restaurant(self, question: str) -> dict:
        """Ask the kitchen a quick question mid-call; the answer comes back into the conversation."""
        question = (question or "").strip()
        if not question:
            raise OrderError("Give the kitchen a short question to answer.")
        if self.on_ask is None:
            return {"ok": False, "error": "The kitchen can't be reached right now. Offer to transfer the caller."}
        answer = self.on_ask(question[:200])
        if not answer:
            return {"ok": False, "error": "No answer from the kitchen within the wait time. Apologize and offer to transfer."}
        return {"ok": True, "answer": answer, "instruction": "Tell the caller what the kitchen said, in your own words."}

    def tool_reorder_last(self) -> dict:
        """Put the caller's previous order in the cart (repeat customers)."""
        self._guard_editable()
        if not self.last_order:
            raise OrderError("No previous order is known for this caller.")
        added, problems = [], []
        for line in self.last_order.get("lines", []):
            try:
                new = self.cart.add_item(line["item_id"], size=line.get("size"), options=line.get("options") or [],
                                         quantity=line.get("quantity", 1), notes=line.get("notes") or "")
                added.append(new.describe())
            except OrderError as e:
                problems.append({"item": line.get("name"), "error": str(e)})
        if added:
            self._changed()
        return {"ok": bool(added), "added": added, "problems": problems,
                "order_total_so_far": fmt_money(self.cart.total_cents),
                **({"error": problems[0]["error"]} if not added and problems else {})}

    def tool_answer_question(self, topic: str) -> dict:
        r = self.menu.restaurant
        now = self._now()
        info = {
            "hours": self.menu.hours_text(),
            "open_now": "open" if self.menu.is_open(now) else "closed",
            "address": r.address,
            "phone": r.phone,
            "pickup_time": f"about {self._pickup_minutes()} minutes",
            "payment": r.payment_note,
            "delivery": "We only do pickup orders by phone.",
        }
        key = (topic or "").strip().lower().replace(" ", "_")
        if key not in info:
            return {"ok": True, "topics": list(info), "note": "Unknown topic; here are the ones available.",
                    **info}
        return {"ok": True, "topic": key, "answer": info[key]}

    def tool_submit_order(self, customer_name: str, phone: str | None = None) -> dict:
        if self.submitted_order is not None:
            raise OrderError("This order was already sent to the kitchen.")
        if not self.cart.lines:
            raise OrderError("The order is empty.")
        if self._read_back_version != self._version:
            raise OrderError("Call get_order and read the full order back to the caller before submitting.")
        name = (customer_name or "").strip()
        if not name:
            raise OrderError("Ask for a name for the order.")
        if phone:
            digits = clean_phone(phone)
            if digits is None:
                raise OrderError("That phone number needs 10 digits: ask the caller to repeat it.")
        else:
            digits = self.caller_phone
            if digits is None:
                raise OrderError("No caller ID on this call: ask for a 10-digit callback number.")
        pickup_at = self._now() + timedelta(minutes=self._pickup_minutes())
        order = {
            "restaurant_id": self.menu.restaurant.id,
            "customer_name": name[:40],
            "phone": digits,
            "lines": [line.to_dict() for line in self.cart.lines],
            "subtotal_cents": self.cart.subtotal_cents,
            "tax_cents": self.cart.tax_cents,
            "total_cents": self.cart.total_cents,
            "pickup_at": pickup_at.isoformat(timespec="minutes"),
            "call_id": self.call_id,
        }
        number = self.on_submit(order) if self.on_submit else 0
        order["order_number"] = number
        self.submitted_order = order
        if self.on_finish:
            self.on_finish("order placed")
        return {"ok": True, "order_number": number, "total": fmt_money(order["total_cents"]),
                "pickup_time": pickup_at.strftime("%I:%M %p").lstrip("0"),
                "instruction": "Tell the caller the order is in, the pickup time and total, then say goodbye."}

    def tool_end_call(self, reason: str = "") -> dict:
        """Politely end the call (prank, abuse, dead air, caller is done without ordering)."""
        self.ended_reason = reason or "ended by assistant"
        if self.on_finish:
            self.on_finish(self.ended_reason)
        return {"ok": True, "instruction": "Say one short goodbye sentence and nothing else; the call will end."}

    def tool_transfer_to_human(self, reason: str = "") -> dict:
        self.transferred = True
        self.transfer_reason = reason
        if self.on_transfer:
            self.on_transfer(reason)
        return {"ok": True, "instruction": "Say: 'One moment, I'm connecting you to the restaurant.' Then stop."}

    # ---- helpers ------------------------------------------------------
    def _pickup_minutes(self) -> int:
        return self.pickup_minutes or self.menu.restaurant.pickup_minutes

    def last_order_summary(self) -> str | None:
        """One line for the system prompt: 'Last order: 1 x General Tso's (Large), 2 x Egg Roll'."""
        if not self.last_order or not self.last_order.get("lines"):
            return None
        parts = []
        for line in self.last_order["lines"]:
            size = f" ({line['size']})" if line.get("size") else ""
            opts = f", {', '.join(line['options'])}" if line.get("options") else ""
            parts.append(f"{line.get('quantity', 1)} x {line['name']}{size}{opts}")
        return "; ".join(parts)

    def outcome(self) -> str:
        if self.submitted_order:
            return "order"
        if self.transferred:
            return "transferred"
        if self.ended_reason:
            return "ended"
        return "no_order"
