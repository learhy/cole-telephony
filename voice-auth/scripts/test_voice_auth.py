#!/usr/bin/env python3
"""
Test script for the voice authentication system.
Generates synthetic audio, tests enrollment and verification flows.

Usage:
  python3 test_voice_auth.py
"""

import httpx
import asyncio
import io
import wave
import struct
import numpy as np
import sys
import os

VOICE_AUTH_URL = os.environ.get("VOICE_AUTH_URL", "http://localhost:8011")
ANTI_SPOOF_URL = os.environ.get("ANTI_SPOOF_URL", "http://localhost:8012")
API_KEY = os.environ["VOICEAUTH_API_KEY"]
USER_ID = "test-user"

def generate_test_audio(duration_sec: float = 10.0, freq: float = 150.0, noise: bool = True) -> bytes:
    """Generate synthetic speech-like audio (sine + noise) as WAV bytes."""
    sr = 16000
    t = np.linspace(0, duration_sec, int(sr * duration_sec), endpoint=False)

    # Simulate speech-like signal: base frequency with harmonics + amplitude modulation
    fundamental = freq
    signal = np.sin(2 * np.pi * fundamental * t)
    signal += 0.5 * np.sin(2 * np.pi * 2 * fundamental * t)  # 2nd harmonic
    signal += 0.3 * np.sin(2 * np.pi * 3 * fundamental * t)  # 3rd harmonic

    # Amplitude modulation (simulate speech rhythm)
    am_freq = 3.0  # 3 Hz syllable rate
    am = 0.5 + 0.5 * np.sin(2 * np.pi * am_freq * t)
    signal *= am

    # Add noise for realism
    if noise:
        signal += 0.05 * np.random.randn(len(t))

    # Normalize and convert to 16-bit
    signal = signal / np.max(np.abs(signal)) * 0.7
    signal_int16 = (signal * 32767).astype(np.int16)

    # Write WAV
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(signal_int16.tobytes())

    return buf.getvalue()


def generate_different_voice(duration_sec: float = 10.0) -> bytes:
    """Generate audio with different frequency (simulating different speaker)."""
    return generate_test_audio(duration_sec=duration_sec, freq=250.0)


async def test_health():
    """Test service health endpoints."""
    print("\n=== Testing Health Endpoints ===")
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(f"{VOICE_AUTH_URL}/health")
            print(f"Voice Auth Health: {resp.status_code} {resp.json()}")
        except Exception as e:
            print(f"Voice Auth Health FAILED: {e}")
            return False

        try:
            resp = await client.get(f"{ANTI_SPOOF_URL}/health")
            print(f"Anti-Spoof Health: {resp.status_code} {resp.json()}")
        except Exception as e:
            print(f"Anti-Spoof Health FAILED (non-fatal): {e}")

    return True


async def test_enrollment():
    """Test voice enrollment flow."""
    print("\n=== Testing Enrollment ===")
    audio = generate_test_audio(duration_sec=10.0)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{VOICE_AUTH_URL}/enroll",
            headers={"X-API-Key": API_KEY},
            data={"user_id": USER_ID, "display_name": "Test User"},
            files={"audio": ("audio.wav", audio, "audio/wav")},
        )

    print(f"Enroll Response: {resp.status_code}")
    data = resp.json()
    print(f"  Status: {data.get('status')}")
    print(f"  Embedding dim: {data.get('embedding_dim')}")
    print(f"  Duration: {data.get('audio_duration')}s")
    return data.get("status") == "enrolled"


async def test_verification_accept():
    """Test verification with same voice (should accept)."""
    print("\n=== Testing Verification (same voice → expect accept) ===")
    audio = generate_test_audio(duration_sec=10.0, freq=150.0)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{VOICE_AUTH_URL}/verify",
            headers={"X-API-Key": API_KEY},
            data={
                "user_id": USER_ID,
                "run_anti_spoof": "true",
                "passphrase_ok": "true",
            },
            files={"audio": ("audio.wav", audio, "audio/wav")},
        )

    print(f"Verify Response: {resp.status_code}")
    data = resp.json()
    print(f"  Score: {data.get('score')}")
    print(f"  Threshold: {data.get('threshold')}")
    print(f"  Decision: {data.get('decision')}")
    print(f"  Spoof: {data.get('spoof_decision')}")
    print(f"  Final: {data.get('final_decision')}")
    return data


async def test_verification_reject():
    """Test verification with different voice (should reject)."""
    print("\n=== Testing Verification (different voice → expect reject) ===")
    audio = generate_different_voice(duration_sec=10.0)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{VOICE_AUTH_URL}/verify",
            headers={"X-API-Key": API_KEY},
            data={
                "user_id": USER_ID,
                "run_anti_spoof": "true",
                "passphrase_ok": "false",
            },
            files={"audio": ("audio.wav", audio, "audio/wav")},
        )

    print(f"Verify Response: {resp.status_code}")
    data = resp.json()
    print(f"  Score: {data.get('score')}")
    print(f"  Decision: {data.get('decision')}")
    print(f"  Final: {data.get('final_decision')}")
    return data


async def test_user_status():
    """Test user status retrieval."""
    print("\n=== Testing User Status ===")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{VOICE_AUTH_URL}/users/{USER_ID}",
            headers={"X-API-Key": API_KEY},
        )
    print(f"User Status: {resp.status_code} {resp.json()}")


async def test_stats():
    """Test verification stats."""
    print("\n=== Testing Stats ===")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{VOICE_AUTH_URL}/stats",
            headers={"X-API-Key": API_KEY},
        )
    print(f"Stats: {resp.status_code} {resp.json()}")


async def test_anti_spoof():
    """Test anti-spoofing endpoint directly."""
    print("\n=== Testing Anti-Spoofing ===")
    audio = generate_test_audio(duration_sec=10.0)

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{ANTI_SPOOF_URL}/detect",
                headers={"X-API-Key": API_KEY},
                files={"audio": ("audio.wav", audio, "audio/wav")},
            )
            print(f"Anti-Spoof: {resp.status_code} {resp.json()}")
        except Exception as e:
            print(f"Anti-Spoof FAILED (non-fatal): {e}")


async def test_delete_user():
    """Test user deletion (GDPR/BIPA)."""
    print("\n=== Testing User Deletion ===")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(
            f"{VOICE_AUTH_URL}/users/{USER_ID}",
            headers={"X-API-Key": API_KEY},
        )
    print(f"Delete: {resp.status_code} {resp.json()}")


async def main():
    print("Voice Authentication System Test")
    print("=" * 50)
    print(f"Voice Auth URL: {VOICE_AUTH_URL}")
    print(f"Anti-Spoof URL: {ANTI_SPOOF_URL}")

    # 1. Health check
    healthy = await test_health()
    if not healthy:
        print("\n❌ Services not healthy. Start them first: docker compose up -d")
        sys.exit(1)

    # 2. Enrollment
    enrolled = await test_enrollment()
    if not enrolled:
        print("\n❌ Enrollment failed")
        sys.exit(1)

    # 3. Verification — same voice
    await test_verification_accept()

    # 4. Verification — different voice
    await test_verification_reject()

    # 5. User status
    await test_user_status()

    # 6. Stats
    await test_stats()

    # 7. Anti-spoofing
    await test_anti_spoof()

    # 8. Cleanup
    await test_delete_user()

    print("\n" + "=" * 50)
    print("✅ All tests completed!")


if __name__ == "__main__":
    asyncio.run(main())