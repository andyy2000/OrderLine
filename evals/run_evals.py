"""Run scripted calls against the agent and score order accuracy.

A second Claude plays the caller from each case's description; the agent under
test takes the order with the real tools. A case passes only if the final
submitted order matches the expected lines exactly (or the call was
transferred when a transfer is expected).

    export ANTHROPIC_API_KEY=...
    python -m evals.run_evals            # all cases
    python -m evals.run_evals --case changes-mind -v
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from app.agent.text_agent import TextAgent
from app.menu import load_restaurant_menu
from app.session import OrderSession

CASES = Path(__file__).with_name("cases.json")
MAX_TURNS = 14
CALLER_MODEL = "claude-haiku-5-5"


def normalize_lines(lines: list[dict]) -> list[tuple]:
    return sorted((l["item_id"], l.get("size"), tuple(sorted(l.get("options", []))), l["quantity"])
                  for l in lines)


def caller_reply(client, persona: str, transcript: list[tuple[str, str]]) -> str:
    convo = "\n".join(f"{who}: {text}" for who, text in transcript)
    msg = client.messages.create(
        model=CALLER_MODEL, max_tokens=120,
        system=("You are a customer calling a Chinese takeout restaurant. Stay in character, reply with "
                "ONLY what you say out loud, one or two short sentences. When your order is confirmed "
                "and they say goodbye, reply 'Bye.'\nYour situation: " + persona),
        messages=[{"role": "user", "content": f"Conversation so far:\n{convo}\n\nWhat do you say next?"}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def run_case(case: dict, client, verbose: bool) -> dict:
    menu = load_restaurant_menu("sample-china")
    tz = menu.now().tzinfo
    session = OrderSession(menu=menu, on_submit=lambda order: 1, caller_phone="8045550123",  # like a real call
                           now_fn=lambda: datetime.now(tz).replace(hour=18, minute=0))
    agent = TextAgent(session, client=client)
    transcript = [("Restaurant", agent.opening_line)]
    for _ in range(MAX_TURNS):
        said = caller_reply(client, case["caller"], transcript)
        transcript.append(("Caller", said))
        answer = agent.respond(said)
        transcript.append(("Restaurant", answer))
        if session.submitted_order or session.transferred or session.ended_reason:
            break
    expect = case["expect"]
    if expect.get("transfer"):
        passed = session.transferred
        got = "transferred" if session.transferred else "not transferred"
    else:
        got_lines = normalize_lines(session.submitted_order["lines"]) if session.submitted_order else None
        passed = got_lines == normalize_lines(expect["lines"])
        got = got_lines if got_lines is not None else "no order submitted"
    if verbose or not passed:
        print(f"\n--- {case['id']} ---")
        for who, text in transcript:
            print(f"{who}: {text}")
        print(f"GOT: {got}")
    return {"id": case["id"], "passed": passed, "turns": len(transcript) // 2}


def main() -> None:
    import anthropic

    p = argparse.ArgumentParser()
    p.add_argument("--case")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    cases = json.loads(CASES.read_text())
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    client = anthropic.Anthropic()
    results = [run_case(c, client, args.verbose) for c in cases]
    passed = sum(r["passed"] for r in results)
    print("\n" + "\n".join(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']}  ({r['turns']} turns)" for r in results))
    print(f"\nAccuracy: {passed}/{len(results)} = {100 * passed / max(len(results), 1):.0f}%")


if __name__ == "__main__":
    main()
