"""Cole SMS Gateway — Twilio inbound webhook + outbound relay for Dan Rohan.

Inbound:  Twilio POSTs TwiML form data to /sms/twilio (public path /sms-gw/sms/twilio).
          Signature-validated, deduped, logged to SQLite, forwarded to OWNER_MOBILE.
Outbound: POST /send with Bearer SEND_TOKEN -> Twilio Messages API via GATEWAY_NUMBER.
Defaults: AUTO_REPLY=off (silent), FORWARD_ALL=on, forward rate cap 20/min.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "gateway.db"
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)


def load_env() -> dict:
    env = {}
    for line in (BASE / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


CFG = load_env()
ACCOUNT_SID = CFG["TWILIO_ACCOUNT_SID"]
AUTH_TOKEN = CFG["TWILIO_AUTH_TOKEN"]
GATEWAY_NUMBER = CFG["GATEWAY_NUMBER"]
OWNER_MOBILE = CFG.get("OWNER_MOBILE", "")
SEND_TOKEN = CFG.get("SEND_TOKEN", "")
MSG_SERVICE_SID = CFG.get("TWILIO_MESSAGING_SERVICE_SID", "")
AUTO_REPLY = CFG.get("AUTO_REPLY", "off").lower() == "on"
FORWARD_ALL = CFG.get("FORWARD_ALL", "on").lower() == "on"
FORWARD_MAX_PER_MIN = int(CFG.get("FORWARD_MAX_PER_MIN", "20"))
PUBLIC_HOST = CFG.get("GATEWAY_PUBLIC_HOST", "dev.learhy.net")
WEBHOOK_PATH = "/sms-gw/sms/twilio"  # public path Twilio signs; no prefix stripping in Caddy

E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_WORDS = {"START", "UNSTOP", "YES"}
HELP_TEXT = (
    "This is Dan Rohan's assistant gateway. Messages are relayed to Dan. "
    "Reply STOP to opt out, START to resume, HELP for this message."
)

_help = {}

db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = db()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL,
                from_number TEXT,
                to_number TEXT,
                body TEXT,
                sid TEXT UNIQUE,
                status TEXT,
                error TEXT,
                media_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS optouts (
                number TEXT PRIMARY KEY,
                stopped_at TEXT
            );
            """
        )
        conn.commit()
        conn.close()


def log_line(line: str):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG_DIR / "gateway.log", "a") as f:
        f.write(f"{stamp} {line}\n")


def mask_number(num: str) -> str:
    if not num or len(num) < 6:
        return "****"
    return f"{num[:3]}****{num[-2:]}"


def record(direction: str, from_number: str, to_number: str, body: str,
           sid: str, status: str, error: str = None, media_count: int = 0) -> bool:
    """Insert a message row. Returns False if sid already seen (dedupe)."""
    with db_lock:
        conn = db()
        try:
            conn.execute(
                "INSERT INTO messages (ts, direction, from_number, to_number, body, sid, status, error, media_count) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), direction, from_number, to_number,
                 body, sid, status, error, media_count),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()


def is_opted_out(number: str) -> bool:
    with db_lock:
        conn = db()
        row = conn.execute("SELECT 1 FROM optouts WHERE number=?", (number,)).fetchone()
        conn.close()
        return row is not None


def set_optout(number: str, stopped: bool):
    with db_lock:
        conn = db()
        if stopped:
            conn.execute(
                "INSERT OR IGNORE INTO optouts (number, stopped_at) VALUES (?,?)",
                (number, datetime.now(timezone.utc).isoformat()),
            )
        else:
            conn.execute("DELETE FROM optouts WHERE number=?", (number,))
        conn.commit()
        conn.close()


# ── Twilio signature validation ─────────────────────────────────────────────
def valid_signature(signature: str, url: str, params: dict) -> bool:
    """Twilio HMAC-SHA1 over full URL + sorted k+v concatenation, base64."""
    if not signature:
        return False
    data = url + "".join(k + v for k, v in sorted(params.items()))
    digest = hmac.new(AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def signed_url(request: Request) -> str:
    """Reconstruct the exact public URL Twilio signed (https + public host + path)."""
    return f"https://{PUBLIC_HOST}{WEBHOOK_PATH}"


# ── TwiML helpers ────────────────────────────────────────────────────────────
def escape_xml(v: str) -> str:
    return (v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def twiml(message: str = None) -> Response:
    if message:
        body = escape_xml(message[:1600])
        xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{body}</Message></Response>'
    else:
        xml = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
    return Response(content=xml, media_type="text/xml")


# ── Outbound sends (shared by forward + /send) ──────────────────────────────
send_lock = threading.Lock()
send_timestamps: list[float] = []


def allow_send() -> bool:
    with send_lock:
        now = time.monotonic()
        cutoff = now - 60.0
        global send_timestamps
        send_timestamps = [t for t in send_timestamps if t > cutoff]
        if len(send_timestamps) >= FORWARD_MAX_PER_MIN:
            return False
        send_timestamps.append(now)
        return True


def twilio_send(to: str, body: str) -> dict:
    """POST to Twilio Messages API. Returns {sid, status, error}."""
    if not E164_RE.match(to):
        return {"sid": None, "status": "rejected", "error": f"invalid E.164: {mask_number(to)}"}
    if not allow_send():
        return {"sid": None, "status": "rate_limited", "error": f"over {FORWARD_MAX_PER_MIN}/min"}
    data = {"To": to, "Body": body[:1600]}
    # A2P-compliant path: MessagingServiceSid when configured; Twilio rejects
    # requests that set both. Fall back to From for unregistered test setups.
    if MSG_SERVICE_SID:
        data["MessagingServiceSid"] = MSG_SERVICE_SID
    else:
        data["From"] = GATEWAY_NUMBER
    try:
        r = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json",
            data=data, auth=(ACCOUNT_SID, AUTH_TOKEN), timeout=15,
        )
        if 200 <= r.status_code < 300:
            payload = r.json()
            return {"sid": payload.get("sid"), "status": payload.get("status", "queued"), "error": None}
        return {"sid": None, "status": f"http_{r.status_code}", "error": r.text[:300]}
    except Exception as exc:
        return {"sid": None, "status": "network_error", "error": str(exc)[:300]}


def forward_to_owner(from_number: str, body: str, media_count: int) -> dict:
    if not FORWARD_ALL or not OWNER_MOBILE or is_opted_out(from_number):
        return {"sid": None, "status": "skipped", "error": None}
    label = mask_number(from_number)
    prefix = f"Cole gw | {label} says:"
    if media_count:
        prefix += f" [{media_count} media]"
    return twilio_send(OWNER_MOBILE, f"{prefix} {body}".strip())


# ── FastAPI app ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        r = httpx.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}.json",
            auth=(ACCOUNT_SID, AUTH_TOKEN), timeout=10,
        )
        ok = r.status_code == 200 and r.json().get("status") == "active"
    except Exception as exc:
        ok = False
        log_line(f"startup twilio check exception: {exc}")
    log_line(f"startup ok={ok} gateway={mask_number(GATEWAY_NUMBER)} "
             f"forward_all={FORWARD_ALL} auto_reply={AUTO_REPLY}")
    _help["twilio_ok"] = ok
    yield


app = FastAPI(title="Cole SMS Gateway", lifespan=lifespan)


@app.get("/health")
async def health():
    with db_lock:
        conn = db()
        n = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        conn.close()
    return {
        "ok": True,
        "service": "cole-gateway",
        "gateway_number": mask_number(GATEWAY_NUMBER),
        "twilio_auth_ok": _help.get("twilio_ok"),
        "messages_logged": n,
        "forward_all": FORWARD_ALL,
        "auto_reply": AUTO_REPLY,
    }


@app.post("/sms/twilio")
async def sms_webhook(request: Request):
    """Twilio inbound webhook. Public URL: https://dev.learhy.net/sms-gw/sms/twilio"""
    try:
        raw = (await request.body()).decode("utf-8", errors="replace")
        params = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
        sig = request.headers.get("x-twilio-signature", "")

        if not valid_signature(sig, signed_url(request), params):
            log_line(f"WEBHOOK bad_signature from_ip={request.client.host if request.client else '?'}")
            return Response(content="Invalid signature", status_code=403)

        from_number = params.get("From", "")
        to_number = params.get("To", "")
        body = params.get("Body", "")
        message_sid = params.get("MessageSid", f"local-{int(time.time()*1000)}")
        try:
            media_count = min(int(params.get("NumMedia", "0")), 10)
        except ValueError:
            media_count = 0

        if not from_number or (not body and not media_count):
            log_line("WEBHOOK missing_content")
            return twiml(None)

        # Dedupe by MessageSid (Twilio retries can double-deliver)
        if not record("in", from_number, to_number, body, message_sid, "received",
                      media_count=media_count):
            log_line(f"IN deduped sid={message_sid}")
            return twiml(None)

        keyword = body.strip().upper()

        if keyword in STOP_WORDS:
            set_optout(from_number, True)
            log_line(f"STOP number={mask_number(from_number)}")
            return twiml(None)  # Twilio sends its own compliance confirmation

        if keyword in START_WORDS:
            set_optout(from_number, False)
            log_line(f"START number={mask_number(from_number)}")
            return twiml(None)

        if keyword == "HELP":
            record("out", to_number, from_number, HELP_TEXT, f"help-{message_sid}", "queued")
            log_line(f"HELP number={mask_number(from_number)}")
            return twiml(HELP_TEXT)

        # Normal message: forward to owner; stay silent unless AUTO_REPLY on
        fwd = forward_to_owner(from_number, body, media_count)
        if fwd.get("sid"):
            record("out", to_number, OWNER_MOBILE, f"[fwd of {message_sid}]",
                   fwd["sid"], fwd.get("status", "queued"))
        log_line(f"IN number={mask_number(from_number)} media={media_count} "
                 f"body={body[:200]!r} fwd={fwd.get('status')} "
                 f"err={fwd.get('error') or '-'}")

        if AUTO_REPLY:
            reply = "Got it — relayed to Dan."
            record("out", to_number, from_number, reply, f"auto-{message_sid}", "queued")
            return twiml(reply)
        return twiml(None)

    except Exception as exc:
        # Never 5xx — Twilio retry-storms on non-2xx. Log and swallow.
        log_line(f"WEBHOOK error {type(exc).__name__}: {exc}")
        return twiml(None)


@app.post("/send")
async def send(request: Request):
    """Bearer-protected outbound send. JSON: {to, body}."""
    auth = request.headers.get("authorization", "")
    if not SEND_TOKEN or auth != f"Bearer {SEND_TOKEN}":
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
        to = payload.get("to", "")
        body = payload.get("body", "")
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not body:
        return JSONResponse({"ok": False, "error": "empty body"}, status_code=400)
    result = twilio_send(to, body)
    if result.get("sid"):
        record("out", GATEWAY_NUMBER, to, body, result["sid"], result.get("status", "queued"))
    log_line(f"OUT api to={mask_number(to)} status={result.get('status')} err={result.get('error') or '-'}")
    return JSONResponse({"ok": bool(result.get("sid")), **result}, status_code=200)
