"""Tool definitions the LLM sees (Anthropic Messages API format).

Names and arguments match the tool_* methods on OrderSession exactly, so the
same session works for the text prototype, the evals and the voice bot.
"""

TOOLS = [
    {
        "name": "search_menu",
        "description": "Look up menu items when the caller asks what's available, asks about a dish, "
                       "or add_items said a name was ambiguous. Not needed before add_items.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What the caller asked for, e.g. 'general chicken'"}},
            "required": ["query"],
        },
    },
    {
        "name": "add_items",
        "description": "Add everything the caller just asked for in ONE call. Pass each item as the "
                       "caller said it (query); the system matches it to the menu, so you usually "
                       "don't need search_menu first. Each result is either added or a problem "
                       "(missing size, required choice, ambiguous name, not on menu) to ask about.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "The dish as the caller said it, e.g. 'general chicken'"},
                            "item_id": {"type": "string", "description": "Use instead of query when you already have the id"},
                            "quantity": {"type": "integer", "minimum": 1, "default": 1},
                            "size": {"type": "string", "description": "Size the caller said, e.g. 'large', 'quart'"},
                            "options": {"type": "array", "items": {"type": "string"},
                                        "description": "Choices the caller said, e.g. ['shrimp'], ['extra spicy']"},
                            "notes": {"type": "string", "description": "Short special instruction, e.g. 'no onions'"},
                        },
                    },
                }
            },
            "required": ["items"],
        },
    },
    {
        "name": "reorder_last",
        "description": "Put this caller's previous order in the cart. Only when the system prompt says "
                       "a last order exists and the caller says they want the same thing again.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_item",
        "description": "Change a line already in the order. Only pass the fields that change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "line_index": {"type": "integer", "description": "Line number from get_order, starting at 0"},
                "quantity": {"type": "integer", "minimum": 1},
                "size": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"},
                            "description": "Full new list of options for this line"},
                "notes": {"type": "string"},
            },
            "required": ["line_index"],
        },
    },
    {
        "name": "remove_item",
        "description": "Remove a line from the order.",
        "input_schema": {
            "type": "object",
            "properties": {"line_index": {"type": "integer"}},
            "required": ["line_index"],
        },
    },
    {
        "name": "get_order",
        "description": "Get the current order with line numbers and the total. Call this before "
                       "reading the order back; submit_order only works after it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "answer_question",
        "description": "Get facts about the restaurant. Never answer these from memory.",
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string",
                                     "enum": ["hours", "open_now", "address", "phone", "pickup_time",
                                              "payment", "delivery"]}},
            "required": ["topic"],
        },
    },
    {
        "name": "submit_order",
        "description": "Send the confirmed order to the kitchen. Only after reading the full order "
                       "back and the caller clearly saying yes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "phone": {"type": "string", "description": "Only if there is no caller ID; otherwise leave it out"},
            },
            "required": ["customer_name"],
        },
    },
    {
        "name": "ask_restaurant",
        "description": "Ask the kitchen staff a short question the menu can't answer (substitutions, "
                       "allergies, spice, timing for a big order). Don't say anything first; the caller "
                       "is told to hold automatically. The staff's answer comes back as the result.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "One short question, ideally yes/no"}},
            "required": ["question"],
        },
    },
    {
        "name": "end_call",
        "description": "Politely end the call: prank or abusive caller, dead air, or the caller is done and "
                       "doesn't want to order. Say a one-line goodbye after calling it.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
    {
        "name": "transfer_to_human",
        "description": "Connect the caller to restaurant staff. Use when they ask for a person, are "
                       "upset, want something you can't do (catering, complaints, delivery), or you "
                       "failed to understand them twice.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOLS]
