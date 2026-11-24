# OrderLine

An AI that answers a restaurant's phone and takes takeout orders. The order shows up on a tablet in the kitchen and prints on the receipt printer.

I grew up working at my family's Chinese takeout place. On a busy night the phone rings nonstop, and whoever picks up has to stop cooking or packing orders. This is my attempt to fix that.

## How it works

A call comes in through Twilio. Deepgram turns the caller's speech into text, Claude decides what to do, and Cartesia speaks the reply. Pipecat connects the pieces.

The main rule I followed: the AI never does math and never makes up facts. It only picks items off the menu. Prices, tax, sizes, hours and order limits are all plain Python (`app/order.py` and `app/session.py`), and an order can't go to the kitchen until it has been read back to the caller.

Some things that took a while to get right:

- People don't say dish names the way the menu spells them, and phone audio makes it worse. "Crab ragoon" and "general sauce chicken" still have to find the right dish, so there's a matcher in `app/menu.py` that compares how words sound.
- Speed. The whole order goes in with one tool call instead of one per dish, which cuts most of the waiting.
- Dishes that come three ways (regular, combo, special). The AI asks which one and gives the prices.
- Questions it can't answer, like allergies or substitutions. It sends the question to the kitchen tablet and waits for someone to tap an answer.

## Running it

This part needs no API keys:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn app.server:app --port 8000
```

Open http://localhost:8000/kitchen?restaurant=tops-china, then in a second terminal:

```bash
python -m scripts.demo_order
```

A test order should pop up on the kitchen screen.

To talk to the agent by typing, set `ANTHROPIC_API_KEY` and run `python -m scripts.chat --ignore-hours`.

Real phone calls need Twilio, Deepgram and Cartesia accounts. Copy `.env.example` to `.env`, fill it in, run `pip install -r requirements-voice.txt`, and follow the notes at the top of `app/voice/bot.py`.

## Where it stands

It works on real test calls and has about 100 automated tests. It isn't live at the restaurant yet. A few menu details (the exact tax rate and some prices) still have to be confirmed with the owner. They're listed in `data/menus/tops-china.REVIEW.md`.
