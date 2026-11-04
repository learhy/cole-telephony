#!/usr/bin/env python3
"""Unit tests: multi-session enrollment state machine + telegram approval
offline logic. Run with the livekit venv python. No live services needed."""
import os
import sys
import wave
import json
import math
import shutil
import struct
import tempfile

sys.path.insert(0, os.path.expanduser("~/cole-agent"))
from enrollment_state import EnrollmentState, TARGET_TOTAL_SECONDS, MIN_SESSIONS, MIN_CAPTURE_SECONDS
import telegram_approval as ta

FAILURES = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)

# ---------------------------------------------------------------------------
# Synthetic audio helper
# ---------------------------------------------------------------------------

def make_wav_bytes(seconds: float, freq: float = 220.0) -> bytes:
    """16kHz mono sine WAV — synthetic stand-in for captured speech."""
    rate = 16000
    n = int(rate * seconds)
    buf = __import__("io").BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(n)
        )
        w.writeframes(frames)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Test 1: accumulation across simulated 'calls' (sessions)
# ---------------------------------------------------------------------------

work = tempfile.mkdtemp(prefix="enroll-test-")
try:
    # Session 1: Dan banks 2 x 25s chunks (50s total, 1 session) -> NOT ready
    s1 = EnrollmentState("test.caller", state_dir=work)
    check("fresh state starts empty", s1.total_seconds == 0.0 and s1.sessions_count == 0)

    for i, dur in enumerate([25.0, 25.0]):
        path = os.path.join(work, f"chunk-s1-{i}.wav")
        with open(path, "wb") as f:
            f.write(make_wav_bytes(dur))
        s1.add_capture(path, dur, "call-room-A")
    check("session 1 accumulates 50s", abs(s1.total_seconds - 50.0) < 0.01,
          f"total={s1.total_seconds}")
    check("session 1 counts 1 session", s1.sessions_count == 1)
    check("not ready after session 1 (1 < 2 sessions)", not s1.ready())

    # Simulate the agent process dying: reload state from disk fresh
    s1_reloaded = EnrollmentState("test.caller", state_dir=work)
    check("state persists across simulated restart",
          abs(s1_reloaded.total_seconds - 50.0) < 0.01 and s1_reloaded.sessions_count == 1)

    # Session 2: Dan banks 1 x 25s chunk -> 75s across 2 sessions -> ready
    path2 = os.path.join(work, "chunk-s2-0.wav")
    with open(path2, "wb") as f:
        f.write(make_wav_bytes(25.0, freq=300.0))
    s1_reloaded.add_capture(path2, 25.0, "call-room-B")
    check("session 2 banks across distinct session", s1_reloaded.sessions_count == 2)
    check("ready at 75s across 2 sessions", s1_reloaded.ready(),
          f"total={s1_reloaded.total_seconds} sessions={s1_reloaded.sessions_count}")
    check("ready() requires 60s+",
          s1_reloaded.total_seconds >= TARGET_TOTAL_SECONDS)

    # Edge: 60s in a SINGLE session is still not ready
    work2 = tempfile.mkdtemp(prefix="enroll-test2-")
    try:
        s2 = EnrollmentState("test.caller2", state_dir=work2)
        for i in range(3):
            p = os.path.join(work2, f"c{i}.wav")
            with open(p, "wb") as f:
                f.write(make_wav_bytes(20.0 + i))
            s2.add_capture(p, 20.0 + i, "call-room-C")
        check("60s+ in a single session NOT ready (needs 2 calls)",
              s2.total_seconds >= TARGET_TOTAL_SECONDS and not s2.ready())

        # Edge: 2 sessions but under 60s
        work3 = tempfile.mkdtemp(prefix="enroll-test3-")
        try:
            s3 = EnrollmentState("test.caller3", state_dir=work3)
            for i, sid in enumerate(["call-room-E", "call-room-F"]):
                p = os.path.join(work3, f"e{i}.wav")
                with open(p, "wb") as f:
                    f.write(make_wav_bytes(20.0 + i))
                s3.add_capture(p, 20.0 + i, sid)
            check("2 sessions but <60s NOT ready",
                  not s3.ready(), f"total={s3.total_seconds} sessions={s3.sessions_count}")
        finally:
            shutil.rmtree(work3, ignore_errors=True)
    finally:
        shutil.rmtree(work2, ignore_errors=True)

    # Sub-capture rejection is enforced by the agent (duration < MIN -> return);
    # MIN_CAPTURE_SECONDS constant mirrors it:
    check("MIN_CAPTURE_SECONDS is 20 (mirrors agent MIN_ENROLLMENT_SECONDS)",
          MIN_CAPTURE_SECONDS == 20)

    # mark_enrolled flips ready() off permanently
    s1_reloaded.mark_enrolled(score=0.71)
    check("enrolled state disables ready()", not s1_reloaded.ready())
    s1_reloaded2 = EnrollmentState("test.caller", state_dir=work)
    check("enrolled flag persists", s1_reloaded2.data["enrolled"] is True
          and s1_reloaded2.data["enrolled_score"] == 0.71)

    # progress_text sanity
    check("progress_text renders",
          "75" in s1_reloaded.progress_text() and "2 calls" in s1_reloaded.progress_text(),
          s1_reloaded.progress_text())
finally:
    shutil.rmtree(work, ignore_errors=True)

# ---------------------------------------------------------------------------
# Test 2: telegram offline logic (classification + cursor)
# ---------------------------------------------------------------------------

check("classify APPROVE", ta._classify_reply("APPROVE") == "approved")
check("classify approve ", ta._classify_reply("  approve ") == "approved")
check("classify Approved!", ta._classify_reply("Approved!") == "approved")
check("classify DENY", ta._classify_reply("DENY") == "denied")
check("classify denied", ta._classify_reply("denied") == "denied")
check("classify random text is None", ta._classify_reply("what's up") is None)
check("classify empty is None", ta._classify_reply("") is None)
check("classify 'approve it later' -> approved (first-match)",
      ta._classify_reply("approve it later") == "approved")

# Cursor round-trip
tmp_cursor = tempfile.mktemp(prefix="cursor-test-")
old_cursor_path = ta.CURSOR_PATH
try:
    ta.CURSOR_PATH = tmp_cursor
    check("cursor defaults to 0", ta._load_cursor() == 0)
    ta._save_cursor(12345)
    check("cursor round-trips", ta._load_cursor() == 12345)
finally:
    ta.CURSOR_PATH = old_cursor_path
    try:
        os.remove(tmp_cursor)
    except OSError:
        pass

# Fail-closed: no creds -> timeout
old_env = ta.TELEGRAM_ENV_PATH
try:
    ta.TELEGRAM_ENV_PATH = "/nonexistent/.env"
    import asyncio
    verdict = asyncio.run(ta.request_approval("fail-closed test", amount_of_time=5))
    check("missing creds fail-closed -> timeout", verdict == "timeout", verdict)
finally:
    ta.TELEGRAM_ENV_PATH = old_env

print()
print("FAILURES:", FAILURES if FAILURES else "none")
sys.exit(1 if FAILURES else 0)
