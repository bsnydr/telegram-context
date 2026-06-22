#!/usr/bin/env bash
# Wrapper that pulls secrets from the macOS Keychain and execs the bot.
# Used by launchd (com.telegram-context.plist) and for a foreground run.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

token="$(security find-generic-password -s telegram-bot-token -w 2>/dev/null || true)"
if [[ -z "$token" ]]; then
  echo "ERROR: Keychain item 'telegram-bot-token' not found." >&2
  echo "Add it with: security add-generic-password -s telegram-bot-token -a \"\$USER\" -w <BOT_TOKEN>" >&2
  exit 1
fi
export TELEGRAM_BOT_TOKEN="$token"

allowed="$(security find-generic-password -s telegram-allowed-ids -w 2>/dev/null || true)"
if [[ -n "$allowed" ]]; then
  export TELEGRAM_ALLOWED_USER_IDS="$allowed"
fi

exec "$HERE/.venv/bin/python" "$HERE/telegram_context.py"
