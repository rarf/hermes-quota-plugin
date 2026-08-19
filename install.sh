#!/usr/bin/env bash
#
# Quota plugin — one-shot installer.
# Installs BOTH surfaces of the plugin:
#   * backend + dashboard  -> ~/.hermes/plugins/quota/
#   * desktop widget       -> ~/.hermes/desktop-plugins/quota/plugin.js
# then enables the backend plugin and tells you how to reload the desktop.
#
# Idempotent: safe to run more than once. Does not overwrite your config
# blindly — it only ensures `quota` is listed under plugins.enabled.
#
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGINS_DIR="$HERMES_HOME/plugins"
DESKTOP_PLUGINS_DIR="$HERMES_HOME/desktop-plugins"
QUOTA_PLUGINS="$PLUGINS_DIR/quota"
QUOTA_DESKTOP="$DESKTOP_PLUGINS_DIR/quota"

# repo root = directory this script lives in
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Hermes home: $HERMES_HOME"
echo "==> Repo root:    $REPO_ROOT"

# 1) backend + dashboard --------------------------------------------------
echo "==> Installing backend + dashboard to $QUOTA_PLUGINS"
mkdir -p "$QUOTA_PLUGINS"
# copy everything except dev/installer artifacts
for item in plugin.yaml __init__.py commands.py quota_cache.py SPEC.md LICENSE \
            quota_providers dashboard; do
  if [ -e "$REPO_ROOT/$item" ]; then
    cp -Rf "$REPO_ROOT/$item" "$QUOTA_PLUGINS/"
  fi
done

# 2) desktop widget -------------------------------------------------------
echo "==> Installing desktop widget to $QUOTA_DESKTOP"
mkdir -p "$QUOTA_DESKTOP"
if [ -f "$REPO_ROOT/desktop/plugin.js" ]; then
  cp -f "$REPO_ROOT/desktop/plugin.js" "$QUOTA_DESKTOP/plugin.js"
else
  echo "!! desktop/plugin.js not found in repo — widget not installed."
fi

# 3) enable backend plugin ------------------------------------------------
echo "==> Enabling backend plugin 'quota'"
if command -v hermes >/dev/null 2>&1; then
  hermes plugins enable quota 2>/dev/null || true
else
  # fallback: ensure listed under plugins.enabled in config.yaml
  CONFIG="$HERMES_HOME/config.yaml"
  if [ -f "$CONFIG" ]; then
    grep -q "quota" "$CONFIG" || \
      printf '\nplugins:\n  enabled:\n    - quota\n' >> "$CONFIG"
  fi
fi

echo
echo "✅ Quota plugin installed."
echo
echo "Next steps:"
echo "  • Desktop widget: press ⌘K → 'Reload desktop plugins' (or restart Hermes Desktop)."
echo "  • Open the status bar: look for the quota chip (e.g. 'openai-codex 0%')."
echo "  • Click the chip to open the full /quota page."
echo "  • Configure in Settings ▸ Plugins ▸ Quota (show status bar, reset format, …)."
