#!/usr/bin/env python3
"""
Enroll Dan's voiceprint from a WAV file.

Usage:
  python3 enroll_dan.py <path_to_wav_file>

The WAV file should contain 20-60 seconds of Dan speaking naturally.
Record with:
  ffmpeg -f alsa -i default -t 30 -ar 16000 -ac 1 dan_enrollment.wav
"""

import httpx
import sys
import os

VOICE_AUTH_URL = os.environ.get("VOICE_AUTH_URL", "http://localhost:8011")
API_KEY = os.environ["VOICEAUTH_API_KEY"]
USER_ID = "dan.rohan"

async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 enroll_dan.py <path_to_wav_file>")
        print("Record with: ffmpeg -f alsa -i default -t 30 -ar 16000 -ac 1 dan_enrollment.wav")
        sys.exit(1)

    wav_path = sys.argv[1]
    if not os.path.exists(wav_path):
        print(f"Error: {wav_path} not found")
        sys.exit(1)

    print(f"Enrolling Dan's voiceprint from {wav_path}...")

    import asyncio
    async with httpx.AsyncClient(timeout=120.0) as client:
        with open(wav_path, "rb") as f:
            resp = await client.post(
                f"{VOICE_AUTH_URL}/enroll",
                headers={"X-API-Key": API_KEY},
                data={
                    "user_id": USER_ID,
                    "display_name": "Dan Rohan",
                    "phone_number": "+120****1228",
                },
                files={"audio": ("audio.wav", f, "audio/wav")},
            )

    result = resp.json()
    if result.get("status") == "enrolled":
        print(f"✅ Enrollment successful!")
        print(f"   Embedding dim: {result.get('embedding_dim')}")
        print(f"   Audio duration: {result.get('audio_duration')}s")
        print(f"\nDan's voiceprint is now stored. The agent will verify his voice on each call.")
    else:
        print(f"❌ Enrollment failed: {result}")
        sys.exit(1)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())