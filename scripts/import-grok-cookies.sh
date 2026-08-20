#!/usr/bin/env bash
#
# Import Grok session cookies from the clipboard -> grok_session.json
#
# Cross-platform companion to scripts/import-grok-cookies.ps1 (Windows).
# Copies the `Cookie:` header value for a grok.com request, then runs this:
#   ./scripts/import-grok-cookies.sh [output-path]
#
# The payload is written as { "cookies": "<raw Cookie header value>" } so the
# Grok provider's fetcher can reuse your authenticated session.
#
set -euo pipefail

OUTPUT_PATH="${1:-$HOME/grok_session.json}"

# --- read clipboard (cross-platform) ------------------------------------
if command -v pbpaste >/dev/null 2>&1; then
  COOKIE="$(pbpaste)"
elif command -v xclip >/dev/null 2>&1; then
  COOKIE="$(xclip -selection clipboard -o)"
elif command -v xsel >/dev/null 2>&1; then
  COOKIE="$(xsel --clipboard --output)"
elif command -v wl-paste >/dev/null 2>&1; then
  COOKIE="$(wl-paste)"
else
  echo "No clipboard tool found (need pbpaste / xclip / xsel / wl-paste)." >&2
  exit 1
fi

COOKIE="$(printf '%s' "$COOKIE" | tr -d '\r')"
COOKIE="$(printf '%s' "$COOKIE" | sed -E 's/^[[:space:]]*Cookie:[[:space:]]*//I')"
COOKIE="$(printf '%s' "$COOKIE" | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g' | sed -E 's/^ //; s/ $//')"

if [ -z "$COOKIE" ]; then
  echo "Clipboard is empty. Copy the Cookie header value from a grok.com request first." >&2
  exit 1
fi
if ! printf '%s' "$COOKIE" | grep -Eq '(^|;[[:space:]]*)[^=;[:space:]]+='; then
  echo "Clipboard does not look like a Cookie header. No file was written." >&2
  exit 1
fi

PARENT="$(dirname "$OUTPUT_PATH")"
[ -n "$PARENT" ] && mkdir -p "$PARENT"

# Write { "cookies": "..." } without echoing the secret.
python3 - "$OUTPUT_PATH" "$COOKIE" <<'PY'
import json, sys
out_path, cookie = sys.argv[1], sys.argv[2]
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({"cookies": cookie}, fh)
print("Saved Grok session cookies to", out_path)
print("Next: hermes quota refresh")
PY
