# Voice Authentication System

Voice biometric authentication for Dan's LiveKit voice agent using:
- **SpeechBrain ECAPA-TDNN** — speaker verification (192-dim embeddings, ~1.5% EER)
- **AASIST** — anti-spoofing (voice clone / replay / synthetic detection)
- **PostgreSQL + pgvector** — voiceprint embedding storage
- **FastAPI** — microservice architecture
- **Docker Compose** — deployment

## Architecture

```
Dan calls → LiveKit Cloud SIP → cole-agent (agent_voice_auth.py)
  ├── Factor 1: Passphrase challenge (COLE_PASSPHRASE)
  ├── Factor 2: Voice biometric verification
  │     ├── Capture 15s of audio from LiveKit session
  │     ├── POST to voice-auth:8001/verify
  │     │     ├── Extract ECAPA-TDNN embedding
  │     │     ├── Compare to enrolled voiceprint (cosine similarity)
  │     │     └── Call anti-spoof:8002/detect (AASIST)
  │     └── Decision: authenticated / rejected
  └── Both factors must pass for full access

Enrollment:
  Dan calls (first time) → Agent detects no voiceprint
  → Dan speaks naturally for ~30s
  → Audio sent to voice-auth:8001/enroll
  → 192-dim embedding stored in PostgreSQL + pgvector
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| voice-auth-db | 5433 | PostgreSQL 16 + pgvector |
| voice-auth | 8001 | SpeechBrain ECAPA-TDNN verification API |
| anti-spoof | 8002 | AASIST anti-spoofing API |

## Quick Start

```bash
# 1. Start the auth services
cd /home/dan.rohan/voice-auth
docker compose up -d

# 2. Wait for models to load (~2 min on first start — downloads ECAPA-TDNN weights)
docker compose logs -f voice-auth

# 3. Run tests
python3 scripts/test_voice_auth.py

# 4. Deploy the updated agent
cd /home/dan.rohan/cole-agent
# Update start.sh to use agent_voice_auth.py instead of agent.py
```

## Voice Auth API

### Enroll
```bash
curl -X POST http://localhost:8001/enroll \
  -H "X-API-Key: $VOICEAUTH_API_KEY" \
  -F "user_id=dan.rohan" \
  -F "display_name=Dan Rohan" \
  -F "audio=@voice_sample.wav"
```

### Verify
```bash
curl -X POST http://localhost:8001/verify \
  -H "X-API-Key: $VOICEAUTH_API_KEY" \
  -F "user_id=dan.rohan" \
  -F "run_anti_spoof=true" \
  -F "passphrase_ok=true" \
  -F "audio=@test_voice.wav"
```

### Check Enrollment Status
```bash
curl -H "X-API-Key: $VOICEAUTH_API_KEY" \
  http://localhost:8001/users/dan.rohan
```

### Delete Voiceprint (GDPR/BIPA)
```bash
curl -X DELETE -H "X-API-Key: $VOICEAUTH_API_KEY" \
  http://localhost:8001/users/dan.rohan
```

### Verification Stats
```bash
curl -H "X-API-Key: $VOICEAUTH_API_KEY" \
  http://localhost:8001/stats
```

## Configuration

Environment variables (in `.env` or docker-compose.yml):

| Variable | Default | Description |
|----------|---------|-------------|
| `VOICEAUTH_DB_PASSWORD` | (required, no default) | PostgreSQL password |
| `VOICEAUTH_API_KEY` | (required, no default) | API key for service auth |
| `THRESHOLD` | 0.25 | Cosine similarity threshold (tune for FAR/FRR) |
| `DEVICE` | cpu | torch device (cpu or cuda) |
| `MIN_AUDIO_DURATION_SEC` | 5 | Minimum audio for verification |
| `MAX_AUDIO_DURATION_SEC` | 120 | Maximum audio length |

## Threshold Tuning

The cosine similarity threshold (default 0.25) is the critical security parameter:
- **Higher** (0.30+) = more secure, more false rejects (Dan might get rejected)
- **Lower** (0.20) = smoother UX, more false accepts (impostor risk)

Monitor FAR/FRR via the `/stats` endpoint and adjust based on real data.

## Template Adaptation

Voiceprints drift over time (colds, aging, stress). The system supports:
- **Automatic adaptation**: After each successful verification, the enrolled embedding is updated (80/20 blend) to track gradual voice changes
- **Re-enrollment**: Dan can re-enroll by deleting his voiceprint and calling again

## Privacy & Compliance

- Voiceprints stored as **embeddings (192-dim vectors), not raw audio**
- Delete endpoint provided for GDPR/BIPA right to deletion
- All verification attempts logged for audit
- Services bind to localhost only (no external access)