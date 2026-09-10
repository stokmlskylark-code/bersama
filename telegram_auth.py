"""Validation for Telegram Mini App initData payloads."""

import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl


# ponytail: reopen the Mini App after one hour; longer sessions need server-issued sessions.
DEFAULT_TTL_SECONDS = 60 * 60


def authenticate_init_data(init_data, *, now=None, bot_token=None, ttl=DEFAULT_TTL_SECONDS):
    """Return Telegram user id or a safe authentication error message."""
    if not isinstance(init_data, str) or not init_data:
        return None, "Autentikasi Telegram diperlukan."
    token = (bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    if not token:
        return None, "Autentikasi Telegram belum tersedia."
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError("duplicate key")
            values[key] = value
        received_hash = values.pop("hash")
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = user["id"]
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("invalid user")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, "Autentikasi Telegram tidak valid."

    now = time.time() if now is None else now
    if auth_date > now or now - auth_date > ttl:
        return None, "Autentikasi Telegram telah kedaluwarsa."
    if len(received_hash) != 64 or not received_hash.isascii() or any(char not in "0123456789abcdefABCDEF" for char in received_hash):
        return None, "Autentikasi Telegram tidak valid."
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        return None, "Autentikasi Telegram tidak valid."
    return user_id, None
