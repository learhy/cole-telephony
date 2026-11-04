#!/usr/bin/env python3
"""
Telegram approval gate for the Cole voice agent.

When a sensitive tool is requested on a call that is NOT fully voice-verified,
the agent asks Dan over Telegram. Dan replies APPROVE or DENY from his phone;
if he doesn't answer within the window the request FAILS CLOSED ('timeout').

Design constraints (from the 2026-10-23 auth hardening):
  - Fail-closed: on ANY error (no creds, network failure, Telegram API error)
    the verdict is 'timeout' — the sensitive action is never performed on a
    broken approval channel.
  - Dedupe: a persisted cursor (highest update_id processed) guarantees old
    messages can never approve new requests. Messages older than the request
    timestamp are additionally ignored (belt and suspenders).
  - Chat-locked: only messages from TELEGRAM_HOME_CHANNEL (Dan's chat) count.

Usage:
    import telegram_approval
    verdict = await telegram_approval.request_approval("Create Todoist task")
    # verdict in {'approved', 'denied', 'timeout'}
"""

import os
import re
import time
import json
import asyncio
import logging

import httpx

log = logging.getLogger("cole-telegram-approval")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_ENV_PATH = os.path.expanduser(
    "~/.hermes/profiles/work-assistant/.env"
)
CURSOR_PATH = os.path.expanduser(
    "~/.hermes/cache/telegram-approval-cursor"
)

_HTTP_SEND_TIMEOUT = 10.0

# The bot token is SHARED with the hermes-work Telegram gateway
# (hermes-work.service), which also calls getUpdates. Telegram allows only one
# consumer: a second concurrent getUpdates gets a 409 conflict. Empirically the
# gateway's poll occupies the channel only ~10% of the time, so SHORT polls
# (timeout=1) with 409 retry win the channel reliably — a long poll loses
# every race. Sleep between polls and back off on conflicts.
_SHORT_POLL_TIMEOUT = 1
_POLL_INTERVAL = 0.4
_CONFLICT_BACKOFF = 0.6

# Clock skew allowance when filtering messages by timestamp.
_TS_SKEW_SECONDS = 5


def _load_env_file(path: str) -> dict:
    """Parse a simple KEY=VALUE .env file. Missing file -> empty dict."""
    out = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _get_creds():
    """Return (bot_token, home_chat_id) or (None, None)."""
    env = _load_env_file(TELEGRAM_ENV_PATH)
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_raw = env.get("TELEGRAM_HOME_CHANNEL", "").strip()
    try:
        chat_id = int(chat_raw)
    except ValueError:
        chat_id = None
    if not token or chat_id is None:
        return None, None
    return token, chat_id


# ---------------------------------------------------------------------------
# Cursor persistence — the dedupe backbone
# ---------------------------------------------------------------------------

def _load_cursor() -> int:
    """Highest update_id we have fully processed. 0 means 'no cursor yet'."""
    try:
        with open(CURSOR_PATH, "r") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _save_cursor(update_id: int) -> None:
    """Persist the cursor atomically. Never raises."""
    try:
        os.makedirs(os.path.dirname(CURSOR_PATH), exist_ok=True)
        tmp = CURSOR_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(update_id))
        os.replace(tmp, CURSOR_PATH)
        os.chmod(CURSOR_PATH, 0o600)
    except OSError as e:
        log.warning("telegram-approval: cursor save failed: %s", e)


# ---------------------------------------------------------------------------
# Telegram API (all network calls timeout-wrapped)
# ---------------------------------------------------------------------------

async def _api(token: str, method: str, payload: dict, timeout: float) -> dict:
    """One Telegram Bot API call. Raises on network/API failure."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data}")
        return data["result"]


async def _send_request_message(token: str, chat_id: int, text: str) -> None:
    await _api(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": text},
        timeout=_HTTP_SEND_TIMEOUT,
    )


def _classify_reply(text: str):
    """Map a message to 'approved' / 'denied' / None (not a verdict)."""
    if not text:
        return None
    # Normalise: lowercase, collapse whitespace, strip stray punctuation.
    norm = re.sub(r"[^a-z ]", " ", text.lower()).strip()
    if norm.startswith("approve"):
        return "approved"
    if norm.startswith("deny") or norm.startswith("denied"):
        return "denied"
    return None


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

async def request_approval(action_desc: str, amount_of_time: int = 90) -> str:
    """Ask Dan over Telegram to approve/deny a sensitive action.

    Returns 'approved', 'denied', or 'timeout'. NEVER raises: every failure
    mode collapses to 'timeout' (fail-closed).
    """
    try:
        token, chat_id = _get_creds()
        if not token or not chat_id:
            log.error("telegram-approval: missing credentials — failing closed")
            return "timeout"

        request_ts = int(time.time())
        message = (
            "Cole: approval needed — "
            f"{action_desc}. Reply APPROVE or DENY. "
            f"Auto-deny in {amount_of_time}s."
        )
        await _send_request_message(token, chat_id, message)

        deadline = time.monotonic() + max(int(amount_of_time), 5)
        cursor = _load_cursor()
        offset = cursor + 1 if cursor > 0 else None
        max_seen = cursor

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = await _api(
                    token,
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": _SHORT_POLL_TIMEOUT,
                        "limit": 100,
                        "allowed_updates": ["message"],
                    },
                    timeout=_SHORT_POLL_TIMEOUT + _HTTP_SEND_TIMEOUT,
                )
            except RuntimeError as e:
                if "409" in str(e):
                    # hermes-work gateway holds the poll channel — back off
                    # and retry; NOT a failure of the approval flow.
                    await asyncio.sleep(_CONFLICT_BACKOFF)
                    continue
                raise  # unexpected API error -> outer handler -> fail closed
            for upd in result:
                uid = upd.get("update_id", 0)
                if uid > max_seen:
                    max_seen = uid
                offset = uid + 1

                msg = upd.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                if chat != chat_id:
                    continue  # not Dan's chat — cursor still advances
                # Dedupe by timestamp: a message older than the request (plus
                # skew) is stale, no matter what it says.
                if int(msg.get("date", 0)) + _TS_SKEW_SECONDS < request_ts:
                    continue
                verdict = _classify_reply(msg.get("text", ""))
                if verdict is not None:
                    # first-match-wins: consume everything up to this update
                    _save_cursor(max(uid, max_seen))
                    return verdict

            await asyncio.sleep(_POLL_INTERVAL)

        # Deadline hit (or loop exhausted): persist the cursor so none of the
        # messages we consumed can ever approve a future request.
        _save_cursor(max_seen)
        return "timeout"

    except Exception as e:
        log.error("telegram-approval: failed (%s) — failing closed", e)
        # Persist nothing we can't vouch for; a raised failure means we may
        # not have consumed updates — the old cursor stays valid.
        return "timeout"


# ---------------------------------------------------------------------------
# Offline self-test hooks (used by tests; not called in production)
# ---------------------------------------------------------------------------

def _reset_for_tests() -> None:
    """Clear the persisted cursor (test helper only)."""
    try:
        os.remove(CURSOR_PATH)
    except OSError:
        pass


if __name__ == "__main__":
    import sys
    import asyncio

    desc = sys.argv[1] if len(sys.argv) > 1 else "Test approval"
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    verdict = asyncio.run(request_approval(desc, secs))
    print(json.dumps({"action": desc, "verdict": verdict}))
