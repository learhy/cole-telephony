# Cole Voice Agent — Setup & Architecture Guide

## What's Running

Three systemd services (user-level):

1. **livekit-server.service** — LiveKit WebRTC server (port 7880 API, 7881 TCP, 50000-60000 UDP for WebRTC)
2. **cole-voice-agent.service** — Python agent worker (connects to LiveKit, handles calls)
3. **redis-server** — State management for SIP trunking (port 6379)

## Architecture

```
Dan's Phone → SIP Provider (Twilio/Telnyx) → LiveKit SIP Trunk → LiveKit Agent
    → OpenAI (STT + LLM + TTS)
    → Hermes Agent API (<hermes-host>:8642) for tool calls
```

## What's Configured

- LiveKit server: self-hosted on this box, API key `<livekit-api-key-id>`
- SIP trunk: `<livekit-trunk>` (number: <old-sip-number>)
- SIP dispatch rule: `<dispatch-rule>` (routes calls to agent rooms)
- Agent: Python LiveKit Agents worker with OpenAI gpt-4.1 + gpt-4o-mini-tts
- Hermes API: working (tested — responds as Cole)
- Passphrase: "<passphrase>" (first thing the agent asks)
- Tools available mid-call: create_task, query_hermes, dispatch_work, send_email, end_call

## What's Left (Needs Dan's Input)

The SIP trunk is created in LiveKit but needs a SIP provider to bridge PSTN calls to it. Two options:

### Option A: Twilio SIP Trunk (Recommended — you already have a Twilio account)

1. Log into Twilio console (console.twilio.com)
2. Go to Voice → SIP → SIP Trunks
3. Create a new SIP Trunk
4. Set the termination URL to: `sip:<server-public-ip>:5060` (this server's public IP)
5. Buy a phone number and assign it to the trunk
6. Add the number to the LiveKit SIP trunk:
   ```
   lk sip inbound update <livekit-trunk> --numbers "+1XXXXXXXXXX"
   ```

Twilio Account SID (from previous config): <twilio-sid>

### Option B: LiveKit Cloud (Easier, free tier)

1. Sign up at livekit.io
2. Create a project (free tier includes 1 phone number)
3. Purchase a phone number through LiveKit Cloud
4. Update the agent to connect to LiveKit Cloud instead of self-hosted

## Files

- `/home/dan.rohan/cole-agent/agent.py` — The agent code
- `/home/dan.rohan/cole-agent/start.sh` — Startup script
- `/home/dan.rohan/livekit-config.yaml` — LiveKit server config
- `/home/dan.rohan/livekit-venv/` — Python venv with LiveKit dependencies
- `/home/dan.rohan/voice-ai-platforms-research.md` — Full platform comparison report

## Service Management

```bash
# Status
systemctl --user status livekit-server.service
systemctl --user status cole-voice-agent.service

# Restart
systemctl --user restart cole-voice-agent.service
systemctl --user restart livekit-server.service

# Logs
journalctl --user -u cole-voice-agent.service -f
journalctl --user -u livekit-server.service -f
```

## Security

1. **Passphrase**: Agent asks "what's the passphrase?" on call connect. Must say "<passphrase>" to proceed.
2. **Caller ID**: Code has `ALLOWED_CALLERS` list — currently empty (passphrase is the gate). Can restrict to Dan's number only by setting `ALLOWED_CALLERS = ["+1206XXXXXXX"]`
3. **Hermes API**: Authenticated with API_SERVER_KEY from .env
4. **LiveKit API**: Authenticated with API key/secret pair

## Mid-Call Tools (The Big Upgrade from Retell)

The agent can call these tools DURING the call (not after):

- **create_task(content, priority)** — Creates a Todoist task in real time
- **query_hermes(question)** — Asks Hermes Agent anything, speaks the answer back
- **dispatch_work(task, assignee)** — Dispatches work to a specialist agent via Kanban
- **send_email(to, subject, body)** — Sends an email via Hermes
- **end_call()** — Gracefully wraps up the call

This is the key difference from Retell: Dan can say "create a task to review the Plot API docs" and the agent will actually create it in Todoist while Dan is still on the line, then confirm it's done.

## Cost

- LiveKit server: free (self-hosted)
- OpenAI gpt-4.1 (LLM): ~$0.075/min
- OpenAI gpt-4o-transcribe (STT): ~$0.006/min
- OpenAI gpt-4o-mini-tts (TTS): ~$0.024/min
- Twilio/SIP provider: ~$0.01-0.02/min
- **Total: ~$0.12/min** (vs Retell's $0.20-0.31/min)

## Outbound Calls (call-dan) — 2026-10-23

`~/bin/call-dan` places a voice call TO Dan's cell from the Twilio +1 360
number (TWILIO_FROM_NUMBER) and, on answer, bridges it into the Cole agent.

**Working architecture (verified live):** LiveKit Cloud dials OUT through an
outbound SIP trunk into a Twilio Elastic SIP Trunk (termination):
`lk sip participant create --room cole-call-<ts> --trunk <livekit-trunk>
--call <dan cell>` → Twilio trunk <twilio-sid>
(<termination-trunk>.pstn.twilio.com, credential-list auth) → PSTN.
The cole-voice-agent worker auto-joins the new room, so Dan talks to Cole.
The script tears the room down at the time limit (default 120s).

**Failed architecture (for the record):** Twilio REST + TwiML
`<Dial><Sip>sip:<num>@<project-subdomain>.sip.livekit.cloud;transport=tcp` fails
with LiveKit Cloud `404 No trunk found` (AuthNoTrunkFound) — even with the
BYOT inbound trunk `<livekit-trunk>` holding the matching E.164 number
byte-exact (verified via API), and with both `+`/no-`+` To formats and an
auth'd trunk. DNS for *.sip.livekit.cloud is wildcard, so the project's
real SIP URI could not be confirmed from the host; per LiveKit issue
#592 the usual cause is INVITEs hitting the wrong project subdomain.
To revive that path: get the SIP URI from cloud.livekit.io → Project
settings, recreate/update the inbound trunk + a dispatch rule, and point
the TwiML `<Sip username= password=>` at it. Trunk <livekit-trunk> (with
SIP auth credentials) was left in place for that purpose. The temporary
dispatch rule from the attempt (<dispatch-rule>) was deleted.

**Verification evidence (12:37 UTC, 2026-10-23):**
- Script: `call-dan --limit 120` → room `cole-call-1792759070`,
  sip_call_id <sip-call-id>, reported "answered".
- LiveKit: room RM_ZTASQPy8dSUs with 2 participants (SIP caller +
  agent); `journalctl --user -u cole-voice-agent`: job AJ_jY3YFd6TeSC2
  received, "Found participant audio track TR_AMbn4mS9s2nC2X from
  participant dan-1792759070", "Audio capture started".
- Twilio: leg from +1 360 number to Dan's cell, direction
  trunking-terminating (earlier verify call <call-sid>,
  status completed, 15s; caller ID = Twilio number confirmed).
- Inbound (484) path untouched: trunk <livekit-trunk>, dispatch rule
  <dispatch-rule>, and the cole-voice-agent inbound behavior unchanged.
## Inbound Calls — 360 Line (2026-10-23)

The 360 line now rings the Cole agent (consolidated from the 484 LiveKit line per Dan):

- Twilio Elastic trunk `<twilio-sid>` (`<inbound-trunk>.pstn.twilio.com`),
  origination URI `sip:<sip-subdomain>.sip.livekit.cloud;transport=tcp`, number <twilio-sid> associated.
- LiveKit inbound trunk `<livekit-trunk>` (<cole-360-number>, allowed_addresses = Twilio signaling CIDRs, no auth) +
  dispatch rule `<dispatch-rule>` → room `cole-<caller>_<random>` (agent auto-joins).
- **Critical:** the Cloud SIP subdomain (`<sip-subdomain>`) differs from the WS subdomain (`<project-subdomain>`).
  Never guess it — read it from cloud.livekit.io → Project settings. Wrong subdomain = 404 "No trunk found".
- Inbound SMS on 360 → `https://dev.learhy.net/sms-gw/sms/twilio` (cole-gateway, `gateway.db`).
- Verified live 2026-10-23: test call completed, agent job received, audio capture started.

## Toll-Free Line (2026-10-23)

Purchased Twilio toll-free number (sid <twilio-sid>) —
the outbound-SMS workhorse, since 10DLC/A2P registration is not viable for this account
(sole-prop OTP path is carrier-filtered/dead; LVS declined on cost).

- **Inbound voice:** added to Twilio trunk <twilio-trunk> + LiveKit inbound trunk <livekit-trunk>
  (same dispatch → Cole agent). VoiceUrl/SmsUrl also set (SmsUrl → cole-gateway webhook).
- **Outbound voice:** dedicated LiveKit outbound trunk <livekit-trunk> (numbers=[TF],
  same Twilio termination domain + v2 credential list CLd8af5cdf…). Verified: completed 13s.
- **call-dan:** `--from +1844…` uses the TF trunk; default uses the 360-only trunk
  <livekit-trunk> (verified caller ID = the 360 line). NOTE: lk's `--number` flag does NOT pin
  caller ID on a multi-number trunk — caller ID follows the trunk's number list; one
  number per trunk is the reliable pattern.
- **SMS:** inbound works now (gateway webhook). Outbound SMS is carrier-filtered until
  toll-free verification (TFV <twilio-sid>, submitted 2026-10-23)
  is approved — 5–15 business days, no fee, no monthly 10DLC charge. Check status:
  GET messaging.twilio.com/v1/Tollfree/Verifications/<twilio-sid>

## SECURITY INCIDENT: TTS voice-clone accepted via override (2026-10-23)

Dan called in using a Mac `say` TTS voice plus the passphrase and was FULLY
accepted: the voice-verification else-branch had a "phone codec workaround"
override that set `voice_verified = True` whenever `passphrase_checked and
score >= 0.1`, even when the AASIST anti-spoof model flagged the audio as
`spoof`. TTS attack scores: voiceprint "match" 0.3669 / 0.3008 (old threshold
0.1 was meaningless) but spoof 0.0236 / 0.0050 — anti-spoof was the ONLY
factor that caught it.

Fix (same date, agent_voice_auth.py):
- Override DELETED. `spoof == "spoof"` now always denies: `voice_verified`
  stays False, audit logs `voiceprint_result` result=fail,
  decision_note=`anti_spoof_spoof_denied`.
- `MATCH_THRESHOLD = 0.55` named constant replaces `score >= 0.1`. A
  borderline real-voice score now means "unverified" (limited mode), never a
  lockout — the call stays up.
- `unverified_mode`: on any non-authenticated voice result the call continues
  but sensitive tools (create_task, dispatch_work, send_email, create_bear_note)
  are gated off, and the system prompt now states identity is established ONLY
  by the auth pipeline — the LLM must never confirm/deny identity from
  conversation.
- Regression test with the exact attack scores (0.3669/0.0236, 0.3008/0.0050)
  confirms deny + audit. Threshold 0.55 is a chosen bar (no ground-truth
  distribution for Dan's real voice yet); if real calls start landing in
  unverified mode, look at the score in auth-audit.log before lowering.

## Transcripts & Auth Audit (2026-10-23)

The agent now saves per-call transcripts and a structured auth audit log.

**Transcripts** — `~/cole-agent/transcripts/<room_name>.txt` (one file per
call; rooms are unique per call). Rewritten from `session.history` on every
`conversation_item_added` event. Format: one line per turn,
`[HH:MM:SS UTC] CALLER: ...` / `[HH:MM:SS UTC] COLE: ...`. Function-call
items are skipped. Query a call: `grep -l <room> ~/cole-agent/transcripts/`
or just open the room's file. Newest: `ls -t ~/cole-agent/transcripts | head`.

**Auth audit** — `~/cole-agent/auth-audit.log` (JSON Lines, append-only,
mode 0600). One event per line, `ts` is UTC ISO8601. Event types:
- `call_start` — room, caller_number (SIP attr), job_id
- `passphrase_result` — result pass/fail, spoken_text (truncated 200 chars);
  emitted on EVERY evaluation including failures
- `voiceprint_result` — result pass/fail/error, score, spoof_score,
  decision_note (`voiceprint_authenticated`, `anti_spoof_spoof_denied`,
  `voiceprint_below_threshold`, `voiceprint_threshold_met_but_not_authenticated`,
  `voiceprint_mismatch_low_score` legacy)
- `call_end` — reason

Query failed attempts:
    grep '"result": "fail"' ~/cole-agent/auth-audit.log
    grep 'anti_spoof_spoof_denied' ~/cole-agent/auth-audit.log
    jq -r 'select(.event=="voiceprint_result" and .result!="pass") | [.ts,.room,.decision_note,.score,.spoof_score] | @tsv' ~/cole-agent/auth-audit.log

All audit/transcript writes are wrapped in try/except and log a warning on
failure — a logging failure can never crash a call. Pre-change backup:
`~/cole-agent/agent_voice_auth.py.bak-transcripts`.

## Telegram Approval & Multi-Session Enrollment (2026-10-23)

### Telegram approval gate (sensitive actions)

On any call that is **not fully voice-verified** (voiceprint below
`MATCH_THRESHOLD`, spoof-flagged, or never confirmed), every sensitive tool —
`create_task`, `dispatch_work`, `send_email`, `create_bear_note` — now requires
Dan's approval over Telegram before it executes. Read-only tools
(`search_bear_notes`, `query_hermes`) are untouched.

**Flow:**
1. Caller asks for a sensitive action on an unverified call.
2. Cole says "I need to confirm that with Dan via Telegram — one moment."
3. `telegram_approval.request_approval(desc)` sends:
   `Cole: approval needed — <desc>. Reply APPROVE or DENY. Auto-deny in 90s.`
   to Dan's chat (TELEGRAM_HOME_CHANNEL from the work-assistant profile .env).
4. Short-polls getUpdates (1s timeout, 0.4s interval) for up to 90s.
   First matching reply wins: `approved` → tool proceeds; `denied` → refused;
   deadline → `timeout` → refused.
5. Every verdict is audited: `{"event": "approval", "action", "verdict", "room",
   "voice_verified"}` in `auth-audit.log`.

**Fail-closed:** missing creds, network errors, unexpected Telegram API errors
— everything collapses to `timeout`, i.e. the action is refused. Verified live.

**Security properties:**
- Cursor dedupe: the highest processed `update_id` is persisted to
  `~/.hermes/cache/telegram-approval-cursor`; old messages can never approve a
  new request (cursor advances past them, and messages older than
  request_ts + 5s skew are additionally ignored).
- Chat-locked: only messages from Dan's chat count.
- One approval authorizes ONE action — it never flips the call to verified.

**KNOWN COEXISTENCE ISSUE — shared bot token with hermes-work:** the bot token
in `~/.hermes/profiles/work-assistant/.env` is also consumed by
`hermes-work.service` (Telegram gateway). Telegram permits only one getUpdates
consumer; the second gets HTTP 409. Implementation: short polls (timeout=1)
with 409 backoff — empirically the gateway's poll occupies the channel ~10% of
the time, so we win the channel and hermes-work sees
"terminated by other getUpdates request" conflicts and backs off (20→30→40s)
during our window. Effects: (a) during a ≤90s approval window the work
assistant's Telegram polling is degraded and messages that arrive may be
consumed by the approval gate instead; (b) after the window it recovers on its
own (verified in journal). Clean fix if this matters: create a dedicated
approval bot via BotFather and point `TELEGRAM_BOT_TOKEN` for the gate at it —
`telegram_approval.py` reads creds independently, so it's a one-line env swap.

**Manual test harness:** `~/livekit-venv/bin/python ~/cole-agent/telegram_approval.py "desc" 90`
sends a real request and prints the verdict JSON. Retest the approve path with
Dan at his phone: run it, have him reply APPROVE, expect
`{"verdict": "approved"}`.

### Multi-session voice re-enrollment

Replaces enroll-on-first-20s (that produced the stale single-sample template
whose real-voice scores collapsed to 0.078-0.41 vs threshold 0.55).

**Constants:** `MIN_ENROLLMENT_SECONDS = 20` (per capture, unchanged),
`TARGET_TOTAL_SECONDS = 60`, `MIN_ENROLLMENT_SESSIONS = 2`.

**Flow:**
1. Dan calls; no enrolled voiceprint → enrollment mode (passphrase still
   required first — blocks caller-ID spoofers from enrolling their own voice).
2. Cole reports banked progress: "You have X of 60 seconds so far."
3. Every ≥20s stretch of speech is banked as a WAV chunk under
   `~/cole-agent/enrollment-state/` plus a JSON state file
   (`<caller>.json`: total_seconds, sessions, chunks, cleared_old, enrolled).
   Buffer resets after each bank so chunks are distinct audio. Progress
   survives hang-ups and restarts; state persists across calls.
4. First fresh capture deletes the STALE template via the voice-auth service's
   `DELETE /users/dan.rohan` endpoint (voiceprints FK cascades — verified
   equivalent to the psql DELETE; audited as `stale_template_cleared`).
   If deletion fails, banking is refused (fail-closed — never enroll into a
   mixed template).
5. At ≥60s across ≥2 distinct calls, all banked chunks are POSTed to
   `/enroll` (session_type='enrollment'), then the fresh template is verified
   and the score is spoken to Dan and audited
   (`enrollment_progress`, `enrollment_completed` events).

**Reset note:** the old template is deleted as part of the enroll flow (first
new capture), NOT manually — from that point every call runs in enrollment
mode until the fresh template completes.

**Unit tests:** `~/livekit-venv/bin/python ~/cole-agent/test_enrollment_and_gate.py`
— 26 checks: accumulation across simulated calls, persistence across
simulated restarts, 60s-single-session NOT ready, 2-sessions-under-60s NOT
ready, enrolled-flag persistence, reply classification, cursor round-trip,
missing-creds fail-closed.

**Rollback:** `cp ~/cole-agent/agent_voice_auth.py.bak-telegram
~/cole-agent/agent_voice_auth.py && systemctl --user restart
cole-voice-agent.service`. (Backups: `.bak-transcripts` pre-audit, `.bak-telegram`
pre-this-change. `telegram_approval.py` / `enrollment_state.py` are additive —
the old agent never imports them.)
