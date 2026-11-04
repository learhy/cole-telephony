#!/usr/bin/env bash
# daily-sync-push.sh — cron job: mirror live telephony files into the
# cole-telephony repo; commit + push ONLY if something changed.
# Empty stdout on no-change days => cron delivers nothing (watchdog pattern).
set -euo pipefail
REPO="$HOME/work/cole-telephony"
cd "$REPO"

./sync-from-live.sh > /dev/null

if [[ -n "$(git status --porcelain)" ]]; then
  # Never push numbers, IDs or the passphrase. If the live files still
  # hardcode them, fix the live files (read from ~/.hermes/.env) first.
  if ! ./scripts/check-no-secrets.sh; then
    git checkout -- . && git clean -fdq
    echo "cole-telephony: sync BLOCKED by check-no-secrets (nothing committed)" >&2
    exit 1
  fi
  git add -A
  git -c user.name='Cole Sync Bot' -c user.email='cole-sync@users.noreply.github.com' \
    commit -m "chore(sync): mirror live changes $(date -u +%FT%TZ)" > /dev/null
  if git push -q origin main 2>/dev/null; then
    echo "cole-telephony: pushed $(git log -1 --format=%h) — $(git diff --stat HEAD~1 HEAD | tail -1)"
  else
    echo "cole-telephony: PUSH FAILED — commit $(git log -1 --format=%h) is local-only" >&2
    exit 1
  fi
fi
# No changes => no output => no delivery.
