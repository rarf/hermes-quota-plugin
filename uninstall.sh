#!/usr/bin/env bash
#
# Quota plugin — uninstaller.
# Removes both surfaces (backend + dashboard, and the desktop widget) and
# disables the backend plugin. If you cloned the repo elsewhere, this only
# touches the installed locations under ~/.hermes.
#
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
QUOTA_PLUGINS="$HERMES_HOME/plugins/quota"
QUOTA_DESKTOP="$HERMES_HOME/desktop-plugins/quota"

echo "==> Removing backend + dashboard: $QUOTA_PLUGINS"
rm -rf "$QUOTA_PLUGINS"

echo "==> Removing desktop widget: $QUOTA_DESKTOP"
rm -rf "$QUOTA_DESKTOP"

echo "==> Disabling backend plugin 'quota'"
if command -v hermes >/dev/null 2>&1; then
  hermes plugins disable quota 2>/dev/null || true
fi

echo
echo "✅ Quota plugin removed."
echo "   Restart Hermes Desktop (or ⌘K → 'Reload desktop plugins')."
