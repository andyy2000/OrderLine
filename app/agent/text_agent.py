"""Text version of the phone agent: the same brain and tools, typed instead of spoken.

Used by scripts/chat.py and the evals. It runs Claude's tool-use loop:
send the conversation, run any tools Claude asks for, send the results back,
repeat until Claude replies with words.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..session import OrderSession
from .prompts import build_system_prompt, greeting
from .tools import TOOLS

DEFAULT_MODEL = os.getenv("ORDERLINE_MODEL", "claude-haiku-5-5")
MAX_TOOL_ROUNDS = 8


def _block_get(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


class TextAgent:
    def __init__(self, session: OrderSession, client: Any = None, model: str = DEFAULT_MODEL,
                 voice_style: bool = False):
        if client is None:
            import anthropic  # imported here so tests can run without the SDK

            client = anthropic.Anthropic()
        self.client = client
        self.session = session
        self.model = model
        self.system = build_system_prompt(session.menu, voice=voice_style,
                                          last_order=session.last_order_summary(),
                                          last_name=(session.last_order or {}).get("customer_name"),
                                          has_caller_id=bool(session.caller_phone))
        is_open = session.menu.is_open(session._now())
        self.messages: list[dict] = [{"role": "assistant", "content": greeting(session.menu, is_open)}]
        # The API expects the conversation to start with a user turn.
        self.messages.insert(0, {"role": "user", "content": "(phone rings, caller connects)"})

    @property
    def opening_line(self) -> str:
        return self.messages[1]["content"]

    def respond(self, caller_text: str) -> str:
        self.messages.append({"role": "user", "content": caller_text})
        for _ in range(MAX_TOOL_ROUNDS):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=400,
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=self.messages,
            )
            content = [d for d in (self._as_dict(b) for b in response.content)
                       if d["type"] in ("text", "tool_use") and (d["type"] != "text" or d["text"])]
            self.messages.append({"role": "assistant", "content": content})
            tool_uses = [b for b in content if b["type"] == "tool_use"]
            if not tool_uses:
                return " ".join(b["text"] for b in content if b["type"] == "text").strip()
            results = []
            for use in tool_uses:
                result = self.session.call_tool(use["name"], use.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": use["id"],
                                "content": json.dumps(result, ensure_ascii=False),
                                "is_error": not result.get("ok", False)})
            self.messages.append({"role": "user", "content": results})
        return "Let me get someone from the restaurant to help you."

    @staticmethod
    def _as_dict(block: Any) -> dict:
        kind = _block_get(block, "type")
        if kind == "text":
            return {"type": "text", "text": _block_get(block, "text")}
        if kind == "tool_use":
            return {"type": "tool_use", "id": _block_get(block, "id"), "name": _block_get(block, "name"),
                    "input": _block_get(block, "input") or {}}
        return {"type": kind}
