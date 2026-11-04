#!/usr/bin/env bash
# sync-from-live.sh — mirror the LIVE Cole telephony files into this repo.
# The live directories remain canonical (systemd services run from them);
# this repo is the version-controlled mirror. Run from repo root.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sync() { # sync <src> <dest-dir>
  local src="$1" dest="$ROOT/$2"
  mkdir -p "$dest"
  rsync -a "$src" "$dest/"
}

# Cole agent (canonical: ~/cole-agent) — code + docs only (excludes handle
# runtime data: transcripts/, auth-audit.log, backups, state dirs)
rsync -a \
  --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'transcripts/' --exclude 'auth-audit.log' \
  --exclude '*.bak*' --exclude 'enrollment-state/' \
  --exclude '*.wav' --exclude 'logs/' \
  --exclude 'agent.py.orig' \
  "$HOME/cole-agent/" cole-agent/

# Voice-auth stack (canonical: ~/voice-auth) — compose, docs, service code
sync "$HOME/voice-auth/docker-compose.yml" voice-auth/
sync "$HOME/voice-auth/README.md" voice-auth/
[ -d "$HOME/voice-auth/services" ] && sync "$HOME/voice-auth/services/" voice-auth/services/
[ -d "$HOME/voice-auth/scripts" ] && sync "$HOME/voice-auth/scripts/" voice-auth/scripts/
[ -d "$HOME/voice-auth/alembic" ] && sync "$HOME/voice-auth/alembic/" voice-auth/alembic/

# SMS gateway (canonical: ~/software/cole-gateway) — app code only, no DB
for f in app.py README.md verify_otp.py; do
  [ -f "$HOME/software/cole-gateway/$f" ] && sync "$HOME/software/cole-gateway/$f" cole-gateway/
done

# Helper scripts (canonical: ~/bin)
for f in call-dan text-dan; do
  [ -f "$HOME/bin/$f" ] && sync "$HOME/bin/$f" bin/
done

# Deployment configs — sanitized example only (real livekit-config.yaml has keys)
[ -f "$HOME/livekit-config.example.yaml" ] && sync "$HOME/livekit-config.example.yaml" deploy/

# Generated calibration/ops reports
[ -d "$HOME/cole-agent/docs" ] && rsync -a --exclude '*.wav' "$HOME/cole-agent/docs/" docs/ 2>/dev/null || true

echo "sync complete: $(date -u +%FT%TZ)"
