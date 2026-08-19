#!/usr/bin/env bash
# Install the quota plugin backend and desktop widget.
# Safe to re-run; configuration is changed only through the Hermes CLI.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Hermes uses %LOCALAPPDATA%\\hermes on Windows. HERMES_HOME wins when the
# operator explicitly supplies it; profile-scoped values are normalized back
# to the shared root because plugins are global, not per-profile.
if [ -n "${HERMES_HOME:-}" ]; then
  HOME_DIR="$HERMES_HOME"
elif [ -n "${LOCALAPPDATA:-}" ] && command -v cygpath >/dev/null 2>&1; then
  HOME_DIR="$(cygpath -u "$LOCALAPPDATA")/hermes"
elif [ -n "${XDG_DATA_HOME:-}" ]; then
  HOME_DIR="$XDG_DATA_HOME/hermes"
else
  HOME_DIR="$HOME/.hermes"
fi
case "$HOME_DIR" in
  */profiles/*) HOME_DIR="${HOME_DIR%%/profiles/*}" ;;
esac

PLUGIN_DIR="$HOME_DIR/plugins/quota"
DESKTOP_DIR="$HOME_DIR/desktop-plugins/quota"

for required in plugin.yaml __init__.py commands.py quota_cache.py quota_providers dashboard/plugin_api.py dashboard/manifest.json desktop/plugin.js; do
  [ -e "$REPO_ROOT/$required" ] || { echo "Missing required file: $required" >&2; exit 1; }
done

mkdir -p "$PLUGIN_DIR" "$DESKTOP_DIR"

# Backend and API. Do not copy the git metadata or caches.
for item in plugin.yaml __init__.py commands.py quota_cache.py quota_providers dashboard LICENSE SPEC.md; do
  [ -e "$REPO_ROOT/$item" ] && cp -Rf "$REPO_ROOT/$item" "$PLUGIN_DIR/"
done
rm -rf "$PLUGIN_DIR/__pycache__" "$PLUGIN_DIR/quota_providers/__pycache__" "$PLUGIN_DIR/dashboard/__pycache__"

# Desktop plugins are loaded from one shared directory, never from profiles.
cp -f "$REPO_ROOT/desktop/plugin.js" "$DESKTOP_DIR/plugin.js"

if ! command -v hermes >/dev/null 2>&1; then
  echo "Installed files, but 'hermes' is not on PATH; enable with: hermes plugins enable quota" >&2
  exit 2
fi
hermes plugins enable quota --no-allow-tool-override

# Desktop Bot chats are profile-scoped. Keep one global plugin copy, but add the
# plugin to every existing profile's enabled list without clobbering any other
# enabled plugin.
if [ -d "$HOME_DIR/profiles" ]; then
  for profile_dir in "$HOME_DIR"/profiles/*; do
    [ -d "$profile_dir" ] || continue
    profile="$(basename "$profile_dir")"
    current="$(hermes -p "$profile" config get plugins.enabled --json 2>/dev/null || printf '[]')"
    desired="$(printf '%s' "$current" | python -c 'import json,sys; v=json.load(sys.stdin); v=v if isinstance(v,list) else []; v=v if "quota" in v else v+["quota"]; print(json.dumps(v))')"
    hermes -p "$profile" config set plugins.enabled "$desired" >/dev/null
  done
fi

printf '\nQuota plugin installed in %s\n' "$HOME_DIR"
printf '%s\n' 'IMPORTANT: restart the Hermes Desktop application completely.'
printf '%s\n' 'Reload desktop plugins alone refreshes JavaScript only; it does not remount dashboard/plugin_api.py.'
printf '%s\n' 'The installer also enables quota in existing profile configs for Bot chats.'
printf '%s\n' 'Verify with: hermes plugins doctor quota && hermes quota refresh'
