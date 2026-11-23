"""Kitchen and admin web server, plus the Twilio entry webhook.

Run:  uvicorn app.server:app --port 8000

Routes
  GET  /kitchen?restaurant=ID[&token=T]   kitchen tablet page (live orders + owner controls)
  GET  /api/orders?restaurant=ID          recent orders
  GET  /api/stream?restaurant=ID          server-sent events: active orders, recent transfers, open questions
  POST /api/questions/{id}/answer         {"answer": "..."}: staff answer to a mid-call question
  GET  /calls?restaurant=ID               owner page: recent calls, transcripts, stats
  GET  /api/calls?restaurant=ID           recent calls (no transcripts); /api/calls/{id} has the transcript
  GET  /api/stats?restaurant=ID&days=7    calls, orders, revenue, transfer rate
  POST /api/orders/{id}/status            {"status": "ready" | "picked_up" | ...}
  GET  /api/print-queue?restaurant=ID     confirmed orders not yet printed, with ticket text
  POST /api/orders/{id}/printed           mark a ticket printed
  GET  /api/menu?restaurant=ID            items (for the sold-out switches)
  GET|POST /api/settings?restaurant=ID    AI on/off, pickup minutes, sold-out items, blocked numbers
  POST /api/block?restaurant=ID           {"phone": "8045551234", "blocked": true} block/unblock a prank caller
  POST /twilio/voice?restaurant=ID        Twilio "a call comes in" webhook
  POST /twilio/fallback?restaurant=ID     Twilio fallback URL: ring the real phone

Built on Starlette (the framework FastAPI is built on), so it runs with only
starlette + uvicorn installed.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from xml.sax.saxutils import escape

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import twilio_security
from .menu import Menu, load_restaurant_menu
from .session import clean_phone
from .store import STATUSES, OrderStore
from .ticket import format_ticket

TEMPLATE = Path(__file__).resolve().parent / "templates" / "kitchen.html"
CALLS_TEMPLATE = Path(__file__).resolve().parent / "templates" / "calls.html"
ACTIVE = ["confirmed", "printed", "ready"]


def create_app(store: OrderStore | None = None, menu_loader=load_restaurant_menu,
               admin_token: str | None = None, twilio_auth_token: str | None = None,
               bot_ws_url: str | None = None, public_base_url: str | None = None,
               poll_seconds: float = 1.0) -> Starlette:
    store = store or OrderStore(os.getenv("ORDERLINE_DB", "data/orderline.db"))
    admin_token = admin_token if admin_token is not None else os.getenv("ADMIN_TOKEN", "")
    twilio_auth_token = (twilio_auth_token if twilio_auth_token is not None
                         else os.getenv("TWILIO_AUTH_TOKEN", ""))
    bot_ws_url = bot_ws_url if bot_ws_url is not None else os.getenv("BOT_WS_URL", "")
    public_base_url = (public_base_url if public_base_url is not None
                       else os.getenv("PUBLIC_BASE_URL", "")).rstrip("/")
    menus: dict[str, Menu] = {}

    def get_menu(restaurant_id: str) -> Menu | None:
        if restaurant_id not in menus:
            try:
                menus[restaurant_id] = menu_loader(restaurant_id)
            except FileNotFoundError:
                return None
        return menus[restaurant_id]

    def authorized(request: Request) -> bool:
        if not admin_token:
            return True
        given = request.headers.get("x-admin-token") or request.query_params.get("token")
        return given == admin_token

    def restaurant_or_error(request: Request) -> tuple[str, Menu | None, Response | None]:
        rid = request.query_params.get("restaurant", "")
        menu = get_menu(rid) if rid else None
        if menu is None:
            return rid, None, JSONResponse({"error": f"unknown restaurant '{rid}'"}, status_code=404)
        return rid, menu, None

    def guarded(handler):
        async def wrapper(request: Request):
            if not authorized(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await handler(request)
        return wrapper

    # ---- pages --------------------------------------------------------
    async def index(request: Request):
        return RedirectResponse("/kitchen?restaurant=tops-china")

    async def kitchen(request: Request):
        rid, menu, err = restaurant_or_error(request)
        if err:
            return err
        if not authorized(request):
            return HTMLResponse("Add ?token=... to the URL.", status_code=401)
        html = TEMPLATE.read_text(encoding="utf-8").replace("{{RESTAURANT_NAME}}", escape(menu.restaurant.name))
        return HTMLResponse(html)

    async def health(request: Request):
        return JSONResponse({"ok": True})

    # ---- orders -------------------------------------------------------
    async def list_orders(request: Request):
        rid, _, err = restaurant_or_error(request)
        if err:
            return err
        statuses = ACTIVE if request.query_params.get("active") == "1" else None
        return JSONResponse(store.list_orders(rid, statuses))

    async def set_status(request: Request):
        order_id = int(request.path_params["order_id"])
        body = await request.json()
        status = body.get("status")
        if status not in STATUSES:
            return JSONResponse({"error": f"status must be one of {STATUSES}"}, status_code=400)
        order = store.set_status(order_id, status)
        if order is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(order)

    def transfer_cards(rid: str) -> list[dict]:
        """Recent calls handed to a person, trimmed to what staff need to see."""
        return [{"call_id": c["call_id"], "at": c["ended_at"] or c["started_at"], "phone": c["caller_phone"],
                 "reason": c["transfer_reason"], "cart": c["cart"]} for c in store.recent_transfers(rid)]

    async def stream(request: Request):
        rid, _, err = restaurant_or_error(request)
        if err:
            return err

        async def events():
            last = None
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    break
                transfers = transfer_cards(rid)
                questions = store.open_questions(rid)
                marker = (store.last_change(rid) + "|" + ",".join(t["call_id"] for t in transfers)
                          + "|" + ",".join(str(q["id"]) for q in questions))
                if marker != last:
                    last = marker
                    idle = 0.0
                    payload = json.dumps({"orders": store.list_orders(rid, ACTIVE), "transfers": transfers,
                                          "questions": questions}, ensure_ascii=False)
                    yield f"event: orders\ndata: {payload}\n\n"
                elif idle >= 15:
                    idle = 0.0
                    yield ": keep-alive\n\n"
                await asyncio.sleep(poll_seconds)
                idle += poll_seconds

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def print_queue(request: Request):
        rid, menu, err = restaurant_or_error(request)
        if err:
            return err
        queue = list(reversed(store.list_orders(rid, ["confirmed"])))  # oldest first
        return JSONResponse([{"id": o["id"], "ticket": format_ticket(o, menu.restaurant.name)} for o in queue])

    async def mark_printed(request: Request):
        order = store.set_status(int(request.path_params["order_id"]), "printed")
        if order is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(order)

    # ---- owner controls ---------------------------------------------
    async def menu_items(request: Request):
        rid, menu, err = restaurant_or_error(request)
        if err:
            return err
        return JSONResponse([{"id": i.id, "name": i.name, "category": i.category} for i in menu.items])

    async def settings(request: Request):
        rid, menu, err = restaurant_or_error(request)
        if err:
            return err
        if request.method == "POST":
            body = await request.json()
            changes = {}
            if "ai_enabled" in body:
                changes["ai_enabled"] = bool(body["ai_enabled"])
            if "pickup_minutes" in body:
                value = body["pickup_minutes"]
                if value is not None and not (isinstance(value, int) and 5 <= value <= 120):
                    return JSONResponse({"error": "pickup_minutes must be 5-120 or null"}, status_code=400)
                changes["pickup_minutes"] = value
            if "sold_out" in body:
                ids = {i.id for i in menu.items}
                bad = [x for x in body["sold_out"] if x not in ids]
                if bad:
                    return JSONResponse({"error": f"unknown items {bad}"}, status_code=400)
                changes["sold_out"] = list(body["sold_out"])
            store.update_settings(rid, **changes)
        result = store.get_settings(rid)
        result["default_pickup_minutes"] = menu.restaurant.pickup_minutes
        return JSONResponse(result)

    # ---- mid-call questions -------------------------------------------
    async def answer_question(request: Request):
        body = await request.json()
        answer = (body.get("answer") or "").strip()
        if not answer:
            return JSONResponse({"error": "answer is empty"}, status_code=400)
        ok = store.answer_question(int(request.path_params["question_id"]), answer)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 409)

    async def list_questions(request: Request):
        rid, _, err = restaurant_or_error(request)
        if err:
            return err
        return JSONResponse(store.open_questions(rid))

    # ---- calls and stats ----------------------------------------------
    async def list_calls(request: Request):
        rid, _, err = restaurant_or_error(request)
        if err:
            return err
        outcome = request.query_params.get("outcome") or None
        calls = store.list_calls(rid, limit=100, outcome=outcome)
        slim = [{k: v for k, v in c.items() if k not in ("transcript", "tool_log")} for c in calls]
        return JSONResponse(slim)

    async def get_call(request: Request):
        call = store.get_call(request.path_params["call_id"])
        if call is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(call)

    async def flag_call(request: Request):
        body = await request.json()
        store.flag_call(request.path_params["call_id"], bool(body.get("flagged", True)))
        return JSONResponse({"ok": True})

    async def block_number(request: Request):
        rid, _, err = restaurant_or_error(request)
        if err:
            return err
        body = await request.json()
        phone = clean_phone(str(body.get("phone", "")))
        if not phone:
            return JSONResponse({"error": "phone must be a 10-digit number"}, status_code=400)
        settings = store.block_number(rid, phone, bool(body.get("blocked", True)))
        return JSONResponse({"ok": True, "blocked_numbers": settings["blocked_numbers"]})

    async def stats(request: Request):
        rid, _, err = restaurant_or_error(request)
        if err:
            return err
        days = int(request.query_params.get("days", "7"))
        return JSONResponse(store.stats(rid, days=max(1, min(days, 365))))

    async def calls_page(request: Request):
        rid, menu, err = restaurant_or_error(request)
        if err:
            return err
        if not authorized(request):
            return HTMLResponse("Add ?token=... to the URL.", status_code=401)
        html = CALLS_TEMPLATE.read_text(encoding="utf-8").replace("{{RESTAURANT_NAME}}", escape(menu.restaurant.name))
        return HTMLResponse(html)

    # ---- Twilio -------------------------------------------------------
    async def twilio_form(request: Request) -> tuple[dict, bool]:
        form = dict(await request.form())
        if not twilio_auth_token:
            return form, True  # local development without verification
        url = (public_base_url + request.url.path + (f"?{request.url.query}" if request.url.query else "")
               if public_base_url else str(request.url))
        ok = twilio_security.is_valid(twilio_auth_token, url, form,
                                      request.headers.get("x-twilio-signature"))
        return form, ok

    def dial(menu: Menu) -> Response:
        twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial>{escape(menu.restaurant.phone)}</Dial></Response>'
        return Response(twiml, media_type="application/xml")

    async def twilio_voice(request: Request):
        rid, menu, err = restaurant_or_error(request)
        if err:
            return err
        form, ok = await twilio_form(request)
        if not ok:
            return Response("invalid signature", status_code=403)
        if not store.get_settings(rid)["ai_enabled"] or not bot_ws_url:
            return dial(menu)
        twiml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
                 f'<Stream url="{escape(bot_ws_url)}">'
                 f'<Parameter name="restaurant" value="{escape(rid)}"/>'
                 f'<Parameter name="from_number" value="{escape(form.get("From", ""))}"/>'
                 f'<Parameter name="to_number" value="{escape(form.get("To", ""))}"/>'
                 '</Stream></Connect></Response>')
        return Response(twiml, media_type="application/xml")

    async def twilio_fallback(request: Request):
        rid, menu, err = restaurant_or_error(request)
        if err:
            return err
        return dial(menu)

    routes = [
        Route("/", index),
        Route("/health", health),
        Route("/kitchen", kitchen),
        Route("/api/orders", guarded(list_orders)),
        Route("/api/orders/{order_id:int}/status", guarded(set_status), methods=["POST"]),
        Route("/api/orders/{order_id:int}/printed", guarded(mark_printed), methods=["POST"]),
        Route("/api/stream", guarded(stream)),
        Route("/api/print-queue", guarded(print_queue)),
        Route("/api/menu", guarded(menu_items)),
        Route("/api/settings", guarded(settings), methods=["GET", "POST"]),
        Route("/api/questions", guarded(list_questions)),
        Route("/api/questions/{question_id:int}/answer", guarded(answer_question), methods=["POST"]),
        Route("/api/calls", guarded(list_calls)),
        Route("/api/calls/{call_id}", guarded(get_call)),
        Route("/api/calls/{call_id}/flag", guarded(flag_call), methods=["POST"]),
        Route("/api/block", guarded(block_number), methods=["POST"]),
        Route("/api/stats", guarded(stats)),
        Route("/calls", calls_page),
        Route("/twilio/voice", twilio_voice, methods=["POST"]),
        Route("/twilio/fallback", twilio_fallback, methods=["POST", "GET"]),
    ]
    application = Starlette(routes=routes)
    application.state.store = store
    return application


class _LazyApp:
    """Build the real app on first request so importing this module has no side effects."""

    def __init__(self):
        self._app = None

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = _LazyApp()
