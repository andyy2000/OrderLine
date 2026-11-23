"""The live phone bot: Twilio audio -> Deepgram -> Claude (+ order tools) -> Cartesia -> Twilio.

Written against Pipecat 1.15 (pipecat-ai >= 1.15), following Pipecat's own
twilio-chatbot/inbound, function-calling-anthropic and detect-user-idle examples.

Run locally (two terminals):
    ngrok http 127.0.0.1:7860 --url=<your-ngrok-host>
    python -m app.voice.bot -t twilio --host 0.0.0.0 -x <your-ngrok-host>

Then set BOT_WS_URL=wss://<your-ngrok-host>/ws for the kitchen server, and point
your Twilio number's "A call comes in" webhook at the kitchen server's
/twilio/voice?restaurant=tops-china (see README).

The order logic is NOT in this file. Every tool below is a thin wrapper that
calls OrderSession.call_tool(), which is fully unit-tested.

Speed and accuracy features in this file:
- Deepgram keyterm boosting with every dish name and alias from the menu.
- One `add_items` call per order instead of one search + one add per dish.
- Repeat callers: their last order is looked up by caller ID before the greeting.
- Latency (caller stops talking -> bot starts talking) is measured on every turn
  with Pipecat's UserBotLatencyObserver and saved with the call.
- The greeting is spoken straight from text (no LLM round trip to start the call).
- Caller ID is fetched from Twilio, so the caller is never asked for a number.
- The call hangs up itself after the goodbye, and transfers after "one moment".
- Trolls: "Still there?" after silence then hang up, a hard time cap, and a
  per-restaurant blocklist (Calls page -> "Block this number").
"""

from __future__ import annotations

import base64
import os
import statistics

import asyncio
import time

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.llm.schemas.direct_function import tool_options
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame, FunctionCallResultProperties, LLMRunFrame, TTSSpeakFrame
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from app.agent.prompts import build_system_prompt, greeting
from app.menu import load_restaurant_menu
from app.notify import confirmation_text, send_sms
from app.session import OrderSession, clean_phone
from app.store import OrderStore

load_dotenv(override=True)

STORE = OrderStore(os.getenv("ORDERLINE_DB", "data/orderline.db"))
DEFAULT_RESTAURANT = os.getenv("DEFAULT_RESTAURANT", "tops-china")
SEND_SMS = os.getenv("SEND_SMS_CONFIRMATIONS", "0") == "1"
ALWAYS_OPEN = os.getenv("ORDERLINE_ALWAYS_OPEN", "0") == "1"  # testing after hours
ASK_TIMEOUT_S = int(os.getenv("ASK_KITCHEN_TIMEOUT", "30"))
IDLE_SECONDS = float(os.getenv("CALLER_IDLE_SECONDS", "12"))      # silence before "still there?"
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "480"))       # hard cap per call


# ---- tools: thin wrappers around OrderSession.call_tool ------------------
async def _run(params: FunctionCallParams, name: str, args: dict) -> None:
    session: OrderSession = params.app_resources
    args = {k: v for k, v in args.items() if v is not None}
    result = session.call_tool(name, args)
    logger.info(f"tool {name}({args}) -> {result}")
    await params.result_callback(result)


async def add_items(params: FunctionCallParams, items: list[dict]):
    """Add everything the caller just asked for in ONE call. Pass each item as the caller said it (query); the system matches it to the menu. Each result is added or a problem to ask about (missing size, required choice, ambiguous name, not on menu).

    Args:
        items: List of objects with keys: query (the dish as the caller said it), item_id (instead of query if known), quantity (default 1), size (e.g. "large", "quart"), options (list of choices the caller said), notes (short special instruction).
    """
    await _run(params, "add_items", {"items": items})


async def search_menu(params: FunctionCallParams, query: str):
    """Look up menu items when the caller asks what's available or about a dish, or add_items said a name was ambiguous. Not needed before add_items.

    Args:
        query: What the caller asked about, e.g. "soups" or "general chicken".
    """
    await _run(params, "search_menu", {"query": query})


async def update_item(params: FunctionCallParams, line_index: int, quantity: int | None = None,
                      size: str | None = None, options: list[str] | None = None, notes: str | None = None):
    """Change a line already in the order. Only pass the fields that change.

    Args:
        line_index: Line number from get_order, starting at 0.
        quantity: New quantity.
        size: New size.
        options: Full new list of options for this line.
        notes: New special instruction.
    """
    await _run(params, "update_item", {"line_index": line_index, "quantity": quantity, "size": size,
                                       "options": options, "notes": notes})


async def remove_item(params: FunctionCallParams, line_index: int):
    """Remove a line from the order.

    Args:
        line_index: Line number from get_order, starting at 0.
    """
    await _run(params, "remove_item", {"line_index": line_index})


async def get_order(params: FunctionCallParams):
    """Get the current order with line numbers and the total. Call before reading the order back; submit_order only works after it."""
    await _run(params, "get_order", {})


async def reorder_last(params: FunctionCallParams):
    """Put this caller's previous order in the cart. Only when the system prompt says a last order exists and the caller wants the same thing again."""
    await _run(params, "reorder_last", {})


async def answer_question(params: FunctionCallParams, topic: str):
    """Get facts about the restaurant. Never answer these from memory.

    Args:
        topic: One of hours, open_now, address, phone, pickup_time, payment, delivery.
    """
    await _run(params, "answer_question", {"topic": topic})


async def submit_order(params: FunctionCallParams, customer_name: str, phone: str | None = None):
    """Send the confirmed order to the kitchen. Only after reading the order back and the caller saying yes.

    Args:
        customer_name: Name for the order.
        phone: Only if there is no caller ID; otherwise leave it out and the caller ID is used.
    """
    await _run(params, "submit_order", {"customer_name": customer_name, "phone": phone})


@tool_options(cancel_on_interruption=False, timeout_secs=ASK_TIMEOUT_S + 5)
async def ask_restaurant(params: FunctionCallParams, question: str):
    """Ask the kitchen staff a short question the menu can't answer (substitutions, allergies, spice, timing for a big order). Don't say anything first; the caller is told to hold automatically. The staff's answer comes back as the result.

    Args:
        question: One short question for the kitchen, ideally yes/no.
    """
    session: OrderSession = params.app_resources
    qid = STORE.ask_question(session.menu.restaurant.id, session.call_id, question, timeout_s=ASK_TIMEOUT_S)
    logger.info(f"asked kitchen #{qid}: {question}")
    await params.result_callback(
        {"status": "sent", "instruction": "Wait silently; the kitchen's answer arrives as the next result."},
        properties=FunctionCallResultProperties(is_final=False),
    )
    answer = None
    for _ in range(ASK_TIMEOUT_S * 2):
        await asyncio.sleep(0.5)
        answer = STORE.get_answer(qid)
        if answer:
            break
    result = ({"ok": True, "answer": answer, "instruction": "Tell the caller what the kitchen said."} if answer
              else {"ok": False, "error": "No answer from the kitchen in time. Apologize and offer to transfer."})
    session.tool_log.append({"tool": "ask_restaurant", "args": {"question": question}, "result": result})
    await params.result_callback(result)


async def end_call(params: FunctionCallParams, reason: str):
    """Politely end the call: prank or abusive caller, dead air, or the caller is done and doesn't want to order. Say a one-line goodbye after calling it.

    Args:
        reason: Short reason the call is ending.
    """
    await _run(params, "end_call", {"reason": reason})


async def transfer_to_human(params: FunctionCallParams, reason: str):
    """Connect the caller to restaurant staff (asked for a person, upset, catering, or not understood twice).

    Args:
        reason: Short reason for the transfer.
    """
    await _run(params, "transfer_to_human", {"reason": reason})


TOOLS = [add_items, search_menu, update_item, remove_item, get_order, reorder_last, answer_question,
         ask_restaurant, submit_order, transfer_to_human, end_call]


async def fetch_caller_number(call_sid: str | None) -> str | None:
    """Caller ID from Twilio's REST API (the plain Media Streams handshake doesn't carry it)."""
    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    if not (sid and token and call_sid):
        return None
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls/{call_sid}.json"
    auth = "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url, headers={"Authorization": auth}, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    return (await resp.json()).get("from")
                logger.warning(f"caller lookup failed {resp.status}")
    except Exception as e:
        logger.warning(f"caller lookup error: {e}")
    return None


# ---- transfer: redirect the live call to the restaurant's phone ----------
async def redirect_call(call_sid: str, number: str) -> None:
    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    if not (sid and token and call_sid):
        logger.error("Cannot transfer: missing Twilio credentials or call sid")
        return
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls/{call_sid}.json"
    twiml = f'<Response><Pause length="1"/><Dial>{number}</Dial></Response>'
    auth = "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()
    async with aiohttp.ClientSession() as http:
        async with http.post(url, data={"Twiml": twiml}, headers={"Authorization": auth}) as resp:
            if resp.status >= 300:
                logger.error(f"Twilio transfer failed {resp.status}: {await resp.text()}")


def summarize_latencies(samples: list[float]) -> dict:
    if not samples:
        return {"turns": 0}
    ordered = sorted(samples)
    p90 = ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]
    return {"turns": len(samples), "latency_median_s": round(statistics.median(samples), 3),
            "latency_p90_s": round(p90, 3), "latency_max_s": round(ordered[-1], 3)}


# ---- the pipeline ---------------------------------------------------------
async def run_bot(transport, runner_args: RunnerArguments, restaurant_id: str,
                  call_id: str | None, caller: str | None):
    menu = load_restaurant_menu(restaurant_id)
    settings = STORE.get_settings(restaurant_id)
    caller_digits = clean_phone(caller or "")
    call_id = call_id or "unknown"
    STORE.start_call(call_id, restaurant_id, caller_digits)

    state = {"transfer": None, "hung_up": False, "idle_warned": False}
    latencies: list[float] = []
    started = time.monotonic()
    is_open = ALWAYS_OPEN or menu.is_open()
    now_fn = (lambda: menu.now().replace(hour=18, minute=0)) if ALWAYS_OPEN else None
    blocked = bool(caller_digits) and caller_digits in set(settings.get("blocked_numbers", []))
    finish_watchdog: list[asyncio.Task] = []

    def on_finish(reason: str) -> None:
        # The bot hangs up after its goodbye; if the model says nothing, this does it anyway.
        async def fallback():
            await asyncio.sleep(10)
            await hang_up(f"{reason} (watchdog)")
        finish_watchdog.append(asyncio.create_task(fallback()))

    last_order = STORE.last_order_for_phone(restaurant_id, caller_digits)
    session = OrderSession(
        menu=menu,
        on_submit=STORE.save_order,
        on_transfer=lambda reason: state.update(transfer=reason or "transfer"),
        on_finish=on_finish,
        caller_phone=caller_digits,
        call_id=call_id,
        sold_out=set(settings["sold_out"]),
        pickup_minutes=settings["pickup_minutes"],
        last_order=last_order,
        now_fn=now_fn,
    )

    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(
            model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
            keyterm=menu.stt_keyterms(),   # boosts dish names the model would otherwise mishear
            numerals=True,                 # "eight oh four" -> 804, for phone numbers
            smart_format=True,
        ),
    )
    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(
            model=os.getenv("ORDERLINE_MODEL", "claude-haiku-5-5"),
            system_instruction=build_system_prompt(
                menu, voice=True, last_order=session.last_order_summary(),
                last_name=(last_order or {}).get("customer_name"), has_caller_id=bool(caller_digits)),
        ),
    )
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(voice=os.environ["CARTESIA_VOICE_ID"]),
    )

    context = LLMContext(tools=TOOLS)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer(),
                                                     user_idle_timeout=IDLE_SECONDS),
    )
    pipeline = Pipeline([
        transport.input(), stt, user_aggregator, llm, tts, transport.output(), assistant_aggregator,
    ])
    latency_observer = UserBotLatencyObserver()
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000,
                              enable_metrics=True, enable_usage_metrics=True),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
        app_resources=session,
        observers=[latency_observer],
    )
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint, force_gc=True)
    await runner.add_workers(worker)

    async def hang_up(reason: str) -> None:
        if not state["hung_up"]:
            state["hung_up"] = True
            logger.info(f"hanging up: {reason}")
            await worker.queue_frames([EndFrame()])

    async def say_and_end(text: str, reason: str) -> None:
        # Spoken text lands in the context as an assistant turn; on_turn_done hangs up after it.
        session.ended_reason = reason
        await tts.queue_frame(TTSSpeakFrame(text))
        on_finish(reason)

    @latency_observer.event_handler("on_latency_measured")
    async def on_latency(observer, latency_seconds: float):
        latencies.append(latency_seconds)
        logger.info(f"turn latency {latency_seconds:.2f}s")

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, client):
        if blocked:
            await say_and_end("Please call the restaurant directly. Goodbye.", "blocked number")
            return
        # Speak the greeting straight away (no LLM round trip); it is appended to the context as spoken.
        context.add_message({"role": "user", "content": "(call connected)"})
        await tts.queue_frame(TTSSpeakFrame(greeting(menu, is_open)))

    @llm.event_handler("on_function_calls_started")
    async def on_tools_started(service, function_calls):
        # Hide the slow tool's round trip behind a filler; fast tools get none (the model says "Got it").
        if any(fc.function_name == "ask_restaurant" for fc in function_calls):
            await tts.queue_frame(TTSSpeakFrame("One sec, checking with the kitchen."))

    @user_aggregator.event_handler("on_user_turn_idle")
    async def on_idle(aggregator):
        if state["hung_up"] or session.ended_reason:
            return
        if not state["idle_warned"]:
            state["idle_warned"] = True
            await tts.queue_frame(TTSSpeakFrame("Still there?"))
        else:
            await say_and_end("I'll let you go. Call back anytime. Goodbye.", "silence")

    @user_aggregator.event_handler("on_user_turn_started")
    async def on_user_spoke(aggregator, *_):
        state["idle_warned"] = False

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_turn_done(*_):
        # Runs after the bot finished speaking, so the caller hears the whole sentence first.
        if state["hung_up"]:
            return
        if state["transfer"]:
            state["hung_up"] = True
            STORE.log_call(call_id, restaurant_id, "transferred", cart=session.cart.summary(),
                           transfer_reason=state["transfer"], caller_phone=caller_digits)
            await redirect_call(call_id, menu.restaurant.phone)
        elif session.submitted_order:
            if SEND_SMS:
                order = session.submitted_order
                try:
                    await send_sms(order["phone"], confirmation_text(order, menu.restaurant.name))
                except Exception as e:  # never let a text break the call
                    logger.warning(f"SMS failed: {e}")
            await hang_up("order placed")
        elif session.ended_reason:
            await hang_up(session.ended_reason)
        elif time.monotonic() - started > MAX_CALL_SECONDS:
            await say_and_end("Sorry, I have to let you go. Please call back. Goodbye.", "time limit")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(transport, client):
        await runner.cancel()

    try:
        await runner.run()
    finally:
        for task in finish_watchdog:
            task.cancel()
        # Always record the call, whether the caller hung up or we did.
        STORE.log_call(call_id, restaurant_id, session.outcome(), transcript=context.get_messages(),
                       tool_log=session.tool_log, cart=session.cart.summary(),
                       transfer_reason=session.transfer_reason, caller_phone=caller_digits,
                       metrics={**summarize_latencies(latencies),
                                "call_seconds": round(time.monotonic() - started),
                                "ended_reason": session.ended_reason})


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(
        runner_args, {"twilio": lambda: FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True)},
    )
    call_data = getattr(runner_args, "call_data", None)
    body = (call_data.body if call_data and call_data.body else {}) or {}
    restaurant_id = body.get("restaurant") or DEFAULT_RESTAURANT
    call_sid = call_data.call_id if call_data else None
    caller = body.get("from_number") or await fetch_caller_number(call_sid)
    await run_bot(transport, runner_args, restaurant_id, call_sid, caller)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
