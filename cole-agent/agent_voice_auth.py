#!/usr/bin/env python3
"""
Cole Mercer Voice Agent — LiveKit Agents (LiveKit Cloud)
With Voice Biometric Authentication (SpeechBrain ECAPA-TDNN + AASIST anti-spoofing)

Architecture:
  Dan's Phone -> LiveKit Cloud SIP -> LiveKit Agent (this script)
    -> OpenAI Realtime (STT + LLM + TTS)
    -> Voice Auth Service (localhost:8001) for speaker verification
    -> Anti-Spoof Service (localhost:8002) for clone/replay detection
    -> Hermes Agent API (HERMES_API_URL) for tool calls

Authentication Flow:
  Phase 1 (done): Passphrase challenge (COLE_PASSPHRASE)
  Phase 2 (this): Voice biometric verification
    - On call connect, capture audio from the LiveKit session
    - Run through SpeechBrain ECAPA-TDNN to extract voiceprint
    - Compare against Dan's enrolled voiceprint
    - Run AASIST anti-spoofing to detect voice clones
    - Both passphrase + voiceprint must pass (two-factor)

Enrollment Flow (multi-session, 2026-10-23 rework):
  - Old behavior (REMOVED): enroll on the first 20s of speech — that produced a
    stale single-sample template whose scores collapsed (0.078-0.41 real voice).
  - New behavior: enrollment audio ACCUMULATES ACROSS CALLS. Each call banks
    capture chunks (>= MIN_ENROLLMENT_SECONDS each) under
    ~/cole-agent/enrollment-state/. Enrollment completes only at
    >= TARGET_TOTAL_SECONDS (60s) across >= MIN_ENROLLMENT_SESSIONS (2) calls.
  - On the first fresh capture, the STALE template is deleted via the
    voice-auth service DELETE /users/{user_id} endpoint before any new
    enrollment rows are written, so similarity is computed against fresh
    audio only.
  - After enrolling, the fresh template is verified and the score reported.

Sensitive-Action Telegram Approval Gate (2026-10-23):
  - Sensitive tools (create_task, dispatch_work, send_email, create_bear_note)
    require Telegram approval from Dan whenever the call is not fully
    voice-verified. See telegram_approval.py. Fail-closed: any error in the
    approval channel = timeout = action not performed.
"""

import os
import io
import time
import wave
import json
import httpx
import asyncio
import struct
import logging
import numpy as np
from typing import Annotated, Optional
from datetime import datetime, timezone

from livekit.agents import (
    Agent, AgentSession, JobContext, WorkerOptions, cli,
    function_tool,
)
from livekit.plugins import openai
from livekit import rtc

import telegram_approval
from enrollment_state import EnrollmentState, TARGET_TOTAL_SECONDS
MIN_ENROLLMENT_SESSIONS = 2  # distinct calls that must contribute enrollment audio

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PASSPHRASE = os.environ["COLE_PASSPHRASE"]  # set in ~/.hermes/.env, never in code
DAN_PHONE = os.environ.get("COLE_OWNER_PHONE", "")
DAN_USER_ID = "dan.rohan"  # voice auth user_id

# Voiceprint match threshold (cosine similarity). 0.1 was meaningless — the
# 2026-10-23 "say" TTS attack scored 0.3008-0.3669 against Dan's voiceprint.
# 0.55 is a deliberate, conservative bar. With spoof-gating restored, a
# borderline real-voice score means "unverified" (limited mode), never a lockout.
MATCH_THRESHOLD = 0.55

# Hermes API endpoint
HERMES_API_URL = os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642/v1/chat/completions")
HERMES_API_KEY = os.environ.get("API_SERVER_KEY", "")

# OpenAI key
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Voice Auth Service
VOICE_AUTH_URL = os.environ.get("VOICE_AUTH_URL", "http://localhost:8011")
VOICEAUTH_API_KEY = os.environ["VOICEAUTH_API_KEY"]

# Audio capture settings
AUDIO_BUFFER_SECONDS = 15  # Collect 15s of speech before verifying
SAMPLE_RATE = 16000
MIN_ENROLLMENT_SECONDS = 20  # Minimum speech for enrollment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cole-agent")

# ---------------------------------------------------------------------------
# Transcripts & auth audit (append-only, never crash the call)
# ---------------------------------------------------------------------------

TRANSCRIPT_DIR = os.path.expanduser("~/cole-agent/transcripts")
AUTH_AUDIT_PATH = os.path.expanduser("~/cole-agent/auth-audit.log")


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _audit_event(event: dict) -> None:
    """Append one JSON line to the auth audit log. Never raises — an audit
    failure must be invisible to the caller."""
    try:
        record = {"ts": _utc_ts(), **event}
        created = not os.path.exists(AUTH_AUDIT_PATH)
        with open(AUTH_AUDIT_PATH, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        if created:
            os.chmod(AUTH_AUDIT_PATH, 0o600)
    except Exception as e:
        log.warning("cole-agent: auth audit write failed: %s", e)


def _transcript_write(room: str, text: str) -> None:
    """Write the per-call transcript to transcripts/<room>.txt.
    Full rewrite each time (overwrite-safe — rooms are unique per call)."""
    try:
        os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in room) or "unknown"
        with open(os.path.join(TRANSCRIPT_DIR, safe + ".txt"), "w") as f:
            f.write(text)
    except Exception as e:
        log.warning("cole-agent: transcript write failed: %s", e)


def _format_history(session) -> str:
    """Render the session's full chat history as a two-sided transcript."""
    lines = []
    try:
        for item in session.history.items:
            text = (getattr(item, "text_content", None) or "").strip()
            if not text:
                continue  # skip function-call / non-text items
            role = str(getattr(item, "role", "unknown"))
            ts = getattr(item, "created_at", None)
            stamp = datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S") if ts else "--:--:--"
            label = {"user": "CALLER", "assistant": "COLE"}.get(role, role.upper())
            lines.append(f"[{stamp} UTC] {label}: {text}")
    except Exception as e:
        log.warning("cole-agent: transcript format failed: %s", e)
    return "\n".join(lines) + ("\n" if lines else "")

# ---------------------------------------------------------------------------
# Memory loader — reads Hermes durable memory so the voice agent has context
# ---------------------------------------------------------------------------

def _load_hermes_memory() -> str:
    """Read ~/.hermes/memories/MEMORY.md and USER.md and return a compact
    context block.  This gives the voice agent access to the same durable
    facts that the Hermes WebUI / CLI sessions see, so Dan can ask about
    research, preferences, and project state while on a phone call."""
    import os
    parts = []
    home = os.path.expanduser("~/.hermes")
    for fname, label in (("USER.md", "USER PROFILE"), ("MEMORY.md", "MEMORY")):
        path = os.path.join(home, "memories", fname)
        try:
            with open(path, "r") as f:
                content = f.read().strip()
            if content:
                # Truncate each file to 6 KB to keep the voice prompt manageable
                if len(content) > 6000:
                    content = content[:6000] + "\n... (truncated)"
                parts.append(f"=== {label} ===\n{content}")
        except FileNotFoundError:
            pass
    return "\n\n".join(parts)


def _refresh_memory_periodically():
    """Background task that reloads memory every 5 minutes so the voice
    agent picks up new entries written by other Hermes sessions mid-call."""
    global _CACHED_MEMORY, _MEMORY_LOADED_AT
    while True:
        try:
            _CACHED_MEMORY = _load_hermes_memory()
            _MEMORY_LOADED_AT = time.time()
            log.info("cole-agent: memory refreshed (%d chars)", len(_CACHED_MEMORY))
        except Exception as e:
            log.warning("cole-agent: memory refresh failed: %s", e)
        time.sleep(300)  # 5 minutes


# Load memory at startup
_CACHED_MEMORY = _load_hermes_memory()
_MEMORY_LOADED_AT = time.time()
log.info("cole-agent: loaded %d chars of Hermes memory at startup", len(_CACHED_MEMORY))

# Start background refresh thread
import threading
_mem_thread = threading.Thread(target=_refresh_memory_periodically, daemon=True)
_mem_thread.start()


# ---------------------------------------------------------------------------
# Audio Buffer — captures participant audio for voice verification
# ---------------------------------------------------------------------------

class AudioBuffer:
    """Buffers raw audio frames from a LiveKit participant track."""

    def __init__(self, max_seconds: int = 120):
        self.frames: list[rtc.AudioFrame] = []
        self.max_frames = max_seconds * 50  # ~50 frames/sec at 20ms
        self.total_samples = 0
        self.recording = False
        self._lock = asyncio.Lock()

    def start(self):
        self.recording = True
        self.frames.clear()
        self.total_samples = 0

    def stop(self):
        self.recording = False

    async def add_frame(self, frame):
        if not self.recording:
            return
        # AudioStream yields AudioFrameEvent — extract the underlying AudioFrame
        actual_frame = frame.frame if hasattr(frame, 'frame') else frame
        async with self._lock:
            if len(self.frames) < self.max_frames:
                self.frames.append(actual_frame)
                self.total_samples += actual_frame.samples_per_channel * actual_frame.num_channels

    def get_duration_seconds(self) -> float:
        # Audio frames may be at 48kHz but we calculate based on actual sample rate
        if not self.frames:
            return 0.0
        total = sum(f.samples_per_channel for f in self.frames)
        # Use the actual sample rate from the first frame
        actual_rate = self.frames[0].sample_rate if self.frames else SAMPLE_RATE
        return total / actual_rate if actual_rate > 0 else 0.0

    def to_wav_bytes(self, start: int = 0, count: Optional[int] = None) -> bytes:
        """
        Convert buffered frames to a 16kHz mono WAV file in memory.
        Resamples from source rate to 16kHz for SpeechBrain compatibility.
        start: starting frame index
        count: number of frames (None = all remaining)
        """
        frames = self.frames[start:count] if count else self.frames[start:]
        if not frames:
            return b""

        # Get source sample rate from first frame
        src_rate = frames[0].sample_rate

        # Concatenate raw audio data (mono)
        raw_data = bytearray()
        for frame in frames:
            data = frame.data
            if frame.num_channels > 1:
                arr = np.frombuffer(data, dtype=np.int16)
                arr = arr.reshape(-1, frame.num_channels)
                arr = arr.mean(axis=1).astype(np.int16)
                raw_data.extend(arr.tobytes())
            else:
                raw_data.extend(bytes(data))

        # Resample to 16kHz if needed
        if src_rate != SAMPLE_RATE:
            audio_np = np.frombuffer(bytes(raw_data), dtype=np.int16).astype(np.float32)
            # Simple decimation/interpolation
            ratio = SAMPLE_RATE / src_rate
            new_len = int(len(audio_np) * ratio)
            indices = np.linspace(0, len(audio_np) - 1, new_len)
            audio_np = np.interp(indices, np.arange(len(audio_np)), audio_np).astype(np.int16)
            raw_data = audio_np.tobytes()

        # Write WAV header + data at 16kHz
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(bytes(raw_data))

        return buf.getvalue()


# ---------------------------------------------------------------------------
# Voice Auth API client
# ---------------------------------------------------------------------------

async def enroll_voiceprint(audio_bytes: bytes, user_id: str = DAN_USER_ID) -> dict:
    """Send audio to voice-auth service for enrollment."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{VOICE_AUTH_URL}/enroll",
                headers={"X-API-Key": VOICEAUTH_API_KEY},
                data={"user_id": user_id, "display_name": "Dan Rohan", "phone_number": DAN_PHONE},
                files={"audio": ("audio.wav", audio_bytes, "audio/wav")},
            )
            return resp.json()
    except Exception as e:
        log.error("Enrollment failed: %s", e)
        return {"status": "error", "error": str(e)}


async def verify_voiceprint(
    audio_bytes: bytes,
    user_id: str = DAN_USER_ID,
    passphrase_ok: bool = True,
    call_id: Optional[str] = None,
) -> dict:
    """Send audio to voice-auth service for verification."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            data = {
                "user_id": user_id,
                "run_anti_spoof": "true",
                "passphrase_ok": str(passphrase_ok).lower(),
            }
            if call_id:
                data["call_id"] = call_id
            resp = await client.post(
                f"{VOICE_AUTH_URL}/verify",
                headers={"X-API-Key": VOICEAUTH_API_KEY},
                data=data,
                files={"audio": ("audio.wav", audio_bytes, "audio/wav")},
            )
            return resp.json()
    except Exception as e:
        log.error("Verification failed: %s", e)
        return {"final_decision": "rejected", "error": str(e)}


async def check_user_enrolled(user_id: str = DAN_USER_ID) -> bool:
    """Check if user has an enrolled voiceprint."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{VOICE_AUTH_URL}/users/{user_id}",
                headers={"X-API-Key": VOICEAUTH_API_KEY},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("enrolled", False)
            return False
    except Exception:
        return False


async def delete_voiceprints(user_id: str = DAN_USER_ID) -> bool:
    """Delete the user and ALL voiceprint rows via the voice-auth service's
    GDPR deletion endpoint (voiceprints FK cascades from voice_users).

    Used exactly once per re-enrollment campaign — the first fresh capture of
    the multi-session enrollment flow — so the stale single-sample template
    (whose real-voice scores collapsed to 0.078-0.41) is never mixed with new
    audio. Subsequent /enroll calls re-create the user row.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(
                f"{VOICE_AUTH_URL}/users/{user_id}",
                headers={"X-API-Key": VOICEAUTH_API_KEY},
            )
            if resp.status_code in (200, 404):  # deleted, or nothing to delete
                return True
            log.error("Voiceprint deletion failed: HTTP %d: %s",
                      resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        log.error("Voiceprint deletion failed: %s", e)
        return False


async def adapt_voiceprint(audio_bytes: bytes, user_id: str = DAN_USER_ID) -> dict:
    """Update voiceprint after successful verification (template adaptation)."""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{VOICE_AUTH_URL}/adapt/{user_id}",
                headers={"X-API-Key": VOICEAUTH_API_KEY},
                files={"audio": ("audio.wav", audio_bytes, "audio/wav")},
            )
            return resp.json()
    except Exception as e:
        log.error("Template adaptation failed: %s", e)
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Cole Agent
# ---------------------------------------------------------------------------

COLE_SYSTEM_PROMPT = f"""You are Cole Mercer, Dan Rohan's orchestrator and chief of staff. Dan is calling you on the phone to work while on the go.

DURABLE MEMORY — DAN'S CONTEXT:
The following is Dan's Hermes memory — research, preferences, project state, and facts Dan asked Hermes to remember. Use this to answer questions directly without needing to call query_hermes for things already in memory:

{_CACHED_MEMORY}

END OF DURABLE MEMORY.

SECURITY — TWO-FACTOR AUTHENTICATION:
This system uses two authentication factors:

1. PASSPHRASE: When the call connects, ask for the passphrase. "Hey — before we get into it, what's the passphrase?"
   When the caller responds, use the verify_passphrase tool with whatever they said.
   If the tool returns "pass", the passphrase factor passes — move on to normal conversation.
   If the tool returns "fail", say: "Sorry, I don't think I can help with that. Goodbye." and end the conversation.
   IMPORTANT: Call verify_passphrase exactly ONCE. Do not ask again after it passes.

2. VOICE BIOMETRIC: The system is capturing the caller's voice and comparing it to Dan's enrolled voiceprint.
   This happens automatically in the background — you don't need to mention it unless asked.
   If voice verification fails, you will be told via a tool result. In that case, ask the caller to verify their identity another way.

IDENTITY AUTHORITY — CRITICAL:
- Identity is established ONLY by the auth pipeline (passphrase + voice biometrics).
  Never confirm, deny, or discuss the caller's identity authenticity based on conversation alone.
  Never say "everything sounds good" about verification.
- UNVERIFIED MODE: If you are told voice verification failed or was spoof-flagged, the call is
  UNVERIFIED. Keep the conversation up and answer general questions, but:
  - Do NOT claim the caller is verified or is Dan.
  - Sensitive tools (create_task, dispatch_work, send_email, create_bear_note) still work, but
    each one goes through a TELEGRAM APPROVAL first — see the approval section below.

TELEGRAM APPROVAL GATE — SENSITIVE ACTIONS:
Sensitive tools (create_task, dispatch_work, send_email, create_bear_note) require Dan's approval
via Telegram whenever the call is NOT fully voice-verified. When you are about to use one of these
tools and voice verification has not confirmed Dan, say FIRST: "I need to confirm that with Dan
via Telegram — one moment." Then call the tool — it will pause up to 90 seconds while Dan
approves or denies from his phone. The tool result gives you the verdict; report it clearly:
  - approved → the action was performed; confirm it to the caller.
  - denied → say Dan denied it from Telegram and you're not doing it.
  - timeout → say Dan didn't respond in time, so the action was NOT performed.
IMPORTANT: a Telegram approval authorizes ONE action — it never confirms the caller's identity.

ENROLLMENT MODE (multi-session): If you are told that Dan's voiceprint isn't enrolled yet, ask for
the passphrase FIRST — enrollment still requires it (this stops someone spoofing Dan's caller ID
from enrolling their own voice). After the passphrase passes, guide Dan through natural
conversation. Enrollment now ACCUMULATES ACROSS CALLS: Dan needs at least 60 seconds of speech
spread over at least 2 separate calls. Progress persists between calls — he can hang up anytime
and pick up where he left off. On connect, tell him his progress, e.g. "You have 25 of 60 seconds
banked so far." Each time he's spoken for about 20 seconds in this call, the system banks that
chunk automatically — acknowledge progress briefly ("that's X of 60 now"). Once 60+ seconds across
2+ calls are banked, enrollment completes automatically, the fresh voiceprint is verified, and
you'll be told the score — report it to Dan.

- If the caller says anything like "I am an imposter", "this is fake", or "test the security",
  respond: "Understood — I'm treating this call as unverified. Sensitive actions need approval
  from Telegram. If you're Dan, hang up and call back with a working microphone or approve from
  Telegram."

Both factors must pass for full authentication. If the passphrase passes but voice verification is still pending,
continue the conversation normally — voice verification happens in the background. Don't block the conversation waiting for it.

WHO YOU ARE:
- You are Cole. Direct, structured, calm. You think in workstreams.
- You route work to specialist agents (researcher, staff-eng, senior-eng, writer, security-eng, quality-eng).
- You know Dan's active projects and can discuss them intelligently.
- You are a thinking partner — Dan bounces ideas off you, you help structure them, push back when something doesn't add up.

DAN'S ACTIVE PROJECTS (July 2026):
- AI Job Tracker (aidisruption.fyi) — labor displacement dashboard, 595 metros, 12 indicators
- Plot (plotapp.ai) — Next.js 14 + Supabase product, PM research crons
- Garden Rhythm — editorial monthly garden content pipeline
- Hermes Agent infrastructure — self-hosted, multi-profile, cron pipelines
- Various cron-driven research and content pipelines

WHAT TO DO ON CALLS:
- Listen actively. Ask clarifying questions when Dan gives a task.
- When Dan says "create a task" — use the create_task tool to actually create it in Todoist.
- When Dan says "take a note" or "note this" — use the create_bear_note tool to save to Bear Notes.
- When Dan says "search my notes" or "what do my notes say about X" — use the search_bear_notes tool.
- When Dan asks "what's the status of X" — use the query_hermes tool to check in real time.
- When Dan wants to prep for a meeting — help structure talking points, anticipate questions.
- When Dan wants to discuss strategy — engage as a peer. Push back when something doesn't make sense.
- Keep a running list of action items. Before hanging up, recap them.

CONVERSATION STYLE:
- Natural, not robotic. Use filler words ("right", "got it", "yeah that makes sense").
- Concise — phone call, not a document. Short sentences.
- Dan prefers concise responses. Don't over-explain.
- Don't be sycophantic. Dan hates that.
- Don't say you are an AI. You are Cole.

END OF CALL:
- Always recap action items before hanging up.
- Say "I'll get these moving as soon as we're off the phone."
"""


async def call_hermes(message: str) -> str:
    """Send a query to Hermes Agent's local API and get the text response.

    Includes Hermes durable memory in the system message so the API
    server has context about Dan's research, preferences, and projects.
    """
    try:
        # Build system message with memory context
        memory_block = _CACHED_MEMORY or ""
        system_content = (
            "You are Cole Mercer. A voice agent is relaying a query from Dan "
            "over the phone. Be concise — your response will be spoken aloud. "
            "Keep it under 3 sentences. Answer directly. If asked to use tools "
            "(Todoist, Bear Notes, Kanban, email), use them and report the "
            "result concisely."
        )
        if memory_block:
            system_content += (
                "\n\nYou have access to Dan's Hermes memory below. Use it to "
                "answer questions about prior research, project status, "
                "preferences, and anything Dan has asked Hermes to remember.\n\n"
                + memory_block
            )

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                HERMES_API_URL,
                headers={
                    "Authorization": f"Bearer {HERMES_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "default",
                    "messages": [
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": message},
                    ],
                    "max_tokens": 300,
                },
            )
            data = response.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "I couldn't reach Hermes for that.")
    except Exception as e:
        return f"Hermes query failed: {str(e)[:80]}"


class ColeAgent(Agent):
    def __init__(self, room: rtc.Room = None) -> None:
        super().__init__(
            instructions=COLE_SYSTEM_PROMPT,
            llm=openai.realtime.RealtimeModel(
                model="gpt-realtime",
                voice="cedar",
                speed=1.1,
                api_key=OPENAI_API_KEY,
                temperature=0.8,
            ),
        )
        # Voice auth state
        self.room = room
        self.audio_buffer = AudioBuffer(max_seconds=120)
        self.voice_verified = False
        self.voice_pending = True
        self.unverified_mode = False
        self.is_enrollment_mode = False
        self.passphrase_checked = False
        self._audio_task: Optional[asyncio.Task] = None
        self._verification_task: Optional[asyncio.Task] = None
        self._enrollment_task: Optional[asyncio.Task] = None
        self._enrollment_state: Optional[EnrollmentState] = None
        self._verification_attempts = 0
        self._max_verification_attempts = 3

    async def on_enter(self):
        # Check caller ID from the room's remote participants
        if self.room:
            for pid, participant in self.room.remote_participants.items():
                caller_id = participant.attributes.get("sip.caller_id", "")
                if caller_id and caller_id != DAN_PHONE:
                    await self.session.say("Sorry, this number isn't accepting calls from this line. Goodbye.")
                    await self.room.disconnect()
                    return

        # Start capturing audio for voice verification
        await self._start_audio_capture()

        # Check if Dan is enrolled
        enrolled = await check_user_enrolled()
        if not enrolled:
            self.is_enrollment_mode = True
            self._enrollment_state = EnrollmentState(DAN_USER_ID)
            # Enrollment still requires the passphrase first
            # This prevents someone spoofing Dan's caller ID from enrolling their own voice
            self.passphrase_checked = False
            self.voice_verified = False
            progress = self._enrollment_state.progress_text()
            if self._enrollment_state.total_seconds > 0:
                progress_note = (
                    f"You already have {progress} banked from earlier calls — "
                    "we'll pick up right where you left off. "
                )
            else:
                progress_note = ""
            await self.session.generate_reply(
                instructions=(
                    "Say: 'Hey — I don't have your voiceprint yet. Before we set that up, "
                    "what's the passphrase?' Wait for the answer. If the caller says '" + PASSPHRASE + "', "
                    "say 'Got it. Now I need about 60 seconds of your voice to set up enrollment — "
                    "and it can be spread across more than one call; your progress is saved between "
                    "calls. " + progress_note + "Just talk naturally — tell me what you're working on.' "
                    "Then let Dan speak naturally. If the passphrase is wrong, say 'Sorry, I don't "
                    "think I can help with that. Goodbye.' and end the call."
                )
            )
        else:
            # Ask for passphrase (first factor)
            self.passphrase_checked = False
            await self.session.generate_reply(
                instructions="Ask for the passphrase. Say: 'Hey — before we get into it, what's the passphrase?'"
            )

    async def _start_audio_capture(self):
        """Start capturing the participant's audio for voice verification."""
        try:
            if not self.room:
                log.warning("No room available for audio capture")
                return

            # Wait a moment for participants to join and publish tracks
            await asyncio.sleep(1)

            # Try to find audio tracks — retry a few times as tracks may take a moment to publish
            for attempt in range(5):
                for pid, participant in self.room.remote_participants.items():
                    for track in participant.track_publications.values():
                        if track.kind == rtc.TrackKind.KIND_AUDIO and track.track:
                            log.info("Found participant audio track: %s from participant %s", track.sid, pid)
                            self._audio_task = asyncio.create_task(self._capture_audio(track.track))
                            return
                log.info("No audio track found yet (attempt %d), retrying...", attempt + 1)
                await asyncio.sleep(2)

            log.warning("No audio track found on any participant after retries")
        except Exception as e:
            log.error("Audio capture setup failed: %s", e)

    async def _capture_audio(self, track):
        """Continuously capture audio frames from the participant track."""
        try:
            audio_stream = rtc.AudioStream(track)
            self.audio_buffer.start()
            log.info("Audio capture started")

            async for frame in audio_stream:
                await self.audio_buffer.add_frame(frame)

                # In enrollment mode, trigger enrollment after enough audio
                # AND only after passphrase has been verified
                if (self.is_enrollment_mode and
                    self.passphrase_checked and
                    self._enrollment_task is None and
                    self.audio_buffer.get_duration_seconds() >= MIN_ENROLLMENT_SECONDS):
                    self._enrollment_task = asyncio.create_task(self._run_enrollment())

                # In verification mode, trigger verification after enough audio
                if (not self.is_enrollment_mode and
                    not self.voice_verified and
                    self._verification_attempts < self._max_verification_attempts and
                    self.audio_buffer.get_duration_seconds() >= AUDIO_BUFFER_SECONDS):

                    # Trigger background verification (only once)
                    if self._verification_task is None or self._verification_task.done():
                        self._verification_task = asyncio.create_task(self._run_voice_verification())

        except Exception as e:
            log.error("Audio capture error: %s", e)

    def _audit_voiceprint(self, result: str, score, spoof_score, note: str) -> None:
        _audit_event({
            "event": "voiceprint_result",
            "room": self.room.name if self.room else "",
            "result": result,
            "score": score,
            "spoof_score": spoof_score,
            "decision_note": note,
        })

    async def _run_voice_verification(self):
        """Run voice verification against the captured audio."""
        try:
            # Circuit breaker: max 3 attempts, then stop trying
            self._verification_attempts += 1
            if self._verification_attempts > self._max_verification_attempts:
                log.warning("Max verification attempts (%d) reached, stopping", self._max_verification_attempts)
                self.voice_pending = False
                # Don't reject — just stop trying. Passphrase is the primary factor.
                return

            duration = self.audio_buffer.get_duration_seconds()
            if duration < 5:
                log.info("Not enough audio for verification yet (%.1fs)", duration)
                return

            log.info("Starting voice verification attempt %d/%d with %.1fs of audio",
                     self._verification_attempts, self._max_verification_attempts, duration)
            audio_bytes = self.audio_buffer.to_wav_bytes()

            result = await verify_voiceprint(
                audio_bytes,
                passphrase_ok=self.passphrase_checked,
                call_id=getattr(self.session, 'room_id', None),
            )

            decision = result.get("final_decision", "rejected")
            score = result.get("score", 0)
            spoof = result.get("spoof_decision", "unknown")

            log.info("Voice verification result: decision=%s score=%.4f spoof=%s", decision, score, spoof)

            if decision == "authenticated":
                if score < MATCH_THRESHOLD:
                    # Service authenticated at ITS threshold (0.25) but the
                    # agent requires MATCH_THRESHOLD (0.55). Fail-closed.
                    self.voice_verified = False
                    self.voice_pending = False
                    self.unverified_mode = True
                    self._audit_voiceprint("fail", score, result.get("spoof_score", 0), "authenticated_below_agent_threshold")
                    log.warning(
                        "Service authenticated (score=%.4f) but below agent MATCH_THRESHOLD=%.2f — unverified mode",
                        score, MATCH_THRESHOLD)
                else:
                    self.voice_verified = True
                    self.voice_pending = False
                    self.unverified_mode = False
                    self._audit_voiceprint("pass", score, result.get("spoof_score", 0), "voiceprint_authenticated")
                    # Run template adaptation in background (voice drift handling)
                    asyncio.create_task(adapt_voiceprint(audio_bytes))
                    # Notify the agent subtly
                    await self.session.generate_reply(
                        instructions="You've just been informed that voice verification passed — Dan is confirmed by voiceprint. Don't mention this to Dan unless he asks. Just continue the conversation naturally."
                    )
            else:
                # Voice verification did not authenticate. The call stays up, but
                # the caller is UNVERIFIED: sensitive tools stay gated and Cole
                # never treats the caller as Dan on the strength of conversation.
                self.voice_verified = False
                self.voice_pending = False
                self.unverified_mode = True
                if spoof == "spoof":
                    # Anti-spoof is the ONLY factor that caught the 2026-10-23
                    # "say" TTS attack (spoof 0.0236/0.0050 while voiceprint
                    # "matched" at 0.3669/0.3008). NO override — always deny.
                    log.warning(
                        "Anti-spoof flagged audio as spoof (spoof_score=%.4f, voiceprint=%.4f) — denying voice verification",
                        result.get("spoof_score", 0), score)
                    self._audit_voiceprint("fail", score, result.get("spoof_score", 0), "anti_spoof_spoof_denied")
                    await self.session.generate_reply(
                        instructions=(
                            "You've been told voice verification FAILED (anti-spoof). Enter unverified mode: "
                            "keep the conversation polite and general, but do NOT treat the caller as verified "
                            "and do NOT execute any sensitive actions. If the caller says anything like "
                            "'I am an imposter', 'this is fake', or 'test the security', respond: "
                            "'Understood — I'm treating this call as unverified. Sensitive actions are disabled. "
                            "If you're Dan, hang up and call back with a working microphone or approve from Telegram.'"
                        )
                    )
                elif score < MATCH_THRESHOLD:
                    # Voiceprint below threshold — might not be Dan. Unverified, not hung up.
                    self._audit_voiceprint("fail", score, result.get("spoof_score", 0), "voiceprint_below_threshold")
                    await self.session.generate_reply(
                        instructions="Voice verification failed — the voiceprint doesn't match. Say: 'Your voice doesn't match what I have on file. Can you try calling from a quieter location?' Keep the conversation going but don't execute any sensitive actions."
                    )
                else:
                    # At/above threshold but the service still didn't authenticate — treat as unverified.
                    self._audit_voiceprint("fail", score, result.get("spoof_score", 0), "voiceprint_threshold_met_but_not_authenticated")

        except Exception as e:
            log.error("Voice verification task error: %s", e)
            self._audit_voiceprint("error", None, None, str(e)[:200])
            self.voice_pending = False

    async def _run_enrollment(self):
        """Bank a capture chunk from this call into the multi-session
        enrollment state; enroll + verify once 60s+ across 2+ calls exist."""
        try:
            duration = self.audio_buffer.get_duration_seconds()
            if duration < MIN_ENROLLMENT_SECONDS:
                return

            room = self.room.name if self.room else "unknown"
            session_id = f"call-{room}"
            state = self._enrollment_state or EnrollmentState(DAN_USER_ID)
            self._enrollment_state = state

            log.info("Enrollment: banking %.1fs chunk (session=%s, had %.1fs total)",
                     duration, session_id, state.total_seconds)
            audio_bytes = self.audio_buffer.to_wav_bytes()

            # Persist the chunk BEFORE touching the buffer so a crash can't
            # lose banked audio.
            ts = int(time.time() * 1000)  # ms — chunks banked within the same second stay distinct
            chunk_path = os.path.join(
                state.state_dir, f"{DAN_USER_ID}-chunk-{ts}.wav")
            with open(chunk_path, "wb") as f:
                f.write(audio_bytes)
            try:
                os.chmod(chunk_path, 0o600)
            except OSError:
                pass

            # First fresh capture of the re-enrollment campaign: delete the
            # stale template so similarity is computed against new audio only.
            if not state.data.get("cleared_old", False):
                deleted = await delete_voiceprints(DAN_USER_ID)
                if deleted:
                    state.mark_cleared_old()
                    _audit_event({
                        "event": "stale_template_cleared",
                        "room": room,
                        "user_id": DAN_USER_ID,
                    })
                    log.info("Stale voiceprint template deleted for %s", DAN_USER_ID)
                else:
                    # Fail-closed: do NOT bank/enroll against a stale template.
                    log.error("Could not delete stale template — aborting enrollment bank")
                    await self.session.generate_reply(
                        instructions=(
                            "Say: 'I hit a snag setting up your voice enrollment — "
                            "your progress from earlier calls is safe, but I need you "
                            "to try again on your next call.'"
                        )
                    )
                    return

            state.add_capture(chunk_path, duration, session_id)
            _audit_event({
                "event": "enrollment_progress",
                "room": room,
                "session_id": session_id,
                "chunk_seconds": round(duration, 1),
                "total_seconds": state.total_seconds,
                "sessions": state.sessions_count,
            })

            # Reset the buffer so the next banked chunk is distinct audio,
            # not a re-capture of the same window.
            self.audio_buffer.start()

            if not state.ready():
                await self.session.generate_reply(
                    instructions=(
                        f"You've just banked another chunk of Dan's enrollment audio. "
                        f"Current progress: {state.progress_text()}. Briefly acknowledge it: "
                        f"say 'Okay, that's {state.total_seconds:.0f} of 60 seconds banked"
                        f"{' across ' + str(state.sessions_count) + ' calls' if state.sessions_count > 1 else ''}. "
                        f"Keep talking, or we can pick this up on your next call.' "
                        f"Then let him continue naturally."
                    )
                )
                return

            # --- Ready: enroll all banked chunks, then verify fresh template ---
            log.info("Enrollment threshold met: %.1fs across %d sessions — enrolling",
                     state.total_seconds, state.sessions_count)
            enroll_ok = 0
            for chunk in state.chunks():
                try:
                    with open(chunk["path"], "rb") as f:
                        chunk_bytes = f.read()
                except OSError as e:
                    log.error("Enrollment chunk unreadable (%s): %s", chunk["path"], e)
                    continue
                result = await enroll_voiceprint(chunk_bytes)
                if result.get("status") == "enrolled":
                    enroll_ok += 1
                else:
                    log.error("Enroll failed for %s: %s", chunk["path"], result)

            if enroll_ok == 0:
                log.error("Enrollment failed — no chunks enrolled")
                return

            self.is_enrollment_mode = False

            # Verify against the fresh template and report the score.
            verify_result = await verify_voiceprint(
                audio_bytes, passphrase_ok=True, call_id=room)
            v_decision = verify_result.get("final_decision", "rejected")
            v_score = verify_result.get("score", 0)
            v_spoof = verify_result.get("spoof_decision", "unknown")
            log.info("Post-enrollment verification: decision=%s score=%.4f spoof=%s",
                     v_decision, v_score, v_spoof)

            state.mark_enrolled(score=v_score)
            _audit_event({
                "event": "enrollment_completed",
                "room": room,
                "total_seconds": state.total_seconds,
                "sessions": state.sessions_count,
                "chunks_enrolled": enroll_ok,
                "verify_decision": v_decision,
                "verify_score": v_score,
                "verify_spoof": v_spoof,
            })

            if v_decision == "authenticated":
                self.voice_verified = True
                self.voice_pending = False
                self.unverified_mode = False
                self._audit_voiceprint("pass", v_score,
                                       verify_result.get("spoof_score", 0),
                                       "post_enrollment_verified")
                await self.session.generate_reply(
                    instructions=(
                        f"Multi-session enrollment is complete and the fresh voiceprint verified. "
                        f"Say: 'Got your voiceprint — {state.total_seconds:.0f} seconds across "
                        f"{state.sessions_count} calls, verified at a score of {v_score:.2f}. "
                        f"You're all set; I'll verify your voice automatically from now on. "
                        f"What can I help with?'"
                    )
                )
            else:
                # Template is enrolled; this call stays unverified per the normal
                # auth pipeline. Future calls verify normally against the fresh
                # template.
                self.voice_verified = False
                self.unverified_mode = True
                await self.session.generate_reply(
                    instructions=(
                        f"Multi-session enrollment is complete ({state.total_seconds:.0f} seconds "
                        f"across {state.sessions_count} calls), but the post-enrollment verification "
                        f"check scored {v_score:.2f} and did not authenticate. Say: 'Your voiceprint "
                        f"is saved — 60 seconds across multiple calls. The verification check on this "
                        f"call didn't pass though, so some actions will need my approval via Telegram "
                        f"for now. Your next call should verify automatically.'"
                    )
                )

        except Exception as e:
            log.error("Enrollment error: %s", e)

    @function_tool
    async def verify_passphrase(
        self,
        spoken_text: Annotated[str, "The exact words the caller said in response to the passphrase question"],
    ) -> str:
        """Verify the passphrase spoken by the caller. Use this ONCE after asking for the passphrase, passing in whatever the caller said. Returns 'pass' or 'fail'."""
        # Check if the passphrase is contained in what the caller said
        spoken_lower = spoken_text.lower().strip()
        passphrase_lower = PASSPHRASE.lower().strip()

        def _audit_passphrase(result: str) -> None:
            _audit_event({
                "event": "passphrase_result",
                "room": self.room.name if self.room else "",
                "result": result,
                "spoken_text": (spoken_text or "")[:200],
            })

        # Accept if the passphrase is contained in the response, or very close
        if passphrase_lower in spoken_lower:
            self.passphrase_checked = True
            _audit_passphrase("pass")
            return "pass"
        # Also accept if the words are all present (e.g., "cedar... waxwing" with hesitation)
        words = passphrase_lower.split()
        if all(w in spoken_lower for w in words):
            self.passphrase_checked = True
            _audit_passphrase("pass")
            return "pass"

        _audit_passphrase("fail")
        return "fail"

    async def _sensitive_action_gate(self, action: str) -> Optional[str]:
        """Telegram approval gate for sensitive tools.

        Fully voice-verified calls (voiceprint >= MATCH_THRESHOLD AND
        anti-spoof bonafide — voice_verified is only set on an
        'authenticated' decision) skip the gate. Everyone else — unverified
        mode, spoof-flagged, or voiceprint never confirmed — needs Dan's
        Telegram approval. Fail-closed: any approval-channel failure is a
        'timeout' verdict and the action is refused.
        Returns None to allow the action, or a caller-facing refusal string.
        """
        if self.voice_verified:
            return None

        descriptions = {
            "create_task": "Create a Todoist task",
            "dispatch_work": "Dispatch work to a specialist agent",
            "send_email": "Send an email on your behalf",
            "create_bear_note": "Create a Bear note",
        }
        desc = descriptions.get(action, action)
        verdict = await telegram_approval.request_approval(desc)
        _audit_event({
            "event": "approval",
            "action": action,
            "verdict": verdict,
            "room": self.room.name if self.room else "",
            "voice_verified": self.voice_verified,
        })
        if verdict == "approved":
            return None
        if verdict == "denied":
            return "Dan DENIED this from Telegram — I'm not doing it."
        return ("Dan didn't respond on Telegram in time (auto-deny) — "
                "I'm not doing it.")

    @function_tool
    async def create_task(
        self,
        content: Annotated[str, "The task title/description"],
        priority: Annotated[str, "Priority level: p1 (highest), p2, p3, or p4 (lowest)"] = "p3",
    ) -> str:
        """Create a task in Todoist. Use this when Dan asks to create a task, add a to-do, set up a reminder, or schedule something."""
        if not self.passphrase_checked:
            return "Passphrase not verified yet — please wait a moment before creating tasks."
        blocked = await self._sensitive_action_gate("create_task")
        if blocked:
            return blocked
        result = await call_hermes(f"Create a Todoist task. Content: {content}. Priority: {priority}. Use the mcp__todoist__add_tasks tool to create it. Return the task ID.")
        return f"Done — task created: {content}"

    @function_tool
    async def create_bear_note(
        self,
        title: Annotated[str, "Note title"],
        content: Annotated[str, "Note content/body"],
        tags: Annotated[str, "Comma-separated tags for the note"] = "",
    ) -> str:
        """Create a note in Bear Notes. Use this when Dan says 'take a note', 'note this', 'save this to Bear', or wants to capture an idea or thought."""
        if not self.passphrase_checked:
            return "Passphrase not verified yet — please wait before creating notes."
        blocked = await self._sensitive_action_gate("create_bear_note")
        if blocked:
            return blocked
        tag_str = f" with tags: {tags}" if tags else ""
        result = await call_hermes(f"Create a Bear note with title '{title}' and content: {content}{tag_str}. Use the Bear Notes MCP tool to create it.")
        return f"Done — note saved to Bear: {title}"

    @function_tool
    async def search_bear_notes(
        self,
        query: Annotated[str, "What to search for in Dan's Bear notes"],
    ) -> str:
        """Search Dan's Bear Notes for relevant content. Use this when Dan says 'search my notes', 'what do my notes say about X', or 'check my Bear notes for Y'."""
        result = await call_hermes(f"Search Bear notes for: {query}. Use the Bear Notes MCP search tool. Return the most relevant results as a brief summary.")
        return result

    @function_tool
    async def query_hermes(
        self,
        question: Annotated[str, "The question to look up — about projects, tasks, status, schedules, anything"],
    ) -> str:
        """Query Hermes Agent in real time for project status, task information, schedules, or anything Hermes knows about. Use this when Dan asks 'what's the status of X' or 'check on Y' or needs you to look something up during the call."""
        return await call_hermes(question)

    @function_tool
    async def dispatch_work(
        self,
        task_description: Annotated[str, "Description of the work to dispatch"],
        assignee: Annotated[str, "Which specialist: researcher, staff-eng, senior-eng, writer, security-eng, or quality-eng"] = "researcher",
    ) -> str:
        """Dispatch work to a specialist agent via the Hermes Kanban board. Use this when Dan wants to send work to a specific team member for research, engineering, writing, or QA."""
        if not self.passphrase_checked:
            return "Passphrase not verified yet — please wait before dispatching work."
        blocked = await self._sensitive_action_gate("dispatch_work")
        if blocked:
            return blocked
        result = await call_hermes(f"Create a Kanban board task assigned to {assignee} with this description: {task_description}")
        return f"Dispatched to {assignee}: {task_description}"

    @function_tool
    async def send_email(
        self,
        to: Annotated[str, "Recipient email address"],
        subject: Annotated[str, "Email subject line"],
        body: Annotated[str, "Email body content"],
    ) -> str:
        """Send an email on Dan's behalf via Hermes. Use this when Dan asks you to email someone."""
        if not self.passphrase_checked:
            return "Passphrase not verified — cannot send email without authentication."
        blocked = await self._sensitive_action_gate("send_email")
        if blocked:
            return blocked
        result = await call_hermes(f"Send an email to {to} with subject '{subject}' and body: {body}")
        return f"Email sent to {to}"

    @function_tool
    async def complete_enrollment(self) -> str:
        """Complete voice enrollment for THIS call: banks whatever speech has accumulated (needs at least 20 seconds this call). Enrollment overall needs 60+ seconds across 2+ calls — use this when Dan asks where he stands or wants to bank what he's said so far."""
        if not self.is_enrollment_mode:
            return "Not in enrollment mode."
        await self._run_enrollment()
        state = self._enrollment_state
        if state and not self.is_enrollment_mode:
            return (f"Enrollment complete — {state.progress_text()} banked and voiceprint stored.")
        if state:
            return (f"Banked. Progress: {state.progress_text()}. "
                    f"Keep talking, or pick this up on your next call — progress is saved.")
        return "Need more speech audio. Keep talking for a bit longer."

    @function_tool
    async def end_call(self) -> str:
        """End the call gracefully. Use this when Dan says goodbye, says 'that's all', or the conversation is wrapping up."""
        self.audio_buffer.stop()
        return "Wrapping up — recap your action items and say goodbye."


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    session = AgentSession()

    # --- call_start audit + transcript wiring (never blocks the call) ---
    room_name = ctx.room.name
    caller_number = ""
    try:
        for participant in ctx.room.remote_participants.values():
            attrs = participant.attributes or {}
            caller_number = (
                attrs.get("sip.callerNumber") or attrs.get("sip.caller_id") or ""
            )
            if caller_number:
                break
    except Exception as e:
        log.warning("cole-agent: caller ID lookup failed: %s", e)
    _audit_event({
        "event": "call_start",
        "room": room_name,
        "caller_number": caller_number,
        "job_id": ctx.job.id,
    })

    _call_end_state = {"done": False}

    def _on_close(ev) -> None:
        if _call_end_state["done"]:
            return
        _call_end_state["done"] = True
        reason = str(getattr(ev, "reason", "session_closed"))
        err = getattr(ev, "error", None)
        if err is not None:
            reason += f" error={err}"
        _audit_event({"event": "call_end", "room": room_name, "reason": reason})

    def _dump_transcript(_ev=None) -> None:
        _transcript_write(room_name, _format_history(session))

    try:
        session.on("conversation_item_added", _dump_transcript)
        session.on("close", _on_close)
        _transcript_write(room_name, "")  # create the file at call start
    except Exception as e:
        log.warning("cole-agent: transcript wiring failed: %s", e)

    # Safety net: also emit call_end when the job shuts down (hangup ends the
    # job). _on_close dedupes so only the first close is recorded.
    async def _emit_call_end_on_shutdown():
        _on_close(None)

    try:
        ctx.add_shutdown_callback(_emit_call_end_on_shutdown)
    except Exception as e:
        log.warning("cole-agent: shutdown callback registration failed: %s", e)

    await session.start(agent=ColeAgent(room=ctx.room), room=ctx.room)
    # NOTE: session.start() returns after setup (session continues detached);
    # call_end is emitted by the "close" event handler above, not here.


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))