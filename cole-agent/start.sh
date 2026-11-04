#!/bin/bash
# Cole Voice Agent — startup script (LiveKit Cloud + Voice Auth)
set -a
source /home/dan.rohan/.hermes/.env 2>/dev/null
set +a

export LIVEKIT_URL="${LIVEKIT_URL}"
export LIVEKIT_API_KEY="${LIVEKIT_API_KEY}"
export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET}"

# Voice auth service
export VOICE_AUTH_URL="http://127.0.0.1:8011"
export VOICEAUTH_API_KEY="${VOICEAUTH_API_KEY:?set VOICEAUTH_API_KEY in ~/.hermes/.env}"
: "${COLE_PASSPHRASE:?set COLE_PASSPHRASE in ~/.hermes/.env}"

echo "Starting Cole Voice Agent (LiveKit Cloud + Voice Auth)..."
echo "  LIVEKIT_URL=$LIVEKIT_URL"
echo "  OPENAI_API_KEY set: $([ -n "$OPENAI_API_KEY" ] && echo yes || echo NO)"
echo "  API_SERVER_KEY set: $([ -n "$API_SERVER_KEY" ] && echo yes || echo NO)"
echo "  VOICE_AUTH_URL=$VOICE_AUTH_URL"

exec /home/dan.rohan/livekit-venv/bin/python /home/dan.rohan/cole-agent/agent_voice_auth.py start
