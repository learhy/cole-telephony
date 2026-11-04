#!/bin/bash
# Cole Voice Agent — startup script with Voice Authentication
# Uses agent_voice_auth.py (voice biometrics + passphrase two-factor)
set -a
source /home/dan.rohan/.hermes/.env 2>/dev/null
set +a

# LiveKit Cloud credentials
export LIVEKIT_URL="${LIVEKIT_URL}"
export LIVEKIT_API_KEY="${LIVEKIT_API_KEY}"
export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET}"

# Voice Auth Service
export VOICE_AUTH_URL="http://localhost:8011"
export VOICEAUTH_API_KEY="${VOICEAUTH_API_KEY:?set VOICEAUTH_API_KEY in ~/.hermes/.env}"
: "${COLE_PASSPHRASE:?set COLE_PASSPHRASE in ~/.hermes/.env}"

echo "Starting Cole Voice Agent with Voice Authentication..."
echo "  LIVEKIT_URL=$LIVEKIT_URL"
echo "  OPENAI_API_KEY set: $([ -n \"$OPENAI_API_KEY\" ] && echo yes || echo NO)"
echo "  API_SERVER_KEY set: $([ -n \"$API_SERVER_KEY\" ] && echo yes || echo NO)"
echo "  LIVEKIT_API_KEY set: $([ -n \"$LIVEKIT_API_KEY\" ] && echo yes || echo NO)"
echo "  VOICE_AUTH_URL=$VOICE_AUTH_URL"
echo ""

# Check voice auth services are running
if ! curl -s http://localhost:8011/health >/dev/null 2>&1; then
    echo "⚠️  Voice Auth Service not responding on localhost:8001"
    echo "   Start it with: cd /home/dan.rohan/voice-auth && docker compose up -d"
    echo "   Continuing anyway — voice verification will be skipped on errors..."
fi

exec /home/dan.rohan/livekit-venv/bin/python /home/dan.rohan/cole-agent/agent_voice_auth.py start