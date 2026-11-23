"""Verify that a webhook request really came from Twilio.

Twilio signs each request: HMAC-SHA1 over the full URL followed by every POST
parameter (sorted by name, name+value concatenated), keyed with your auth
token, base64-encoded, sent in the X-Twilio-Signature header.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def compute_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid(auth_token: str, url: str, params: dict[str, str], signature: str | None) -> bool:
    if not signature:
        return False
    expected = compute_signature(auth_token, url, params)
    return hmac.compare_digest(expected, signature)
