#!/usr/bin/env python3
"""
Multi-session voice enrollment state for the Cole voice agent.

Replaces enroll-on-first-20s: enrollment audio now ACCUMULATES ACROSS CALLS.
Each call banks capture chunks (>= MIN_CAPTURE_SECONDS of speech) to disk;
enrollment is only performed once the caller has banked TARGET_TOTAL_SECONDS
spread over at least MIN_SESSIONS distinct calls.

State (JSON + WAV chunk files) lives in ENROLLMENT_STATE_DIR (default
~/cole-agent/enrollment-state) so it survives restarts and hangs up mid-call.

Pure-python, no LiveKit/numpy imports — unit-testable in isolation.
"""

import os
import json
import time

# --- Enrollment targets -----------------------------------------------------
# MIN_ENROLLMENT_SECONDS (20) in agent_voice_auth.py stays the per-capture
# minimum; these two constants add the multi-session requirement on top.
MIN_CAPTURE_SECONDS = 20   # per-capture minimum (mirrors agent constant)
TARGET_TOTAL_SECONDS = 60  # total banked speech needed across all calls
MIN_SESSIONS = 2           # distinct calls that must contribute

ENROLLMENT_STATE_DIR = os.path.expanduser("~/cole-agent/enrollment-state")


def _safe_caller(caller_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in caller_id) or "unknown"


class EnrollmentState:
    """Persistent per-caller enrollment accumulation state."""

    def __init__(self, caller_id: str, state_dir: str = None):
        self.caller_id = caller_id
        self.state_dir = state_dir or ENROLLMENT_STATE_DIR
        os.makedirs(self.state_dir, exist_ok=True)
        self._path = os.path.join(self.state_dir, f"{_safe_caller(caller_id)}.json")
        self.data = self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> dict:
        try:
            with open(self._path, "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        return {
            "caller_id": self.caller_id,
            "total_seconds": 0.0,
            "sessions": [],       # distinct session ids (one per call)
            "chunks": [],         # [{path, seconds, session_id, ts}]
            "cleared_old": False, # stale template deleted before first fresh enroll
            "enrolled": False,
            "enrolled_score": None,
        }

    def save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    # -- capture accumulation --------------------------------------------------

    def add_capture(self, wav_path: str, duration_seconds: float, session_id: str) -> None:
        """Bank one capture chunk from a call."""
        self.data["chunks"].append({
            "path": wav_path,
            "seconds": round(float(duration_seconds), 2),
            "session_id": session_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        if session_id not in self.data["sessions"]:
            self.data["sessions"].append(session_id)
        self.data["total_seconds"] = round(
            self.data["total_seconds"] + float(duration_seconds), 2
        )
        self.save()

    # -- readiness -------------------------------------------------------------

    @property
    def total_seconds(self) -> float:
        return float(self.data.get("total_seconds", 0.0))

    @property
    def sessions_count(self) -> int:
        return len(self.data.get("sessions", []))

    def ready(self) -> bool:
        """True when 60+ seconds are banked across 2+ distinct calls."""
        return (
            not self.data.get("enrolled", False)
            and self.total_seconds >= TARGET_TOTAL_SECONDS
            and self.sessions_count >= MIN_SESSIONS
        )

    def progress_text(self) -> str:
        """Human-readable progress for the agent to speak."""
        return (
            f"{self.total_seconds:.0f} of {TARGET_TOTAL_SECONDS} seconds "
            f"across {self.sessions_count} call"
            f"{'s' if self.sessions_count != 1 else ''}"
        )

    # -- lifecycle flags ---------------------------------------------------------

    def mark_cleared_old(self) -> None:
        """Record that the stale pre-re-enrollment template was deleted."""
        self.data["cleared_old"] = True
        self.save()

    def mark_enrolled(self, score=None) -> None:
        self.data["enrolled"] = True
        self.data["enrolled_score"] = score
        self.save()

    def chunks(self) -> list:
        """Banked chunk dicts, oldest first."""
        return list(self.data.get("chunks", []))
