#!/usr/bin/env bash
# check-no-secrets.sh: refuse to commit phone numbers, Twilio/LiveKit IDs,
# server IPs or a hardcoded passphrase. Runs before every sync commit
# (daily-sync-push.sh) and works as a git pre-commit hook:
#   ln -sf ../../scripts/check-no-secrets.sh .git/hooks/pre-commit
#
# Optional extra deny list (fixed strings, one per line, kept OUTSIDE the
# repo because it contains the real values): ~/.config/cole-telephony/deny.txt
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PATTERNS=(
  '\+1[2-9][0-9]{9}\b'                                  # US E.164 phone numbers
  '\b(AC|PN|TK|HH|SK|MG|CA|SM|MM)[0-9a-f]{32}\b'        # Twilio SIDs
  '\b(ST|SDR|SCL|AW)_[A-Za-z0-9]{8,}\b'                 # LiveKit trunk/rule/call IDs
  '\bAPI[A-Za-z0-9]{10,}\b'                             # LiveKit API key IDs
  '\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b'                   # Telegram bot tokens
  'PASSPHRASE[[:space:]]*=[[:space:]]*["'"'"']'          # hardcoded passphrase
  '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b'                     # IPv4 addresses (loopback allowed below)
)
ALLOW='127\.0\.0\.1|0\.0\.0\.0'

mapfile -t FILES < <(git ls-files -co --exclude-standard | grep -v -E '^scripts/check-no-secrets\.sh$')
[[ ${#FILES[@]} -eq 0 ]] && exit 0

HITS=""
for p in "${PATTERNS[@]}"; do
  out=$(grep -n -I -E "$p" -- "${FILES[@]}" 2>/dev/null | grep -v -E "$ALLOW" || true)
  [[ -n "$out" ]] && HITS+="$out"$'\n'
done
DENY="$HOME/.config/cole-telephony/deny.txt"
if [[ -s "$DENY" ]]; then
  out=$(grep -n -I -F -f "$DENY" -- "${FILES[@]}" 2>/dev/null || true)
  [[ -n "$out" ]] && HITS+="$out"$'\n'
fi

if [[ -n "$HITS" ]]; then
  echo "check-no-secrets: BLOCKED. These look like numbers, IDs, IPs or a passphrase:" >&2
  # print file:line only, never the matched value
  printf '%s' "$HITS" | cut -d: -f1,2 | sort -u >&2
  echo "Move them to ~/.hermes/.env (see README > Configuration) and try again." >&2
  exit 1
fi
