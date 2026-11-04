# Cole SMS Gateway

Public SMS gateway number for Cole (Hermes orchestrator) on Dan Rohan's Twilio account.

- **Gateway number:** +1206•••••35 (206 area code, SMS+MMS+voice, purchased 2026-09-15)
- **Public webhook:** `https://dev.learhy.net/sms-gw/sms/twilio` (Caddy → 127.0.0.1:18771)
- **Service:** systemd user unit `cole-gateway.service`
- **Log:** `logs/gateway.log` (append-only), message store: `gateway.db` (SQLite)

## Architecture

```
SMS → Twilio → POST https://dev.learhy.net/sms-gw/sms/twilio
                    │ Caddy (TLS) → 127.0.0.1:18771 (uvicorn/FastAPI)
                    ▼
        signature check (HMAC-SHA1 over URL+params)
                    ▼
        dedupe by MessageSid → SQLite log
                    ▼
   STOP/START → optouts table (no reply)
   HELP       → compliance text reply
   normal     → forward to OWNER_MOBILE (rate cap 20/min), silent TwiML
```

Outbound: `POST /send` with `Authorization: Bearer <SEND_TOKEN>`, JSON `{"to": "+1...", "body": "..."}`.
Sends use the Messaging Service SID (A2P 10DLC path) — never bare `From=`.

## Safe defaults (pre-security-review)

| Setting | Value | Why |
|---|---|---|
| AUTO_REPLY | off | Nobody can make the number talk until capabilities are defined |
| FORWARD_ALL | on | Everything is relayed to Dan's cell — nothing silently dropped |
| Signature validation | enforced | 403 on any request without a valid Twilio HMAC |
| Send endpoint | bearer token | `SEND_TOKEN` in .env (600 perms) required |
| Forward rate cap | 20/min | Token bucket; runaway loops can't spam |
| Body cap | 1600 chars | Truncated before Twilio |
| 5xx | never returned | Twilio retry storms impossible; errors logged + silent TwiML |
| Numbers in logs | masked | `+12****35` form |

## Ops

```bash
systemctl --user status cole-gateway          # service state
curl -s https://dev.learhy.net/sms-gw/health  # public health + counters
tail -20 /home/dan.rohan/software/cole-gateway/logs/gateway.log
sqlite3 /home/dan.rohan/software/cole-gateway/gateway.db 'SELECT * FROM messages ORDER BY id DESC LIMIT 10;'
sqlite3 /home/dan.rohan/software/cole-gateway/gateway.db 'SELECT * FROM optouts;'
```

Config: edit `.env`, then `systemctl --user restart cole-gateway`.

## Twilio wiring (account AC3ac3…f5e)

- Number `<twilio-sid>` — SmsUrl webhook set to the public URL (verified via API readback)
- Messaging service `<twilio-sid>` "Cole Gateway (206 A2P)" — sole sender is the 206 number
- A2P brand `BNd9db28609e16aa48ba3d26abc75e3021` (SOLE_PROPRIETOR, APPROVED) — campaign pending OTP verification by Dan
- NOTE: sole-prop brands support exactly one campaign with one number. The 360 number's service (`MGfb28…`) cannot share this campaign.

## Pending

1. **OTP verification** — Dan replies with the code Twilio texts him → run `verify_otp.py`, then register the campaign (samples/flow pre-drafted in this repo's git-less history; see README section below).
2. Campaign creation via `POST /v1/Services/MG2b9e…/Compliance/Usa2p` (UsAppToPersonUsecase=CUSTOMER_CARE).
3. Carrier vetting after that (minutes to 1-5 business days) — until then, outbound/forward messages will 30034 (carrier block). Inbound logging is fully live.
4. Capabilities + security parameters session (user-defined): who can text, what Cole may do with group threads, auto-reply policy, filtering.
