# OrderLine — AI phone ordering for small restaurants

An AI answers a restaurant's phone, takes the takeout order by voice, and sends
it to a kitchen tablet and receipt printer. First restaurant: Top's China.

The full design (architecture, costs, legal, roadmap) is in the
"Restaurant AI Phone Agent — Full System Design" doc.

## What works today

| Part | Status |
| --- | --- |
| Menu, order engine, pricing, tax, hours, order caps | Done, unit-tested |
| Speech-robust menu matching ("general sauce chicken", "crab ragoon") | Done, unit-tested |
| The 9 tools the AI uses (`OrderSession`), incl. one-call `add_items` and `reorder_last` | Done, unit-tested |
| Claude tool-use loop (text version of the agent) | Done, tested with a fake Claude; needs an API key to run for real |
| Kitchen tablet page: live orders, transfer banners, owner controls | Done, tested against a real server |
| Calls page: transcripts, flagging, 7/30-day stats | Done, tested |
| Print agent (thermal printer or terminal) | Done, tested printing to terminal |
| Twilio webhook (AI on/off, fallback, signature check) | Done, tested |
| SMS confirmation to the caller | Done, tested with a fake Twilio; off by default |
| Voice bot (`app/voice/bot.py`): keyterm boosting, instant greeting, caller ID lookup, latency metrics, auto hang-up, transfer, silence/time-limit/blocklist troll handling | Written against Pipecat 1.15; tested on real calls |
| Evals (10 simulated callers, order accuracy) | Done; needs an API key to run |
| Real Top's China menu: 210 items, 20 sections, real pickup prices, phone, hours, from the restaurant's website | Done; 12 things to confirm with the owner (see REVIEW.md) |
| Menu importers: the restaurant's website (`import_site_menu.py`) or an Uber Eats page (`import_menu.py`) | Done, tested |
| Three-way dishes (regular / combo / special): the AI asks which, with prices | Done, tested |
| Combo swaps (white rice, shrimp roll…) and fried-rice-instead-of-white-rice | Done, tested; upcharges are guesses to confirm |
| Mid-call "ask the kitchen": question on the tablet, one-tap/typed/spoken answer back into the call | Done, tested on the server and in a browser; needs a real call to prove the voice side |
| Word-for-word read-back before confirming | Done, tested |
| Owner guide in English and Chinese (`docs/OWNER_GUIDE.md`) | Done |
| Docker / Fly.io deployment files | Done; not deployed yet |

97 automated tests: `pytest -q`

## Why it's fast

Most of a voice agent's delay is the number of model calls per order. This design keeps it to about four
for a typical call: one `add_items` for everything the caller said, one `get_order` for the read-back,
one `submit_order`, plus the greeting. Older designs do a `search_menu` + `add_item` pair per dish.
On top of that: the menu is in a cached system prompt, Deepgram is given every dish name as a keyterm so
it doesn't mishear "Tso's", and repeat callers can say "the usual" (one `reorder_last` call).
Every turn's latency (caller stops talking → bot starts talking) is recorded per call and shown on the calls page.

## Folder layout

```
orderline/
  app/
    menu.py            menu + restaurant models, fuzzy search, opening hours
    order.py           the cart: every pricing and validation rule
    session.py         one call = one OrderSession; the 8 tools the AI can call
    store.py           SQLite: orders, owner settings, call logs
    ticket.py          kitchen ticket text
    server.py          kitchen page, calls page, owner controls, live stream, stats, Twilio webhook
    notify.py          SMS confirmation to the caller
    twilio_security.py checks webhooks really come from Twilio
    templates/kitchen.html  the tablet screen
    templates/calls.html    owner's call log, transcripts and stats
    agent/
      tools.py         tool definitions Claude sees
      prompts.py       system prompt + greeting
      text_agent.py    Claude tool-use loop (typed version of the phone call)
    voice/
      bot.py           live phone bot: Twilio + Deepgram + Claude + Cartesia via Pipecat
  data/menus/tops-china.json         Top's China real menu (generated; don't hand-edit)
  data/menus/tops-china.rules.json   naming, aliases, swaps and confirmations layered on the website data
  data/menus/tops-china.REVIEW.md    checklist of guesses to confirm with the owner
  data/menus/sample-china.json       made-up menu used by the tests and evals
  data/sources/tops-china-website.txt    the restaurant's own menu page (the source of truth)
  data/sources/tops-china-ubereats.txt   the Uber Eats page (fallback importer; prices there are marked up)
  docs/OWNER_GUIDE.md                how the restaurant uses it (English + 中文)
  scripts/
    demo_order.py      place a test order with no API keys
    chat.py            talk to the agent by typing (needs ANTHROPIC_API_KEY)
    print_agent.py     runs at the restaurant, prints tickets
    check_menu.py      lint a menu file; says NOT READY until every guess is confirmed
    import_site_menu.py  restaurant website text + rules file -> menu JSON + review checklist
    import_menu.py       same, from an Uber Eats page
  Dockerfile, docker-compose.yml, fly.toml   deployment
  evals/
    cases.json         scripted callers + expected orders
    run_evals.py       scores order accuracy
  tests/               pytest suite
```

## Step 1 — run it with no API keys (5 minutes)

```bash
cd orderline
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest -q                                               # 48 passed

uvicorn app.server:app --port 8000                      # terminal 1
# open http://localhost:8000/kitchen?restaurant=tops-china
python -m scripts.demo_order                            # terminal 2: order pops up on the screen
python -m scripts.print_agent --once                    # prints the ticket in the terminal
```

## Step 2 — talk to the AI by typing

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY
export $(grep -v '^#' .env | xargs)
python -m scripts.chat --ignore-hours
```

Type like a caller ("can I get general chicken and two egg rolls"). Type `tools`
to see what the AI called and `cart` to see the order. Confirmed orders show up
on the kitchen page.

Then measure accuracy: `python -m evals.run_evals`. Add a case to
`evals/cases.json` every time the agent gets something wrong.

## Step 3 — real phone calls

1. Accounts: Twilio (buy a local number), Deepgram, Cartesia (pick a voice id), Anthropic. Put keys in `.env`.
2. `pip install -r requirements-voice.txt`
3. An ngrok tunnel to the bot with a fixed domain (free ngrok accounts get one):
   `ngrok http 127.0.0.1:7860 --url=<your-domain>.ngrok-free.app`
4. Start the bot: `python -m app.voice.bot -t twilio --host 0.0.0.0 -x <your-domain>.ngrok-free.app`
   (`--host 0.0.0.0` matters: without it the bot listens on IPv6 only and ngrok returns 502.)
5. Start the kitchen server: `uvicorn app.server:app --port 8000` and open `http://localhost:8000/kitchen?restaurant=tops-china`.
6. In Twilio, on your number → Voice Configuration:
   - A call comes in: Webhook, `https://<your-domain>.ngrok-free.app/`, HTTP POST (the bot's dev runner answers with TwiML)
   - For production, point it at the kitchen server instead: `https://<kitchen-host>/twilio/voice?restaurant=tops-china`
     with fallback `https://<kitchen-host>/twilio/fallback?restaurant=tops-china`, so the AI on/off switch and
     caller ID pass-through work without the Twilio REST lookup.
7. Call the number. Every call shows up on `/calls?restaurant=tops-china` with its transcript and latency.

Troll handling: silence gets "Still there?" then a hang-up (`CALLER_IDLE_SECONDS`, default 12), calls are capped at
`MAX_CALL_SECONDS` (default 480), the model can `end_call` on abuse, and the Calls page has **Block this number**.

If Pipecat's API has changed since this was written, run `pipecat init`
(choose phone, Twilio, Deepgram, Anthropic, Cartesia) to generate a fresh bot
and copy over the tool functions and `OrderSession` wiring from `bot.py`.

## Deploying

`docker compose up --build` runs both the kitchen server (port 8000) and the bot (port 7860) sharing one
database. `fly.toml` does the same on Fly.io in the Virginia region; the comments at the top list the commands.

## Before a real restaurant uses it

- Go through `data/menus/tops-china.REVIEW.md` with the owner (12 items: the tax %, hours, swap
  upcharges, the "From" prices on wings). Fix them in `tops-china.rules.json` and re-run
  `python -m scripts.import_site_menu --source data/sources/tops-china-website.txt --rules data/menus/tops-china.rules.json`.
  When the website changes, copy its menu page over `data/sources/tops-china-website.txt` and re-run.
  `python -m scripts.check_menu tops-china` must say "OK: ready for live calls".
- Testing at night: put `ORDERLINE_ALWAYS_OPEN=1` in `.env` so the AI takes orders after closing; remove it before the pilot.
- Transfers dial the restaurant's real line (`phone` in the menu JSON). Don't test transfers at 1 AM.
- Print `docs/OWNER_GUIDE.md` and leave it by the tablet.
- Set `ADMIN_TOKEN` and open the kitchen page as `/kitchen?restaurant=tops-china&token=...`.
- Set `TWILIO_AUTH_TOKEN` so fake webhook calls are rejected.
- Run the evals and a week of shadow calls with someone checking every ticket.
