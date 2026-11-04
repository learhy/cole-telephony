# Cole Telephony

Voice + SMS personal assistant infrastructure for "Cole Mercer" — a LiveKit-based
AI phone agent with voice authentication, an SMS gateway, and a telephony bridge.

**This repo is a version-controlled MIRROR.** The services run from live
directories on the host (`~/cole-agent`, `~/voice-auth`, `~/software/cole-gateway`,
`~/bin`); `sync-from-live.sh` copies code/docs here one-way. Never edit files
here and expect them to run — edit live, verify, then sync + commit + push.

## Architecture

```
                ┌── inbound voice: Twilio Elastic Trunk (<inbound-trunk>.pstn.twilio.com)
   PSTN ────────┤        origination → sip:<sip-subdomain>.sip.livekit.cloud (LiveKit Cloud)
                └── inbound SMS:  webhook → cole-gateway (FastAPI, Caddy /sms-gw/*)

   Outbound calls: LiveKit Cloud → outbound SIP trunk → Twilio termination
                   (<termination-trunk>.pstn.twilio.com) → PSTN, via ~/bin/call-dan

   Cole agent: cole-voice-agent.service (user systemd)
               LiveKit Agents + OpenAI Realtime (gpt-realtime, voice "cedar")
               Auth: passphrase + voiceprint (SpeechBrain ECAPA) + anti-spoof (AASIST)
               Post-call: transcripts/ + auth-audit.log (JSONL)

   Voice auth stack: voice-auth Docker services
               voice-auth-service :8011 (ECAPA verify/enroll, pgvector templates)
               anti-spoof-service  :8012 (AASIST liveness)
               voice-auth-db       :5434 (PostgreSQL+pgvector)
```

## Repo layout

| Path | Live location | What |
|---|---|---|
| `cole-agent/` | `~/cole-agent` | LiveKit agent (Realtime + auth + tools) |
| `voice-auth/` | `~/voice-auth` | Docker voice-biometrics stack |
| `cole-gateway/` | `~/software/cole-gateway` | SMS gateway (Twilio webhook + /send) |
| `bin/` | `~/bin` | `call-dan` (outbound call helper), `text-dan` (paging) |
| `deploy/` | `~/livekit-config.yaml` | Sanitized config examples (real config gitignored) |
| `docs/` | `~/cole-agent/docs` | Calibration/ops reports |

## Deploy flow

1. Edit live files; restart user service: `systemctl --user restart cole-voice-agent.service`
2. Verify: `systemctl --user is-active cole-voice-agent.service` + journal for errors
3. `./sync-from-live.sh`
4. Commit (conventional commits) + push

A scheduled job runs sync + commit + push daily.

## Secrets policy

- `.gitignore` blocks: `*.env`, `livekit-config.yaml`, `*.wav` (biometrics),
  `gateway.db`, `transcripts/`, `auth-audit.log`, logs, models.
- No phone numbers, passphrase, Twilio/LiveKit IDs or server IPs in code or
  docs. They live in `~/.hermes/.env` (see Configuration).
- `scripts/check-no-secrets.sh` runs before every sync commit and blocks the
  push if any of those show up. Install it as a pre-commit hook too:
  `ln -sf ../../scripts/check-no-secrets.sh .git/hooks/pre-commit`
- Voice-auth services bind to 127.0.0.1 only and have no default credentials.

## Configuration

Set these in `~/.hermes/.env` on the host:

| Variable | Used by | What |
|---|---|---|
| `COLE_PASSPHRASE` | cole-agent | Spoken passphrase (factor 1) |
| `COLE_OWNER_PHONE` | cole-agent, call-dan, text-dan | Dan's number (E.164) |
| `COLE_NUMBER_360`, `COLE_NUMBER_TF` | call-dan | Cole's caller-ID numbers |
| `COLE_OUT_TRUNK_360`, `COLE_OUT_TRUNK_TF` | call-dan | LiveKit outbound trunk IDs |
| `HERMES_API_URL` | cole-agent | Hermes API endpoint |
| `VOICEAUTH_API_KEY` | cole-agent, voice-auth | Shared key for the voice-auth services |
| `VOICEAUTH_DB_PASSWORD` | voice-auth | Postgres password |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | cole-agent, call-dan | LiveKit credentials |

## Operational notes

- The full deployment/debugging history lives in `cole-agent/SETUP.md` —
  including the 2026-10-23 TTS-impostor incident (Mac `say` + passphrase got
  accepted via the codec override) and the auth-hardening fix.
- Query failed auth attempts:
  `grep '"result": "fail"' ~/cole-agent/auth-audit.log` (live dir, not mirrored)
- LiveKit Cloud SIP subdomain ≠ WebSocket subdomain — always read the SIP URI
  from cloud.livekit.io → Project settings.
