"""Text the caller a confirmation after their order is in (Twilio Messages API, no SDK needed).

Only the person who just placed the order gets a text, and only one, so this
stays clear of marketing/robotext rules. Best-effort: a failed text never
affects the order.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime
from typing import Awaitable, Callable

from .menu import fmt_money

PostFn = Callable[[str, dict, dict], Awaitable[tuple[int, str]]]  # (url, data, headers) -> (status, body)


def confirmation_text(order: dict, restaurant_name: str) -> str:
    try:
        pickup = datetime.fromisoformat(order["pickup_at"]).strftime("%I:%M %p").lstrip("0")
    except (KeyError, ValueError):
        pickup = "soon"
    items = "; ".join(
        f"{l['quantity']} {l['name']}" + (f" ({l['size']})" if l.get("size") else "") for l in order["lines"]
    )
    return (f"{restaurant_name}: order #{order['order_number']} received. {items}. "
            f"Total {fmt_money(order['total_cents'])}, pay at pickup. Ready about {pickup}.")[:320]


async def _aiohttp_post(url: str, data: dict, headers: dict) -> tuple[int, str]:
    import aiohttp

    async with aiohttp.ClientSession() as http:
        async with http.post(url, data=data, headers=headers) as resp:
            return resp.status, await resp.text()


async def send_sms(to_digits: str, body: str, post: PostFn = _aiohttp_post) -> bool:
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_SMS_FROM")  # the restaurant's Twilio number, E.164
    if not (sid and token and from_number):
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    auth = "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()
    status, _ = await post(url, {"To": f"+1{to_digits}", "From": from_number, "Body": body},
                           {"Authorization": auth})
    return 200 <= status < 300
