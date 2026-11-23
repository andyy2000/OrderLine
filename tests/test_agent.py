"""The tool-use loop, tested with a fake Claude that replays a scripted order."""

import json
from types import SimpleNamespace as NS

from app.agent.prompts import build_system_prompt
from app.agent.text_agent import TextAgent
from app.agent.tools import TOOL_NAMES, TOOLS
from app.session import OrderSession


def text(t):
    return NS(type="text", text=t)


def tool(i, name, args):
    return NS(type="tool_use", id=f"tu_{i}", name=name, input=args)


class FakeClaude:
    """Returns pre-written responses in order and records what it was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        return NS(content=self.responses.pop(0))


def test_tool_schemas_match_session_methods():
    for name in TOOL_NAMES:
        assert hasattr(OrderSession, f"tool_{name}"), name
    for t in TOOLS:
        assert t["input_schema"]["type"] == "object"


def test_full_scripted_call(session, store):
    fake = FakeClaude([
        [tool(2, "add_items", {"items": [{"query": "general chicken"}]})],   # no size -> problem
        [text("Small or large?")],
        [tool(3, "add_items", {"items": [{"query": "general chicken", "size": "large"}]})],
        [text("Got it. Anything else?")],
        [tool(4, "get_order", {})],
        [text("One large General Tso's, fourteen dollars and nine cents with tax. Is that right?")],
        [tool(5, "submit_order", {"customer_name": "Andy", "phone": "8045550123"})],
        [text("You're all set, ready in about fifteen minutes. Bye!")],
    ])
    agent = TextAgent(session, client=fake)
    assert agent.respond("Can I get general chicken?") == "Small or large?"
    # the missing-size problem was sent back to the model as an error tool_result
    last_user = fake.calls[1]["messages"][-1]
    assert last_user["content"][0]["is_error"] is True
    assert "needs a size" in last_user["content"][0]["content"]
    assert agent.respond("Large") == "Got it. Anything else?"
    agent.respond("That's all")
    reply = agent.respond("Yes, Andy, 804 555 0123")
    assert "all set" in reply
    assert session.submitted_order["total_cents"] == 1295 + 69
    assert store.list_orders("sample-china")[0]["customer_name"] == "Andy"
    # system prompt is cached and tools are always offered
    assert fake.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in fake.calls[0]["tools"]] == TOOL_NAMES


def test_runaway_tool_loop_is_capped(session):
    fake = FakeClaude([[tool(i, "get_order", {})] for i in range(20)])
    agent = TextAgent(session, client=fake)
    assert "someone from the restaurant" in agent.respond("hi")
    assert len(fake.calls) == 8


def test_prompt_lists_menu_and_rules(menu):
    prompt = build_system_prompt(menu)
    assert "General Tso's Chicken" in prompt and "submit_order" in prompt and "PHONE CALL" in prompt
    assert "$" not in prompt  # no prices in the prompt: prices only come from tools
