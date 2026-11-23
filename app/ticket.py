"""Kitchen ticket text for an 80 mm thermal printer (about 42 characters wide)."""

from __future__ import annotations

from datetime import datetime

from .menu import fmt_money

WIDTH = 42


def _pickup_label(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return iso


def format_ticket(order: dict, restaurant_name: str) -> str:
    rule = "-" * WIDTH
    header = f"{restaurant_name.upper()}  #{order['order_number']:04d}"
    out = [
        header,
        "PHONE ORDER",
        f"Pickup {_pickup_label(order['pickup_at'])}   Name: {order['customer_name']}",
        f"Phone {order['phone'][:3]}-{order['phone'][3:6]}-{order['phone'][6:]}",
        rule,
    ]
    for line in order["lines"]:
        size = f" ({line['size']})" if line.get("size") else ""
        out.append(f"{line['quantity']:<3}{line['name']}{size}")
        if line.get("kitchen_name"):
            out.append(f"     {line['kitchen_name']}")
        for opt in line.get("options", []):
            out.append(f"     - {opt}")
        if line.get("includes"):
            out.append(f"     w/ {', '.join(line['includes'])}")
        if line.get("notes"):
            out.append(f"     ** {line['notes']}")
    out += [
        rule,
        f"Subtotal {fmt_money(order['subtotal_cents']):>{WIDTH - 9}}",
        f"Tax {fmt_money(order['tax_cents']):>{WIDTH - 4}}",
        f"TOTAL {fmt_money(order['total_cents']):>{WIDTH - 6}}",
        "PAY AT PICKUP",
    ]
    return "\n".join(out) + "\n"
